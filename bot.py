"""BackUp Bot — full Discord server archival (channels · roles · members ·
messages · attachments · emojis). Arabic + English UX.

Slash commands:
  /backup            — run a full backup of this server
  /backup_channel    — back up a single channel
  /status            — last backup summary
  /download          — DM the latest .zip snapshot to the caller
  /schedule          — auto-backup every N hours (0 = off)
  /search            — search saved messages by keyword
  /help              — show all commands
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from aiohttp import web

# Transient network errors that should never error a command — the underlying
# restore/backup runs as its own task and keeps going; only the progress edit
# might blip. Swallow these alongside discord.HTTPException.
_TRANSIENT = (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError)

import backup
import config
import restore as restore_engine
import storage

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(name)s  %(message)s")
log = logging.getLogger("bot")

# In-memory ring buffer of recent log lines, exposed via the admin API so logs
# can be read remotely (e.g. to drive automated tests) without Railway access.
from collections import deque as _deque
_LOG_RING: "_deque[str]" = _deque(maxlen=3000)


class _RingHandler(logging.Handler):
    def emit(self, record):
        try:
            _LOG_RING.append(self.format(record))
        except Exception:
            pass


_ring_handler = _RingHandler()
_ring_handler.setFormatter(logging.Formatter("%(asctime)s  %(name)s  %(levelname)s  %(message)s"))
logging.getLogger().addHandler(_ring_handler)

# Privileged intents — must be enabled in the Developer Portal too.
intents = discord.Intents.default()
intents.members = True           # snapshot_members
intents.message_content = True   # archive message text

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# --------------------------------------------------------------------------- #
#  Download web server — serves big .zip snapshots that exceed Discord's 25 MB
#  upload cap, behind a secret token so the link is private (not browsable).
# --------------------------------------------------------------------------- #
_PORT = int(os.getenv("PORT", "8080"))
_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
# Stable, unguessable root secret. Set DOWNLOAD_SECRET to override; otherwise
# derived from the bot token so links survive restarts without extra config.
DOWNLOAD_SECRET = (os.getenv("DOWNLOAD_SECRET", "").strip()
                   or hashlib.sha256((config.DISCORD_TOKEN or "x").encode()).hexdigest()[:24])
# Secret that gates the remote control API (/admin/...): trigger backups/restores
# and read logs from outside Discord (used to drive automated tests). Set
# ADMIN_SECRET to override; falls back to a token derived from DOWNLOAD_SECRET.
ADMIN_SECRET = (os.getenv("ADMIN_SECRET", "").strip()
                or hashlib.sha256(("admin:" + DOWNLOAD_SECRET).encode()).hexdigest()[:32])
_web_started = False
_cleanup_started = False


def _guild_token(guild_id: int) -> str:
    """Per-server download token. HMAC the root secret with the guild id so each
    server gets a *different*, unguessable token — and knowing one server's link
    never reveals another's (you can't swap the guild id and reach its backup)."""
    return hmac.new(DOWNLOAD_SECRET.encode(), str(guild_id).encode(),
                    hashlib.sha256).hexdigest()[:24]


# Google Drive mirror — {guild_id: shared_drive_url}, pushed by the mirror job
# via /admin/<secret>/set_drive_links and persisted on the volume. When a guild
# has a Drive link, every place that hands out a download link prefers it.
_DRIVE_LINKS_PATH = os.path.join(config.DATA_DIR, "drive_links.json")
try:
    with open(_DRIVE_LINKS_PATH) as _f:
        _drive_links: dict[str, str] = json.load(_f)
except (OSError, ValueError):
    _drive_links = {}


def _latest_link(guild_id: int) -> Optional[str]:
    """A stable per-server link that always serves this guild's newest snapshot.
    Prefers the Google Drive mirror when one exists for this guild."""
    drive = _drive_links.get(str(guild_id))
    if drive:
        return drive
    if not _PUBLIC_DOMAIN:
        return None
    return f"https://{_PUBLIC_DOMAIN}/latest/{_guild_token(guild_id)}/{guild_id}"


def _resolve_restore_link(link: str) -> tuple[Optional[str], Optional[int]]:
    """Map a pasted backup link to (download_url, local_gid) for /restore.
    Drive links the bot itself issued resolve back to the local snapshot's guild
    id (Drive serves an HTML viewer page, not zip bytes). Our own /latest links
    get raw=1 appended so the Drive redirect doesn't hand the downloader HTML."""
    for gid, url in _drive_links.items():
        if link == url:
            return None, int(gid)
    if _PUBLIC_DOMAIN and f"//{_PUBLIC_DOMAIN}/latest/" in link and "raw=" not in link:
        sep = "&" if "?" in link else "?"
        return link + sep + "raw=1", None
    return link, None


# Shield icon — used as the bot avatar and as the embed thumbnail.
_ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "assets", "logos", "01-backupbot-512.png")


def _icon_url() -> Optional[str]:
    """Public URL to the shield icon, served by our own web server."""
    if not _PUBLIC_DOMAIN:
        return None
    return f"https://{_PUBLIC_DOMAIN}/icon.png"


async def _h_health(request):
    """Live status page: real-time server-folder count + stored data size."""
    s = storage.storage_stats()
    used_gb = s["used_bytes"] / 1024**3
    total_gb = s["total_bytes"] / 1024**3
    pct = (s["used_bytes"] / s["total_bytes"] * 100) if s["total_bytes"] else 0
    bar_w = 24
    filled = int(bar_w * pct / 100)
    bar = "█" * filled + "░" * (bar_w - filled)
    body = (
        "BackUp Bot — OK\n"
        "──────────────────────────────\n"
        f"📁 Servers (folders): {s['guilds']}\n"
        f"📦 Snapshots stored:  {s['snapshots']}\n"
        f"💾 Data used:         {used_gb:.2f} GB / {total_gb:.1f} GB\n"
        f"[{bar}] {pct:.1f}%\n"
        f"🕒 as of {s.get('updated','')}\n"
    )
    fmt = request.query.get("format", "")
    if fmt == "json" or "application/json" in request.headers.get("Accept", ""):
        return web.json_response(s)
    return web.Response(text=body)


async def _h_icon(request):
    """Serve the shield icon so embeds can show it as a thumbnail."""
    if os.path.isfile(_ICON_PATH):
        return web.FileResponse(_ICON_PATH, headers={"Cache-Control": "public, max-age=86400"})
    return web.Response(status=404, text="no icon")


async def _h_latest(request):
    """Always serve the NEWEST .zip snapshot for a guild — a stable link."""
    gid = request.match_info["gid"]
    if not gid.isdigit():
        return web.Response(status=400, text="bad request")
    if not hmac.compare_digest(request.match_info["token"], _guild_token(int(gid))):
        return web.Response(status=403, text="forbidden")
    # Drive mirror first: existing /latest links keep working but the bytes come
    # from Google Drive instead of the Railway volume. ?raw=1 bypasses the
    # redirect for callers that need the actual zip bytes (mirror job, /restore).
    drive = _drive_links.get(gid)
    if drive and "raw" not in request.query:
        raise web.HTTPFound(drive)
    # Enforce retention on access: dedup + drop zips older than the retention
    # window, so an expired backup link 404s instead of serving stale data.
    storage.prune_backups(int(gid))
    bdir = storage.backups_dir(int(gid))
    try:
        zips = [f for f in os.listdir(bdir) if f.endswith(".zip")]
    except OSError:
        zips = []
    if not zips:
        return web.Response(
            status=404,
            text="no backup yet (or it expired after "
                 f"{int(config.BACKUP_RETENTION_DAYS)} days) — run /backup")
    newest = max(zips, key=lambda f: os.path.getmtime(os.path.join(bdir, f)))
    return web.FileResponse(
        os.path.join(bdir, newest),
        headers={"Content-Disposition": f'attachment; filename="{newest}"'})


async def _h_download(request):
    gid, fname = request.match_info["gid"], request.match_info["fname"]
    if not gid.isdigit() or "/" in fname or ".." in fname or not fname.endswith(".zip"):
        return web.Response(status=400, text="bad request")
    if not hmac.compare_digest(request.match_info["token"], _guild_token(int(gid))):
        return web.Response(status=403, text="forbidden")
    path = os.path.join(storage.backups_dir(int(gid)), fname)
    if not os.path.isfile(path):
        return web.Response(status=404, text="not found")
    return web.FileResponse(
        path, headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# --------------------------------------------------------------------------- #
#  Remote control API (/admin/<secret>/...) — trigger backups/restores and read
#  logs from outside Discord, so tests can be driven programmatically. Secret-
#  gated; returns JSON. Read endpoints are GET, actions are POST.
# --------------------------------------------------------------------------- #
def _admin_ok(request) -> bool:
    return hmac.compare_digest(request.match_info.get("secret", ""), ADMIN_SECRET)


async def _h_admin_ping(request):
    if not _admin_ok(request):
        return web.json_response({"error": "forbidden"}, status=403)
    return web.json_response({
        "ok": True,
        "user": str(bot.user) if bot.user else None,
        "guilds": [{"id": g.id, "name": g.name,
                    "admin": bool(g.me and g.me.guild_permissions.administrator)}
                   for g in bot.guilds],
        "in_flight": list(in_flight.keys()),
    })


async def _h_admin_logs(request):
    if not _admin_ok(request):
        return web.json_response({"error": "forbidden"}, status=403)
    n = int(request.query.get("n", "120"))
    grep = request.query.get("grep", "")
    lines = list(_LOG_RING)
    if grep:
        lines = [ln for ln in lines if grep.lower() in ln.lower()]
    lines = lines[-n:]
    if request.query.get("format") == "json":
        return web.json_response({"lines": lines, "count": len(lines)})
    return web.Response(text="\n".join(lines))


async def _h_admin_stats(request):
    if not _admin_ok(request):
        return web.json_response({"error": "forbidden"}, status=403)
    return web.json_response(storage.storage_stats())


async def _h_admin_integrity(request):
    if not _admin_ok(request):
        return web.json_response({"error": "forbidden"}, status=403)
    gid = request.query.get("guild", "")
    if not gid.isdigit():
        return web.json_response({"error": "guild query param required"}, status=400)
    conn = await asyncio.to_thread(storage.open_db, int(gid))
    run = await asyncio.to_thread(storage.latest_run, conn)

    def _counts():
        m = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        a = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
        return m, a
    msgs, atts = await asyncio.to_thread(_counts)
    await asyncio.to_thread(conn.close)
    integ = _integrity_of(run) if run else None
    return web.json_response({
        "guild": int(gid), "total_messages": msgs, "total_attachments": atts,
        "last_run": run, "integrity": integ,
        "download_link": _latest_link(int(gid)),
    })


async def _admin_backup_task(guild: discord.Guild):
    """Run a full backup + zip for a guild as a background task (admin-triggered)."""
    if guild.id in in_flight:
        return
    if restoring:
        log.info("admin backup for %s deferred — a restore is in progress", guild.id)
        return
    p = backup.Progress()
    in_flight[guild.id] = p
    try:
        log.info("admin-triggered backup starting for %s (%s)", guild.name, guild.id)
        await backup.run_backup(guild, p)
        await asyncio.to_thread(storage.make_zip, guild.id, "admin")
        log.info("admin-triggered backup DONE for %s — integrity %s%%",
                 guild.id, p.integrity().get("score"))
    except Exception as e:  # noqa: BLE001
        log.exception("admin backup failed for %s: %s", guild.id, e)
    finally:
        in_flight.pop(guild.id, None)


async def _h_admin_backup(request):
    if not _admin_ok(request):
        return web.json_response({"error": "forbidden"}, status=403)
    gid = request.query.get("guild", "")
    if not gid.isdigit():
        return web.json_response({"error": "guild query param required"}, status=400)
    guild = bot.get_guild(int(gid))
    if guild is None:
        return web.json_response({"error": "bot is not in that guild"}, status=404)
    if guild.id in in_flight:
        return web.json_response({"status": "already_running", "guild": guild.id})
    bot.loop.create_task(_admin_backup_task(guild))
    return web.json_response({"status": "started", "guild": guild.id,
                              "hint": "poll /admin/<secret>/logs and /integrity"})


async def _admin_restore_task(guild: discord.Guild, link: str, with_messages: bool):
    log.info("admin-triggered restore starting for %s from %s", guild.id, link[:60])
    restoring.add(guild.id)
    try:
        url, local_gid = _resolve_restore_link(link)
        if local_gid is not None:
            rp = await restore_engine.restore(local_gid, guild, with_messages=with_messages)
        else:
            rp = await restore_engine.restore_from_zip(url, guild, with_messages=with_messages)
        log.info("admin-triggered restore DONE for %s — roles=%s channels=%s msgs=%s err=%s",
                 guild.id, rp.roles, rp.channels, rp.messages, rp.error)
    except Exception as e:  # noqa: BLE001
        log.exception("admin restore failed for %s: %s", guild.id, e)
    finally:
        restoring.discard(guild.id)


async def _h_admin_restore(request):
    if not _admin_ok(request):
        return web.json_response({"error": "forbidden"}, status=403)
    gid = request.query.get("guild", "")
    link = request.query.get("link", "")
    with_messages = request.query.get("messages", "1") != "0"
    if not gid.isdigit() or not link:
        return web.json_response({"error": "guild + link query params required"}, status=400)
    guild = bot.get_guild(int(gid))
    if guild is None:
        return web.json_response({"error": "bot is not in that guild"}, status=404)
    bot.loop.create_task(_admin_restore_task(guild, link, with_messages))
    return web.json_response({"status": "started", "guild": guild.id,
                              "messages": with_messages,
                              "hint": "poll /admin/<secret>/logs"})


async def _h_admin_dedup(request):
    """Reclaim disk by collapsing duplicate (sha256-identical) attachments across
    every backed-up guild. No owner slash command needed."""
    if not _admin_ok(request):
        return web.json_response({"error": "forbidden"}, status=403)
    before = storage.storage_stats().get("used_bytes", 0)
    stats = await asyncio.to_thread(storage.dedup_all)
    after = storage.storage_stats().get("used_bytes", 0)
    log.info("admin-triggered dedup DONE — files_removed=%s bytes_reclaimed=%s",
             stats.get("files_removed"), stats.get("bytes_reclaimed"))
    return web.json_response({
        "status": "done", **stats,
        "volume_used_before": before, "volume_used_after": after,
    })


async def _h_admin_set_drive_links(request):
    """Merge + persist a {guild_id: drive_url} map (POSTed as JSON by the VPS
    mirror job). _latest_link then hands out the Drive link for those guilds."""
    if not _admin_ok(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        incoming = json.loads(await request.text())
        if not isinstance(incoming, dict):
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        return web.json_response({"error": "body must be a JSON object {gid: url}"},
                                 status=400)
    _drive_links.update({str(k): str(v) for k, v in incoming.items()})
    try:
        with open(_DRIVE_LINKS_PATH, "w") as f:
            json.dump(_drive_links, f, indent=1)
    except OSError as e:
        return web.json_response({"error": f"persist failed: {e}"}, status=500)
    log.info("drive links updated — %d guilds now mirror to Drive", len(_drive_links))
    return web.json_response({"status": "ok", "links": len(_drive_links)})


async def _h_admin_backup_all(request):
    """Fire a full backup for EVERY guild the bot is in (skips ones already
    running). Returns the list of guilds queued."""
    if not _admin_ok(request):
        return web.json_response({"error": "forbidden"}, status=403)
    queued, skipped = [], []
    for guild in bot.guilds:
        if guild.id in in_flight:
            skipped.append(guild.id)
            continue
        bot.loop.create_task(_admin_backup_task(guild))
        queued.append(guild.id)
    log.info("admin-triggered backup_all — queued %s, skipped %s (in-flight)",
             len(queued), len(skipped))
    return web.json_response({"status": "started", "queued": queued,
                              "skipped_in_flight": skipped,
                              "hint": "poll /admin/<secret>/logs and /integrity?guild="})


async def _h_admin_cmd(request):
    """Unified AI control plane — ONE secret-gated command that dispatches a curated
    allowlist of admin actions. `?do=<action>`. Read actions are safe over GET;
    mutating actions also accept POST. Deliberately NO arbitrary code/shell execution:
    the secret travels in the URL (and lands in access logs), so a raw exec endpoint
    would be a takeover hole. Everything an operator actually needs is a named action."""
    if not _admin_ok(request):
        return web.json_response({"error": "forbidden"}, status=403)
    do = request.query.get("do", "diag").lower()

    if do == "help":
        return web.json_response({
            "actions": ["diag", "scan", "errors", "logs", "integrity_all", "zipscan",
                        "backup", "backup_all", "dedup", "prune", "leave",
                        "verify_clone"],
            "usage": "/admin/<secret>/cmd?do=<action>[&guild=&source=&target=&n=&grep=]",
        })

    def _integ_for(gid: int):
        try:
            conn = storage.open_db(gid)
            run = storage.latest_run(conn)
            msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            conn.close()
            return (_integrity_of(run) if run else None), msgs
        except Exception:  # noqa: BLE001
            return None, None

    if do == "diag":
        stats = storage.storage_stats()
        guilds = []
        for g in bot.guilds:
            integ, msgs = await asyncio.to_thread(_integ_for, g.id)
            integ = integ or {}
            age = storage.snapshot_age_seconds(g.id)
            guilds.append({
                "id": g.id, "name": g.name,
                "admin": bool(g.me and g.me.guild_permissions.administrator),
                "score": integ.get("score"),
                "blocked": len(integ.get("channels_skipped", [])),
                "channels": f"{integ.get('channels_read')}/{integ.get('channels_total')}",
                "messages": msgs,
                "last_backup_min_ago": round(age / 60) if age is not None else None,
            })
        errors = [ln for ln in _LOG_RING
                  if any(k in ln for k in ("ERROR", "Traceback", "Exception"))][-15:]
        return web.json_response({
            "bot": str(bot.user) if bot.user else None,
            "guild_count": len(bot.guilds), "in_flight": list(in_flight.keys()),
            "disk": stats, "guilds": guilds, "recent_errors": errors,
        })

    if do == "errors":
        n = int(request.query.get("n", "40"))
        errs = [ln for ln in _LOG_RING if any(
            k in ln for k in ("ERROR", "Traceback", "Exception")) or "failed" in ln.lower()]
        return web.json_response({"errors": errs[-n:], "count": len(errs)})

    if do == "logs":
        n = int(request.query.get("n", "60"))
        grep = request.query.get("grep", "")
        lines = [ln for ln in _LOG_RING if grep.lower() in ln.lower()] if grep else list(_LOG_RING)
        return web.json_response({"lines": lines[-n:]})

    if do == "scan":
        # Classify the console into the failure modes that matter so a freeze /
        # truncated backup / failed restore can be spotted in ONE call.
        ring = list(_LOG_RING)
        sigs = {
            "freeze":           ("heartbeat blocked", "blocked for more than",
                                 "has stopped responding"),
            "resume_retry":     ("interrupted", "resuming", "retry", "backoff"),
            "blocked_channels": ("no access", "skipping", "Forbidden"),
            "restore_done":     ("restore DONE",),
            "restore_failed":   ("restore failed",),
            "backup_done":      ("backup DONE",),
            "exceptions":       ("Traceback", "Exception", " ERROR "),
        }
        out = {}
        for name, needles in sigs.items():
            hits = [ln for ln in ring
                    if any(nd.lower() in ln.lower() for nd in needles)]
            out[name] = {"count": len(hits), "last": hits[-1] if hits else None}
        # crude freeze verdict: any freeze line that has no later "backup DONE"
        verdict = "FROZEN?" if out["freeze"]["count"] and (
            not out["backup_done"]["last"]
            or out["freeze"]["last"] > out["backup_done"]["last"]) else "ok"
        return web.json_response({"verdict": verdict, "signals": out,
                                  "ring_lines": len(ring)})

    if do == "integrity_all":
        out = []
        for g in bot.guilds:
            integ, _ = await asyncio.to_thread(_integ_for, g.id)
            integ = integ or {}
            out.append({"id": g.id, "name": g.name,
                        "admin": bool(g.me and g.me.guild_permissions.administrator),
                        "score": integ.get("score"),
                        "blocked": len(integ.get("channels_skipped", []))})
        return web.json_response({"guilds": out})

    if do == "zipscan":
        # Which guilds' newest zip carries members Google Drive flags as malware
        # (executables, or archives nested under attachments/)? The Drive mirror
        # job calls this to decide which guilds need a sanitized upload, so the
        # flagged-guild list no longer has to be maintained by hand.
        import zipfile
        risky_ext = (".exe", ".dll", ".scr", ".bat", ".cmd", ".msi", ".vbs",
                     ".ps1", ".jar", ".apk")
        nested_ext = (".zip", ".rar", ".7z", ".tar", ".gz", ".iso")

        def _scan(gid: int):
            bdir = storage.backups_dir(gid)
            try:
                zips = [f for f in os.listdir(bdir) if f.endswith(".zip")]
            except OSError:
                zips = []
            if not zips:
                return None
            newest = max(zips, key=lambda f: os.path.getmtime(os.path.join(bdir, f)))
            try:
                with zipfile.ZipFile(os.path.join(bdir, newest)) as z:
                    names = z.namelist()
            except Exception:  # noqa: BLE001 — unreadable zip: treat as risky
                return {"zip": newest, "members": None, "risky": -1}
            bad = [n for n in names if n.lower().endswith(risky_ext)
                   or ("attachments/" in n and n.lower().endswith(nested_ext))]
            return {"zip": newest, "members": len(names), "risky": len(bad),
                    "risky_members": bad[:20]}

        q = request.query.get("guild", "")
        targets = [int(q)] if q.isdigit() else [g.id for g in bot.guilds]
        out = {}
        for gid in targets:
            res = await asyncio.to_thread(_scan, gid)
            if res is not None:
                out[str(gid)] = res
        return web.json_response(
            {"guilds": out,
             "risky_guilds": [g for g, r in out.items() if r["risky"] != 0]})

    if do == "dedup":
        before = storage.storage_stats().get("used_bytes", 0)
        s = await asyncio.to_thread(storage.dedup_all)
        after = storage.storage_stats().get("used_bytes", 0)
        return web.json_response({"status": "done", **s,
                                  "used_before": before, "used_after": after})

    if do == "prune":
        keep = int(request.query.get("keep", "1"))
        removed = await asyncio.to_thread(
            storage.prune_all, keep, config.BACKUP_RETENTION_DAYS)
        return web.json_response({"status": "done", "zips_removed": removed})

    if do in ("backup", "backup_all"):
        targets = ([bot.get_guild(int(request.query["guild"]))]
                   if do == "backup" and request.query.get("guild", "").isdigit()
                   else list(bot.guilds))
        if do == "backup" and (not targets or targets[0] is None):
            return web.json_response({"error": "guild required / not in guild"}, status=400)
        queued = []
        for g in targets:
            if g and g.id not in in_flight:
                bot.loop.create_task(_admin_backup_task(g))
                queued.append(g.id)
        return web.json_response({"status": "started", "queued": queued})

    if do == "leave":
        gid = request.query.get("guild", "")
        guild = bot.get_guild(int(gid)) if gid.isdigit() else None
        if guild is None:
            return web.json_response({"error": "not in guild"}, status=404)
        name = guild.name
        await guild.leave()
        log.info("admin-triggered LEAVE guild %s (%s)", name, gid)
        return web.json_response({"status": "left", "guild": int(gid), "name": name})

    if do == "verify_clone":
        # Deep per-room check: did EVERY source room's messages land in the target?
        # Read-only — back up the target first (do=backup&guild=<target>) so its DB
        # reflects the restored content, then call this.
        s_gid = request.query.get("source", "")
        t_gid = request.query.get("target", "")
        if not (s_gid.isdigit() and t_gid.isdigit()):
            return web.json_response({"error": "source + target (guild ids) required"},
                                     status=400)

        def _counts(gid):
            conn = storage.open_db(int(gid))
            rows = conn.execute(
                "SELECT channel_name, COUNT(*) FROM messages GROUP BY channel_name"
            ).fetchall()
            conn.close()
            return {(r[0] or "?"): r[1] for r in rows}

        src = await asyncio.to_thread(_counts, s_gid)
        tgt = await asyncio.to_thread(_counts, t_gid)
        rooms, full, partial, missing = [], 0, 0, 0
        for name, sc in sorted(src.items(), key=lambda x: -x[1]):
            tc = tgt.get(name, 0)
            # restore skips system/content-less msgs, so ~70%+ replayed = full room.
            if sc > 0 and tc == 0:
                status = "missing"; missing += 1
            elif tc >= max(1, int(sc * 0.7)):
                status = "full"; full += 1
            else:
                status = "partial"; partial += 1
            rooms.append({"room": name, "source": sc, "target": tc, "status": status})
        return web.json_response({
            "source": int(s_gid), "target": int(t_gid),
            "summary": {"rooms_total": len(src), "full": full,
                        "partial": partial, "missing": missing},
            "rooms": rooms,
        })

    return web.json_response({"error": f"unknown action '{do}'",
                              "hint": "do=help"}, status=400)


async def _start_webserver():
    app = web.Application(client_max_size=0)
    app.router.add_get("/", _h_health)
    app.router.add_get("/icon.png", _h_icon)
    app.router.add_get("/latest/{token}/{gid}", _h_latest)
    app.router.add_get("/dl/{token}/{gid}/{fname}", _h_download)
    # Remote control API (secret-gated)
    app.router.add_get("/admin/{secret}/ping", _h_admin_ping)
    app.router.add_get("/admin/{secret}/logs", _h_admin_logs)
    app.router.add_get("/admin/{secret}/stats", _h_admin_stats)
    app.router.add_get("/admin/{secret}/integrity", _h_admin_integrity)
    app.router.add_post("/admin/{secret}/backup", _h_admin_backup)
    app.router.add_post("/admin/{secret}/backup_all", _h_admin_backup_all)
    app.router.add_post("/admin/{secret}/dedup", _h_admin_dedup)
    app.router.add_post("/admin/{secret}/set_drive_links", _h_admin_set_drive_links)
    app.router.add_post("/admin/{secret}/restore", _h_admin_restore)
    # Unified control plane — one command, GET or POST, dispatches by ?do=<action>
    app.router.add_route("*", "/admin/{secret}/cmd", _h_admin_cmd)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", _PORT).start()
    log.info("download web server listening on :%d (public=%s)", _PORT, _PUBLIC_DOMAIN or "none")

# A single in-flight backup task per guild, so /backup can't be spammed.
in_flight: dict[int, backup.Progress] = {}
# Guilds currently being RESTORED. Backups (auto + admin) stand down while any
# restore runs: a restore is event-loop-heavy (webhook replay), and running a
# backup at the same time is what blocked the gateway heartbeat (the freeze).
restoring: set[int] = set()
# Auto-backup schedules, in hours, per guild.
schedules: dict[int, int] = {}


# --------------------------------------------------------------------------- #
#  Lifecycle
# --------------------------------------------------------------------------- #
@bot.event
async def on_ready():
    # Register slash commands PER-GUILD so they appear instantly (global sync can
    # take up to ~1h), and clear the global set so nothing shows up duplicated.
    try:
        cmds = tree.get_commands()
        tree.clear_commands(guild=None)
        await tree.sync()                 # wipe stale global commands on Discord
        for c in cmds:                    # keep them in-memory for copy_global_to
            tree.add_command(c)
        for g in bot.guilds:
            tree.clear_commands(guild=g)
            tree.copy_global_to(guild=g)
            await tree.sync(guild=g)
        log.info("slash commands synced to %d guild(s)", len(bot.guilds))
    except Exception as e:                # noqa: BLE001
        log.warning("command sync issue: %s", e)
    log.info("Logged in as %s — in %d guild(s).", bot.user, len(bot.guilds))
    # Set the bot's avatar to the shield icon — once, only if none is set yet,
    # so we never hit Discord's 2-changes/hour avatar rate limit on restarts.
    try:
        if bot.user.avatar is None and os.path.isfile(_ICON_PATH):
            with open(_ICON_PATH, "rb") as fh:
                await bot.user.edit(avatar=fh.read())
            log.info("bot avatar set to shield icon")
    except Exception as e:  # noqa: BLE001
        log.info("avatar set skipped: %s", e)
    global _web_started, _cleanup_started
    if not _web_started:
        _web_started = True
        bot.loop.create_task(_start_webserver())
    if not _cleanup_started:
        _cleanup_started = True
        bot.loop.create_task(_cleanup_loop())
    if config.AUTO_BACKUP_HOURS > 0:
        for g in bot.guilds:
            schedules[g.id] = config.AUTO_BACKUP_HOURS
        bot.loop.create_task(_auto_loop())


async def _cleanup_loop():
    """Hourly retention sweep across all servers: deletes zips older than
    config.BACKUP_RETENTION_DAYS (24h default) + any duplicates, so the volume
    stays small even with no link hits or new backups."""
    cycle = 0
    while True:
        try:
            removed = await asyncio.to_thread(storage.prune_all)
            if removed:
                log.info("retention sweep removed %d expired/duplicate zip(s)", removed)
            # Once a day, collapse duplicate attachment bytes fleet-wide (re-hashing
            # the whole volume hourly would be wasteful; go-forward dedup is automatic).
            if cycle % 24 == 0:
                s = await asyncio.to_thread(storage.dedup_all)
                if s.get("bytes_reclaimed"):
                    log.info("daily dedup reclaimed %s bytes (%s files)",
                             s["bytes_reclaimed"], s["files_removed"])
        except Exception as e:  # noqa: BLE001
            log.warning("cleanup loop error: %s", e)
        cycle += 1
        await asyncio.sleep(3600)   # every hour


WELCOME_URL = os.getenv("LANDING_URL", "https://discordbackupbot.vercel.app")


@bot.event
async def on_guild_join(guild: discord.Guild):
    # Make slash commands appear instantly the moment the bot is added.
    try:
        tree.clear_commands(guild=guild)
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        log.info("synced commands to new guild %s (%s)", guild.id, guild.name)
    except Exception as e:  # noqa: BLE001
        log.warning("guild_join sync failed: %s", e)

    # First-join welcome message — how to use it + the website.
    try:
        me = guild.me
        ch = guild.system_channel
        if ch is None or not ch.permissions_for(me).send_messages:
            ch = next((c for c in guild.text_channels
                       if c.permissions_for(me).send_messages), None)
        if ch is None:
            return
        e = discord.Embed(
            title="🛡️  BackUp Bot — أهلاً فيك! / Welcome!",
            description=(
                "**أحفظ سيرفرك بالكامل وأقدر أنسخه لسيرفر ثاني.**\n"
                "*I back up your whole server — and can clone it into another one.*\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"),
            color=0xE8001C, url=WELCOME_URL)
        if me.display_avatar:
            e.set_thumbnail(url=me.display_avatar.url)
        # Step 1 — the one thing the owner must do.
        e.add_field(
            name="①  أعطني Administrator / Give me Admin",
            value=("عشان أقرأ **كل** روم بدون نقص.\n"
                   "so I can read **every** channel with no gaps."),
            inline=False)
        # Core commands, each explained simply in both languages.
        e.add_field(
            name="②  الأوامر الأساسية / Core commands",
            value=(
                "💾 **`/backup`** — نسخة كاملة للسيرفر / full server backup\n"
                "📥 **`/download`** — رابط تحميل النسخة / get the download link\n"
                "♻️ **`/restore`** — استعد أو انسخ سيرفر (رابط أو ملف) / restore or "
                "clone a server (link **or** uploaded file)\n"
                "📊 **`/status`** · **`/stats`** — حالة النسخة / backup status\n"
                "🔎 **`/search`** — دوّر بالرسائل المحفوظة / search saved messages\n"
                "⏰ **`/schedule`** — نسخ تلقائي / automatic backups\n"
                "❓ **`/help`** — كل الأوامر / every command"),
            inline=False)
        # If added without admin, the one-click fix.
        if not me.guild_permissions.administrator:
            invite = config.invite_url()
            if invite:
                e.add_field(
                    name="⚠️  محتاج Administrator / I need Admin",
                    value=(f"➡️ **[اضغط هنا / Click here]({invite})**\n"
                           "بدونها بتطلع رومات ناقصة. / without it, channels are skipped."),
                    inline=False)
        e.add_field(
            name="✅ مميزات / Highlights",
            value=("• نسخ **كامل** — رسائل، صور، رولات، رومات / **full** clone — messages, "
                   "images, roles, rooms\n"
                   "• ملف سيرفرك **خاص** فيك / your server's files stay **private**\n"
                   "• يكمل لو انقطع / **resumes** if interrupted"),
            inline=False)
        e.add_field(
            name="🌐",
            value=(f"[الموقع / Website]({WELCOME_URL})  ·  "
                   "Programmed by **[@KhaledQ84Ever](https://x.com/KhaledQ84Ever)**"),
            inline=False)
        e.set_footer(text="تحتاج Manage Server للأوامر · Manage Server required to run commands")
        await ch.send(embed=e)
        log.info("sent welcome to %s", guild.id)
    except Exception as ex:  # noqa: BLE001
        log.warning("welcome message failed: %s", ex)


async def _auto_loop():
    """Background loop: re-runs /backup for every scheduled guild."""
    while True:
        if restoring:
            # A restore is running — don't compete for the event loop (avoids the
            # heartbeat freeze). Re-check shortly instead of starting backups now.
            await asyncio.sleep(30)
            continue
        for guild_id, hours in list(schedules.items()):
            try:
                guild = bot.get_guild(guild_id)
                if guild and guild_id not in in_flight:
                    progress = backup.Progress()
                    in_flight[guild_id] = progress
                    try:
                        await backup.run_backup(guild, progress)
                        await asyncio.to_thread(storage.make_zip, guild_id, "auto")
                    finally:
                        in_flight.pop(guild_id, None)
            except Exception as e:
                log.warning("auto-backup for %s failed: %s", guild_id, e)
        # The shortest scheduled interval drives the loop cadence.
        await asyncio.sleep(min(schedules.values(), default=24) * 3600)


def _fmt_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    i = 0
    f = float(n)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}"


import json as _json


def _integrity_of(run: Optional[dict]) -> Optional[dict]:
    """Parse the persisted integrity dict from a backup run row, if present."""
    if not run:
        return None
    raw = run.get("integrity_json")
    if not raw:
        return None
    try:
        return _json.loads(raw)
    except (ValueError, TypeError):
        return None


def _integrity_emoji(score: int) -> str:
    return "🟢" if score >= 95 else "🟡" if score >= 75 else "🔴"


def _integrity_gaps(integ: dict) -> list:
    """Human-readable list of what lowered the score (empty = perfect)."""
    gaps = []
    skipped = integ.get("channels_skipped") or []
    if skipped:
        shown = ", ".join("#" + s for s in skipped[:6])
        more = f" +{len(skipped) - 6} more" if len(skipped) > 6 else ""
        gaps.append(f"{len(skipped)} channel(s) NOT read (need Administrator): {shown}{more}")
    if integ.get("attachments_failed"):
        gaps.append(f"{integ['attachments_failed']} attachment(s) failed (CDN link expired)")
    if integ.get("attachments_oversize"):
        gaps.append(f"{integ['attachments_oversize']} attachment(s) skipped (over size limit)")
    return gaps


def _integrity_field_value(integ: dict) -> str:
    score = int(integ.get("score", 0))
    bar = "█" * int(20 * score / 100) + "░" * (20 - int(20 * score / 100))
    head = (f"{_integrity_emoji(score)} **{score}%** complete\n`{bar}`\n"
            f"channels {integ.get('channels_read', 0)}/{integ.get('channels_total', 0)} · "
            f"files {integ.get('attachments_stored', 0)}/{integ.get('attachments_total', 0)}")
    gaps = _integrity_gaps(integ)
    if gaps:
        head += "\n⚠️ " + "\n⚠️ ".join(gaps)
    return head


import re as _re
# Bare custom-emoji id tokens (`:899471179822293042:`) that Discord can't render
# inside an embed — strip them so channel names read cleanly.
_EMOJI_ID_RE = _re.compile(r":\d{15,}:")


def _clean_channel_name(name: str) -> str:
    """Drop unrenderable custom-emoji id codes + tidy separators for display."""
    cleaned = _EMOJI_ID_RE.sub("", name or "")
    cleaned = cleaned.strip(" \t·・|┃〢-")
    return cleaned or (name or "")


def _ago(seconds: Optional[float]) -> str:
    """Human 'time band' since an event (e.g. '3m ago', '2h ago', '1d ago')."""
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m ago"
    return f"{s // 86400}d {s % 86400 // 3600}h ago"


def _admin_only(interaction: discord.Interaction) -> bool:
    """Only members with Manage Server or Administrator can run backups."""
    p = interaction.user.guild_permissions  # type: ignore[union-attr]
    return p.administrator or p.manage_guild


# --------------------------------------------------------------------------- #
#  /backup
# --------------------------------------------------------------------------- #
@tree.command(name="backup",
              description="نسخة احتياطية كاملة للسيرفر / Full server backup")
@app_commands.describe(
    force="تجاهل نسخة عالقة وابدأ من جديد / clear a stuck backup and restart (resumes where it stopped)")
async def backup_cmd(interaction: discord.Interaction, force: bool = False):
    if not interaction.guild:
        return await interaction.response.send_message(
            "هذا الأمر داخل السيرفر فقط / server-only command.", ephemeral=True)
    if not _admin_only(interaction):
        return await interaction.response.send_message(
            "⛔ تحتاج صلاحية Manage Server / you need Manage Server.",
            ephemeral=True)
    if interaction.guild.id in in_flight:
        if not force:
            return await interaction.response.send_message(
                "⏳ في نسخة قيد التشغيل / a backup is already running.\n"
                "لو علِقت، استخدم **/backup force:True** لإعادة التشغيل من حيث وقف.\n"
                "If it's stuck, run **/backup force:True** to restart (resumes where it stopped).",
                ephemeral=True)
        # Force: clear the stuck entry. Mark the old progress cancelled so any
        # lingering task stops, then start fresh (incremental — resumes from last saved).
        old = in_flight.pop(interaction.guild.id, None)
        if old is not None:
            old.cancelled = True
        log.warning("force-cleared stuck backup for guild %s", interaction.guild.id)

    await interaction.response.defer(thinking=True)
    progress = backup.Progress()
    in_flight[interaction.guild.id] = progress

    async def _ticker():
        """Edit the original message every 5 s with live progress."""
        while interaction.guild.id in in_flight:
            await asyncio.sleep(5)
            try:
                await interaction.edit_original_response(
                    embed=_progress_embed(interaction.guild, progress, done=False))
            except Exception:
                break

    bot.loop.create_task(_ticker())
    try:
        await backup.run_backup(interaction.guild, progress)
        # Zipping ~1GB is heavy + synchronous — run it off the event loop so the
        # bot's heartbeat doesn't block (was causing "heartbeat blocked" + freezes).
        zip_path = await asyncio.to_thread(storage.make_zip, interaction.guild.id, "manual")
        zip_size = os.path.getsize(zip_path)
    except Exception as e:
        progress.error = str(e)
        log.exception("backup error")
        return await interaction.followup.send(f"💥 فشلت / failed: `{e}`")
    finally:
        in_flight.pop(interaction.guild.id, None)

    await interaction.edit_original_response(
        embed=_progress_embed(interaction.guild, progress, done=True,
                              zip_path=zip_path, zip_size=zip_size))
    # Clean, direct summary the user asked for: just the link + last backup file.
    link = _latest_link(interaction.guild.id)
    await interaction.followup.send(
        embed=_summary_embed(interaction.guild, zip_path, zip_size, link))


def _summary_embed(guild: discord.Guild, zip_path: str, zip_size: int,
                   link: Optional[str]) -> discord.Embed:
    """Clean post-backup card: latest-backup link + last snapshot file only."""
    e = discord.Embed(title="📊 آخر نسخة احتياطية / Latest backup",
                       color=0x57F287)
    icon = _icon_url()
    if icon:
        e.set_thumbnail(url=icon)
    if link:
        e.add_field(name="🔗 رابط سيرفرك / Your server's link",
                    value=link, inline=False)
    e.add_field(name="📦 آخر ملف نسخة / Last backup file",
                value=f"`{os.path.basename(zip_path)}` ({_fmt_size(zip_size or 0)})",
                inline=False)
    e.add_field(
        name="♻️ استعد بأي سيرفر / Restore to ANY server",
        value=("بأي سيرفر فيه البوت اكتب:\nIn any server that has the bot, run:\n"
               "`/restore link:` ‹الرابط فوق / the link above›"),
        inline=False)
    e.set_footer(text="رابط سري — استعمله مع /restore بأي سيرفر · secret link — restore it to ANY server via /restore")
    return e


def _progress_embed(guild: discord.Guild, p: backup.Progress, *,
                    done: bool, zip_path: Optional[str] = None,
                    zip_size: Optional[int] = None) -> discord.Embed:
    pct = (p.channels_done / p.channels_total * 100) if p.channels_total else 0
    bar_w = 18
    filled = int(bar_w * pct / 100)
    bar = "█" * filled + "░" * (bar_w - filled)
    title = "✅ اكتمل النسخ / Backup complete" if done \
            else "💾 جارٍ النسخ / Backup running"
    e = discord.Embed(title=title, color=0x57F287 if done else 0x5865F2)
    icon = _icon_url()
    if icon:
        e.set_thumbnail(url=icon)
    e.add_field(name="📁 Server", value=guild.name, inline=False)
    e.add_field(name="📊 Progress",
                value=f"`{bar}` {pct:.0f}%\n"
                      f"{p.channels_done} / {p.channels_total} channels",
                inline=False)
    # Use a >=1s denominator so the first sub-second tick doesn't report
    # absurd "912 GB/s" / millions-of-msgs-per-second to the user.
    el = max(p.elapsed(), 1.0)
    speed = p.bytes / el
    e.add_field(name="💬 Messages",    value=f"{p.messages:,}",        inline=True)
    e.add_field(name="📎 Attachments", value=f"{p.attachments:,}",     inline=True)
    e.add_field(name="💾 Downloaded",  value=_fmt_size(p.bytes),       inline=True)
    e.add_field(name="🚀 Speed",       value=f"{_fmt_size(int(speed))}/s", inline=True)
    e.add_field(name="⚡ Msgs/s",      value=f"{p.messages / el:,.0f}", inline=True)
    e.add_field(name="⏱️ Elapsed",     value=f"{p.elapsed():.0f} s",    inline=True)
    if not done and p.current_channel:
        e.add_field(name="🔄 Now archiving",
                    value=f"#{_clean_channel_name(p.current_channel)}", inline=False)
    # Integrity score (0–100%) — how complete this backup is + what's missing.
    if done:
        integ = p.integrity()
        e.add_field(name="🛡️ نسبة الاكتمال / Integrity",
                    value=_integrity_field_value(integ), inline=False)
        e.color = (0x57F287 if integ["score"] >= 95
                   else 0xFEE75C if integ["score"] >= 75 else 0xED4245)
    # LOUD warning when channels were skipped — this is THE reason a backup comes
    # out incomplete (e.g. "only 207 messages"). Show the count + the 1-click admin
    # link so the user fixes it instead of trusting a partial backup.
    skipped = list(getattr(p, "skipped", []) or [])
    if done and skipped:
        me = guild.me
        no_admin = bool(me and not me.guild_permissions.administrator)
        invite = config.invite_url()
        names = ", ".join("#" + _clean_channel_name(s) for s in skipped[:8])
        more = f" +{len(skipped) - 8} more" if len(skipped) > 8 else ""
        val = (f"**{len(skipped)} روم تم تخطيها — نسختك ناقصة!**\n"
               f"**{len(skipped)} channel(s) SKIPPED — your backup is INCOMPLETE**\n"
               f"`{names}{more}`")
        if invite and no_admin:
            val += (f"\n➡️ [اضغط لإعطاء البوت Administrator ثم أعد /backup]"
                    f"({invite})\nGrant Administrator, then run /backup again.")
        else:
            val += ("\nأعطِ رول البوت **Administrator** ثم أعد /backup\n"
                    "Give the bot's role **Administrator**, then re-run /backup.")
        e.add_field(name="⚠️ رومات ناقصة / MISSING CHANNELS", value=val, inline=False)
    if done and zip_path:
        e.add_field(name="📦 Snapshot",
                    value=f"`{os.path.basename(zip_path)}` ({_fmt_size(zip_size or 0)})",
                    inline=False)
        link = _latest_link(guild.id)
        if link:
            e.add_field(name="🔗 رابط سيرفرك / Your server's link",
                        value=link, inline=False)
        e.set_footer(text="رابط سري — استعمله مع /restore بأي سيرفر · secret link — restore it to ANY server via /restore")
    return e


# --------------------------------------------------------------------------- #
#  /backup_channel
# --------------------------------------------------------------------------- #
@tree.command(name="backup_channel",
              description="نسخة احتياطية لروم واحد / Back up one channel")
@app_commands.describe(channel="الروم / which channel")
async def backup_channel_cmd(interaction: discord.Interaction,
                              channel: discord.TextChannel):
    if not interaction.guild:
        return
    if not _admin_only(interaction):
        return await interaction.response.send_message(
            "⛔ Manage Server required.", ephemeral=True)
    await interaction.response.defer(thinking=True)
    progress = backup.Progress()
    in_flight[interaction.guild.id] = progress
    try:
        await backup.run_backup(interaction.guild, progress,
                                specific_channel=channel)
    except Exception as e:
        return await interaction.followup.send(f"💥 فشل / failed: `{e}`")
    finally:
        in_flight.pop(interaction.guild.id, None)
    await interaction.followup.send(
        embed=_progress_embed(interaction.guild, progress, done=True))


# --------------------------------------------------------------------------- #
#  /status
# --------------------------------------------------------------------------- #
@tree.command(name="status",
              description="معلومات آخر نسخة / Last backup info")
async def status_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        return
    conn = storage.open_db(interaction.guild.id)
    run = storage.latest_run(conn)
    total_msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    total_atts = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
    conn.close()
    if not run:
        return await interaction.response.send_message(
            "ماكو نسخة بعد / no backups yet. Use **/backup** to create one.",
            ephemeral=True)
    folder_bytes = await asyncio.to_thread(
        storage.dir_size, storage.guild_dir(interaction.guild.id))
    e = discord.Embed(title="📊 آخر نسخة احتياطية / Latest backup",
                      color=0x5865F2)
    e.add_field(name="💬 إجمالي الرسائل / Total messages",
                value=f"**{total_msgs:,}**", inline=True)
    e.add_field(name="📎 إجمالي المرفقات / Total attachments",
                value=f"**{total_atts:,}**", inline=True)
    e.add_field(name="📁 Channels", value=str(run["channels"]), inline=True)
    e.add_field(name="🕒 آخر نسخة / Last backup",
                value=(run["ended_at"] or run["started_at"] or "—"), inline=True)
    e.add_field(name="➕ آخر تشغيل أضاف / Last run added",
                value=f"+{run['messages']:,} msgs", inline=True)
    e.add_field(name="🗄️ On-disk total",
                value=_fmt_size(folder_bytes), inline=True)
    if run["error"]:
        e.add_field(name="⚠️ Error", value=str(run["error"]), inline=False)
    link = _latest_link(interaction.guild.id)
    if link:
        e.add_field(name="🔗 رابط سيرفرك / Your server's link",
                    value=link, inline=False)
    e.set_footer(text="رابط سري — استعمله مع /restore بأي سيرفر · secret link — restore it to ANY server via /restore")
    await interaction.response.send_message(embed=e, ephemeral=True)


# --------------------------------------------------------------------------- #
#  /stats — real-time storage numbers (servers + data) across all backups
# --------------------------------------------------------------------------- #
@tree.command(name="stats",
              description="إحصائيات مباشرة / Live storage stats (servers + data)")
async def stats_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    s = await asyncio.to_thread(storage.storage_stats)
    used, total = s["used_bytes"], s["total_bytes"]
    pct = (used / total * 100) if total else 0
    bar_w = 20
    bar = "█" * int(bar_w * pct / 100) + "░" * (bar_w - int(bar_w * pct / 100))

    e = discord.Embed(title="📊 إحصائيات مباشرة / Live stats", color=0x5865F2)
    icon = _icon_url()
    if icon:
        e.set_thumbnail(url=icon)

    # ── Global (all servers) ──────────────────────────────────────────────
    e.add_field(name="📁 السيرفرات / Servers", value=f"{s['guilds']}", inline=True)
    e.add_field(name="📦 النسخ / Snapshots", value=f"{s['snapshots']}", inline=True)
    e.add_field(name="🔄 يعمل الآن / Running now",
                value=f"{len(in_flight)}", inline=True)
    e.add_field(name="💾 إجمالي البيانات / Total data used",
                value=f"`{bar}`\n{_fmt_size(used)} / {_fmt_size(total)} ({pct:.1f}%)",
                inline=False)

    # ── This server's own data + time band ────────────────────────────────
    if interaction.guild:
        gid = interaction.guild.id
        my_bytes = await asyncio.to_thread(storage.dir_size, storage.guild_dir(gid))
        files = await asyncio.to_thread(storage.guild_file_count, gid)
        age = await asyncio.to_thread(storage.snapshot_age_seconds, gid)
        conn = await asyncio.to_thread(storage.open_db, gid)
        run = await asyncio.to_thread(storage.latest_run, conn)
        await asyncio.to_thread(conn.close)

        e.add_field(name="🗂️ حجم سيرفرك / This server",
                    value=_fmt_size(my_bytes), inline=True)
        e.add_field(name="📄 الملفات / Data files", value=f"{files:,}", inline=True)
        e.add_field(name="🕒 آخر نسخة / Last backup", value=_ago(age), inline=True)
        if run:
            e.add_field(name="💬 الرسائل / Messages",
                        value=f"{run['messages']:,}", inline=True)
            e.add_field(name="📎 المرفقات / Attachments",
                        value=f"{run['attachments']:,}", inline=True)
        # Retention countdown — how long until this snapshot auto-deletes.
        if age is not None:
            left = config.BACKUP_RETENTION_DAYS * 86400 - age
            left_txt = _ago(-left).replace(" ago", "") if left > 0 else "expired"
            e.add_field(name="⏳ يُحذف بعد / Auto-delete in",
                        value=(f"{left_txt}" if left > 0
                               else "expired — run /backup"), inline=True)
        # Live download bar if a backup is in progress for THIS server.
        if gid in in_flight:
            p = in_flight[gid]
            cpct = (p.channels_done / p.channels_total * 100) if p.channels_total else 0
            cb = "█" * int(20 * cpct / 100) + "░" * (20 - int(20 * cpct / 100))
            e.add_field(
                name="⬇️ تحميل مباشر / Live download",
                value=(f"`{cb}` {cpct:.0f}%\n"
                       f"{p.channels_done}/{p.channels_total} channels · "
                       f"{_fmt_size(p.bytes)} · {p.messages:,} msgs · "
                       f"#{_clean_channel_name(p.current_channel)}"),
                inline=False)

    e.set_footer(text=f"real-time · يُحدّث لحظياً · as of {s.get('updated','')}")
    await interaction.followup.send(embed=e)


# --------------------------------------------------------------------------- #
#  /dedup — reclaim disk by collapsing duplicate attachments (sha256)
# --------------------------------------------------------------------------- #
@tree.command(name="dedup",
              description="توفير مساحة: حذف المرفقات المكررة / reclaim space (dedup attachments)")
@app_commands.describe(
    all_servers="نظّف كل السيرفرات (مالك البوت) / dedup ALL servers (owner only)")
async def dedup_cmd(interaction: discord.Interaction, all_servers: bool = False):
    if not interaction.guild and not all_servers:
        return await interaction.response.send_message(
            "هذا الأمر داخل السيرفر فقط / server-only command.", ephemeral=True)
    if not _admin_only(interaction):
        return await interaction.response.send_message(
            "⛔ تحتاج صلاحية Manage Server / you need Manage Server.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)

    if all_servers:
        # Only the bot's application owner may sweep every server's data.
        app = await bot.application_info()
        if not (app.owner and interaction.user.id == app.owner.id):
            return await interaction.followup.send(
                "⛔ لمالك البوت فقط / bot owner only.", ephemeral=True)
        s = await asyncio.to_thread(storage.dedup_all)
        e = discord.Embed(
            title="🧹 توفير المساحة / Dedup complete (all servers)",
            color=0x57F287)
        e.add_field(name="📁 السيرفرات / Servers", value=f"{s['guilds']}", inline=True)
        e.add_field(name="🗑️ ملفات محذوفة / Files removed",
                    value=f"{s['files_removed']:,}", inline=True)
        e.add_field(name="💾 مساحة مُستردة / Space reclaimed",
                    value=f"**{_fmt_size(s['bytes_reclaimed'])}**", inline=False)
    else:
        gid = interaction.guild.id
        s = await asyncio.to_thread(storage.dedup_attachments, gid)
        e = discord.Embed(title="🧹 توفير المساحة / Dedup complete", color=0x57F287)
        e.add_field(name="📄 ملفات قبل / Files before",
                    value=f"{s['files_before']:,}", inline=True)
        e.add_field(name="📄 ملفات بعد / Files after",
                    value=f"{s['files_after']:,}", inline=True)
        e.add_field(name="🔑 ملفات فريدة / Unique files",
                    value=f"{s['unique']:,}", inline=True)
        e.add_field(name="💾 مساحة مُستردة / Space reclaimed",
                    value=f"**{_fmt_size(s['bytes_reclaimed'])}**", inline=False)
    icon = _icon_url()
    if icon:
        e.set_thumbnail(url=icon)
    e.set_footer(text="المرفقات المكررة تُخزَّن مرة واحدة / duplicate attachments now stored once")
    await interaction.followup.send(embed=e)


# --------------------------------------------------------------------------- #
#  /report — copyable plain-text status (tap the code block to copy & paste here)
# --------------------------------------------------------------------------- #
# Full command catalog — single source of truth for /help and /copy so the two
# never drift. (name, args, bilingual description)
COMMANDS = [
    ("/backup", "", "نسخة كاملة للسيرفر / full server backup"),
    ("/backup_channel", "<channel>", "روم واحد فقط / one channel only"),
    ("/restore", "<link|source> [target]", "استرجاع سيرفر من نسخة / rebuild a server from a backup"),
    ("/status", "", "معلومات آخر نسخة / last backup info"),
    ("/stats", "", "إحصائيات مباشرة / live storage stats"),
    ("/report", "", "تقرير نصي ينسخ بضغطة / copyable text report"),
    ("/copy", "", "انسخ كل رسائل البوت / copy ALL bot info as text"),
    ("/dedup", "[all_servers]", "توفير مساحة بحذف المكرر / reclaim space (dedup)"),
    ("/download", "", "حمّل آخر .zip / fetch the latest archive"),
    ("/schedule", "<hours>", "نسخ تلقائي كل ساعات / auto-backup every N hours"),
    ("/search", "<query>", "ابحث في الرسائل المؤرشفة / search archived messages"),
    ("/clear", "[channel] [amount] [all_channels]", "امسح رسائل روم أو كل السيرفر / clear a channel (or all channels)"),
    ("/unban_all", "", "فك الحظر عن كل المحظورين ليرجعوا / unban everyone so they can rejoin"),
    ("/msg", "<message>", "أرسل رسالة خاصة لكل الأعضاء / DM a message to every member"),
    ("/room", "<open|close> [channel] [all_channels]", "افتح أو اقفل روم للجميع / open or lock a channel"),
    ("/openvoice", "[channel] [all_channels]", "افتح روم صوتي للجميع / open a voice channel for everyone"),
    ("/help", "", "هذه القائمة / this command list"),
]


def _commands_text() -> str:
    """Plain-text list of every command — copyable (used by /help and /copy)."""
    rows = [f"{n} {a}".strip() + f"  —  {d}" for n, a, d in COMMANDS]
    return "BackUp Bot — commands:\n" + "\n".join(rows)


async def _report_lines(g: discord.Guild) -> list:
    """Build the copyable status report for a guild (shared by /report + /copy)."""
    me = g.me
    admin = bool(me and me.guild_permissions.administrator)
    text_chs = [c for c in g.channels if isinstance(c, discord.TextChannel)]
    readable = sum(1 for c in text_chs
                   if me and c.permissions_for(me).read_message_history)
    unreadable = len(text_chs) - readable
    conn = await asyncio.to_thread(storage.open_db, g.id)
    run = await asyncio.to_thread(storage.latest_run, conn)

    def _totals():
        m = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        a = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
        return m, a
    total_msgs, total_atts = await asyncio.to_thread(_totals)
    await asyncio.to_thread(conn.close)
    size = await asyncio.to_thread(storage.dir_size, storage.guild_dir(g.id))
    files = await asyncio.to_thread(storage.guild_file_count, g.id)
    link = _latest_link(g.id) or "(no public domain set)"

    lines = [
        "=== BackUp Bot report ===",
        f"server          : {g.name} ({g.id})",
        f"bot_admin       : {'YES' if admin else 'NO  <-- GRANT ADMINISTRATOR'}",
        f"channels_read   : {readable}/{len(text_chs)} text channels"
        + (f"   ({unreadable} BLOCKED -> backup incomplete)" if unreadable else "   (full access)"),
        f"TOTAL messages  : {total_msgs:,}   <-- everything backed up",
        f"TOTAL attachments: {total_atts:,}",
    ]
    if run:
        lines += [
            f"last_backup_at  : {run.get('ended_at') or run.get('started_at') or '-'}",
            f"last_run_added  : +{run.get('messages', 0)} msgs (incremental)",
        ]
        integ = _integrity_of(run)
        if integ:
            lines.append(
                f"integrity_score : {integ.get('score', 0)}%   "
                f"(channels {integ.get('channels_read',0)}/{integ.get('channels_total',0)}, "
                f"files {integ.get('attachments_stored',0)}/{integ.get('attachments_total',0)})")
            for gap in _integrity_gaps(integ):
                lines.append(f"  - {gap}")
        if run.get("error"):
            lines.append(f"last_error      : {run['error']}")
    else:
        lines.append("last_backup     : NONE - run /backup first")
    lines += [
        f"data_files     : {files}",
        f"on_disk        : {_fmt_size(size)}",
        f"download_link  : {link}",
    ]
    return lines


@tree.command(name="report",
              description="تقرير نصي للنسخ بضغطة / copyable text report (tap to copy)")
async def report_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("server-only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    body = "\n".join(await _report_lines(interaction.guild))
    # Fenced code block → Discord shows a one-tap "Copy" on the whole block.
    await interaction.followup.send(
        "📋 انسخ كل هذا وارسله / tap & copy all of this:\n```\n" + body + "\n```")


@tree.command(name="copy",
              description="انسخ كل معلومات البوت كنص / copy ALL the bot's info as text")
async def copy_cmd(interaction: discord.Interaction):
    """One tap-to-copy block with EVERYTHING: full command list + (in a server)
    the complete status report. Solves 'I can't copy the bot's embed messages' —
    embeds aren't selectable on mobile, but a fenced code block has a Copy button."""
    await interaction.response.defer(ephemeral=True, thinking=True)
    parts = [_commands_text()]
    if interaction.guild:
        parts.append("\n" + "\n".join(await _report_lines(interaction.guild)))
    body = "\n".join(parts)
    # Discord messages cap at 2000 chars — chunk the code block if needed.
    chunks = _chunk_for_codeblock(body)
    first = True
    for ch in chunks:
        content = ("📋 انسخ كل شيء / tap & copy everything:\n" if first else "") \
            + "```\n" + ch + "\n```"
        if first:
            await interaction.followup.send(content, ephemeral=True)
            first = False
        else:
            await interaction.followup.send(content, ephemeral=True)


def _chunk_for_codeblock(text: str, limit: int = 1800) -> list:
    """Split text on line boundaries so each piece fits in a fenced code block
    under Discord's 2000-char message cap."""
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            if cur:
                out.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        out.append(cur)
    return out or [""]


# --------------------------------------------------------------------------- #
#  /download
# --------------------------------------------------------------------------- #
@tree.command(name="download",
              description="حمّل آخر نسخة / Download the latest .zip snapshot")
async def download_cmd(interaction: discord.Interaction):
    if not interaction.guild or not _admin_only(interaction):
        return await interaction.response.send_message(
            "⛔ Manage Server required.", ephemeral=True)
    bdir = storage.backups_dir(interaction.guild.id)
    zips = sorted(
        (os.path.join(bdir, f) for f in os.listdir(bdir) if f.endswith(".zip")),
        key=os.path.getmtime, reverse=True)
    if not zips:
        return await interaction.response.send_message(
            "ماكو .zip بعد — جرّب /backup أوّل / no archive yet — run /backup first.",
            ephemeral=True)
    path = zips[0]
    size = os.path.getsize(path)
    # Each server has its own private link, unique to this guild.
    link = _latest_link(interaction.guild.id)
    link_line = (f"\n🔗 رابط سيرفرك الخاص / your server's private link:\n{link}"
                 if link else "")
    # Discord limit for a normal bot upload is 25 MB; nitro-boosted servers
    # raise it. Past that, the private link is the only way out.
    if size > 25 * 1024 * 1024:
        if link:
            return await interaction.response.send_message(
                f"📦 الأرشيف كبير ({_fmt_size(size)}) — حمّله من هنا (رابط خاص بسيرفرك):"
                f"{link_line}\n⬇️ Large archive — download via this private link.",
                ephemeral=True)
        return await interaction.response.send_message(
            f"📦 الأرشيف كبير ({_fmt_size(size)}) — حمّله من السيرفر:\n"
            f"`{path}`\nfile too large for direct upload ({_fmt_size(size)}).",
            ephemeral=True)
    await interaction.response.send_message(
        content="📦 آخر نسخة / latest archive:" + link_line,
        file=discord.File(path), ephemeral=True)


# --------------------------------------------------------------------------- #
#  /restore
# --------------------------------------------------------------------------- #
def _restore_embed(p, guild_name: str) -> discord.Embed:
    if p.done and not p.error:
        color, title = 0x57f287, "✅ تمّت الاستعادة / Restore complete"
    elif p.error:
        color, title = 0xe8001c, "❌ فشل / Restore failed"
    else:
        color, title = 0x5865f2, "♻️ جارٍ الاستعادة… / Restoring…"
    e = discord.Embed(title=f"{title}", description=f"**{guild_name}**", color=color)
    e.add_field(name="الرولات / Roles", value=str(p.roles))
    e.add_field(name="التصنيفات / Categories", value=str(p.categories))
    e.add_field(name="الرومات / Channels", value=str(p.channels))
    e.add_field(name="الإيموجي / Emojis", value=str(p.emojis))
    e.add_field(name="الرسائل / Messages", value=str(p.messages))
    e.add_field(name="المرحلة / Stage", value=p.stage)
    if p.error:
        e.add_field(name="خطأ / Error", value=p.error[:1000], inline=False)
    return e


@tree.command(name="restore",
              description="استعد سيرفر من نسخة / Rebuild a server from a backup")
@app_commands.describe(
    file="ارفع ملف نسخة .zip مباشرة / upload a backup .zip file directly",
    link="أو رابط ملف .zip للنسخة / OR a backup .zip download link",
    source="أو ID سيرفر نسخة محفوظة / OR a saved backup's guild id (blank = here)",
    target="ID السيرفر الوجهة (فاضي = هنا) / target guild id (blank = here)",
    messages="استرجاع الرسائل أيضاً؟ بطيء / replay messages too (slow)")
async def restore_cmd(interaction: discord.Interaction,
                      file: Optional[discord.Attachment] = None,
                      link: Optional[str] = None,
                      source: Optional[str] = None,
                      target: Optional[str] = None,
                      messages: bool = True):
    # An uploaded .zip is just a link — Discord hosts it at a CDN URL the restore
    # engine can download exactly like a pasted link.
    if file is not None and not link:
        if not file.filename.lower().endswith(".zip"):
            return await interaction.response.send_message(
                "❌ لازم ملف .zip / the uploaded file must be a .zip backup.",
                ephemeral=True)
        link = file.url
    if not interaction.guild or not _admin_only(interaction):
        return await interaction.response.send_message(
            "⛔ Manage Server required.", ephemeral=True)
    # Resolve the destination server (current, or another the bot is in).
    if target:
        try:
            target_guild = bot.get_guild(int(target))
        except ValueError:
            target_guild = None
        if target_guild is None:
            return await interaction.response.send_message(
                "❌ البوت مو موجود بسيرفر بهالـID — ضيفه هناك أول.\n"
                "Bot isn't in a server with that ID — add it there first.", ephemeral=True)
    else:
        target_guild = interaction.guild
    # Source: a .zip link (downloaded + extracted) OR a saved backup on disk.
    source_gid = None
    if link:
        link = link.strip().replace(" ", "")   # tolerate accidental spaces in pasted URLs
        if not link.lower().startswith(("http://", "https://")):
            return await interaction.response.send_message(
                "❌ لازم رابط http(s) صحيح / link must be a valid http(s) URL.", ephemeral=True)
        url, local_gid = _resolve_restore_link(link)
        if local_gid is not None and storage.read_json(local_gid, "channels.json"):
            link, source_gid = None, local_gid   # bot-issued Drive link → restore the local copy
        else:
            link = url
    else:
        try:
            source_gid = int(source) if source else interaction.guild.id
        except ValueError:
            return await interaction.response.send_message(
                "ID غير صحيح / invalid source id.", ephemeral=True)
        if not storage.read_json(source_gid, "channels.json"):
            return await interaction.response.send_message(
                "ماكو نسخة محفوظة لهالـID — أو حط رابط .zip بدالها\n"
                "no saved backup for that id — or pass a .zip `link` instead.", ephemeral=True)
    me = target_guild.me
    if me is None or not me.guild_permissions.administrator:
        return await interaction.response.send_message(
            "❌ أحتاج صلاحية Administrator في السيرفر الوجهة.\n"
            "I need the Administrator permission in the target server.", ephemeral=True)

    await interaction.response.defer(thinking=True)
    holder: dict = {}
    cb = lambda pp: holder.__setitem__("p", pp)  # noqa: E731
    if link:
        task = asyncio.create_task(restore_engine.restore_from_zip(
            link, target_guild, with_messages=messages, progress=cb))
    else:
        task = asyncio.create_task(restore_engine.restore(
            source_gid, target_guild, with_messages=messages, progress=cb))
    while not task.done():
        await asyncio.sleep(3)
        if "p" in holder:
            try:
                await interaction.edit_original_response(
                    embed=_restore_embed(holder["p"], target_guild.name))
            except _TRANSIENT:
                pass
    p = await task
    try:
        await interaction.edit_original_response(
            embed=_restore_embed(p, target_guild.name))
    except _TRANSIENT:
        pass


# --------------------------------------------------------------------------- #
#  /schedule
# --------------------------------------------------------------------------- #
@tree.command(name="schedule",
              description="نسخ تلقائي كل N ساعة / Auto-backup every N hours (0=off)")
@app_commands.describe(hours="عدد الساعات / interval in hours, 0 disables")
async def schedule_cmd(interaction: discord.Interaction,
                        hours: app_commands.Range[int, 0, 168]):
    if not interaction.guild or not _admin_only(interaction):
        return await interaction.response.send_message(
            "⛔ Manage Server required.", ephemeral=True)
    if hours == 0:
        schedules.pop(interaction.guild.id, None)
        return await interaction.response.send_message(
            "🛑 ألغيت الجدول / auto-backup disabled.", ephemeral=True)
    first = interaction.guild.id not in schedules
    schedules[interaction.guild.id] = hours
    if first and not config.AUTO_BACKUP_HOURS:
        bot.loop.create_task(_auto_loop())
    await interaction.response.send_message(
        f"🗓️ مفعّل — كل **{hours}** ساعة / scheduled every **{hours}h**.",
        ephemeral=True)


# --------------------------------------------------------------------------- #
#  /search
# --------------------------------------------------------------------------- #
@tree.command(name="search",
              description="ابحث في النسخة المحفوظة / Search archived messages")
@app_commands.describe(query="الكلمة / search words")
async def search_cmd(interaction: discord.Interaction, query: str):
    if not interaction.guild:
        return
    conn = storage.open_db(interaction.guild.id)
    rows = conn.execute(
        """SELECT channel_name, author_name, content, created_at
           FROM messages
           WHERE content LIKE ?
           ORDER BY created_at DESC LIMIT 10""",
        (f"%{query}%",)).fetchall()
    conn.close()
    if not rows:
        return await interaction.response.send_message(
            f"ماكو نتيجة عن `{query}` / no hits for `{query}`.",
            ephemeral=True)
    lines = []
    for ch, who, txt, when in rows:
        snippet = (txt or "").replace("\n", " ")[:120]
        lines.append(f"`#{ch}` · **{who}** · {when[:10]}\n> {snippet}")
    embed = discord.Embed(title=f"🔍 {query}",
                          description="\n\n".join(lines),
                          color=0x5865F2)
    embed.set_footer(text=f"Top {len(rows)} matches")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------- #
#  /clear — wipe messages in a channel (with confirmation)
# --------------------------------------------------------------------------- #
async def _wipe_channel(ch: discord.TextChannel, reason: str
                        ) -> discord.TextChannel:
    """Full wipe of a channel: clone (keeps name/perms/position) + delete the
    original. Far faster than purging tens of thousands of messages and works
    on messages older than 14 days. Returns the fresh channel."""
    new_ch = await ch.clone(reason=reason)
    await new_ch.edit(position=ch.position)
    await ch.delete(reason=reason)
    return new_ch


class _ClearConfirm(discord.ui.View):
    """Yes/No confirmation for the destructive /clear command.

    Modes: a single `channel` (purge `amount`, or full wipe when amount is
    None), or `all_channels=True` to fully wipe every text channel in the
    guild."""

    def __init__(self, author_id: int, *,
                 channel: discord.TextChannel | None = None,
                 amount: int | None = None,
                 guild: discord.Guild | None = None,
                 all_channels: bool = False):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.channel = channel
        self.amount = amount          # None = full wipe of the channel
        self.guild = guild
        self.all_channels = all_channels

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "هذا الزر مو إلك / not your button.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="نعم احذف / Yes, clear",
                       style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction,
                      button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="⏳ يحذف / clearing…", view=self)
        reason = f"/clear by {interaction.user}"
        # Public banner posted in each cleared channel so ALL members see it.
        notice = (f"🗑️ تم مسح هذا الروم بواسطة {interaction.user.mention} / "
                  f"This channel was cleared by {interaction.user.mention}.")
        try:
            if self.all_channels:
                channels = [c for c in self.guild.text_channels
                            if c.permissions_for(self.guild.me).manage_channels]
                done, failed = 0, []
                for c in channels:
                    try:
                        new_ch = await _wipe_channel(c, reason)
                        await new_ch.send(notice)               # visible to all
                        done += 1
                    except Exception as e:                       # keep going
                        failed.append(f"{c.name} (`{e}`)")
                msg = (f"✅ انمسحت **{done}** روم بالكامل / "
                       f"fully cleared **{done}** channels.")
                if failed:
                    msg += "\n⚠️ تعذّر / skipped: " + ", ".join(failed[:10])
                await interaction.followup.send(msg, ephemeral=True)
            elif self.amount is not None:
                deleted = await self.channel.purge(limit=self.amount)
                await self.channel.send(                         # visible to all
                    f"🗑️ {interaction.user.mention} مسح **{len(deleted)}** "
                    f"رسالة / cleared **{len(deleted)}** messages.")
                await interaction.followup.send(
                    f"✅ انحذفت **{len(deleted)}** رسالة من "
                    f"{self.channel.mention} / "
                    f"deleted **{len(deleted)}** messages.", ephemeral=True)
            else:
                new_ch = await _wipe_channel(self.channel, reason)
                await new_ch.send(notice)                        # visible to all
                await interaction.followup.send(
                    f"✅ انمسح الروم بالكامل / channel fully cleared → "
                    f"{new_ch.mention}", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(
                "⛔ ماعندي صلاحية / I need **Manage Messages** "
                "(and **Manage Channels** for a full wipe).", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(
                f"💥 فشل / failed: `{e}`", ephemeral=True)
        self.stop()

    @discord.ui.button(label="إلغاء / Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="❎ تم الإلغاء / cancelled.", view=self)
        self.stop()


@tree.command(name="clear",
              description="امسح رسائل روم / Clear messages in a channel")
@app_commands.describe(
    channel="الروم (افتراضي: الحالي) / channel (default: current)",
    amount="عدد الرسائل، فاضي = الروم كامل / how many, blank = whole channel",
    all_channels="امسح كل الرومات بالسيرفر / wipe EVERY channel in the server")
async def clear_cmd(interaction: discord.Interaction,
                    channel: discord.TextChannel | None = None,
                    amount: app_commands.Range[int, 1, 1000] | None = None,
                    all_channels: bool = False):
    if not interaction.guild:
        return
    if not _admin_only(interaction):
        return await interaction.response.send_message(
            "⛔ Manage Server required.", ephemeral=True)

    if all_channels:
        n = len(interaction.guild.text_channels)
        return await interaction.response.send_message(
            f"🛑 راح تنمسح **كل** الرسائل في **{n}** روم بالسيرفر "
            f"(كل روم ينعاد إنشاؤه بآي دي جديد) / this wipes **ALL** messages "
            f"across **all {n}** text channels (each gets a new ID). "
            "**لا رجعة / no undo.**\n"
            "💡 سوّي **/backup** قبلها لو تريد نسخة / run **/backup** first.",
            view=_ClearConfirm(interaction.user.id, guild=interaction.guild,
                               all_channels=True),
            ephemeral=True)

    ch = channel or interaction.channel
    if not isinstance(ch, discord.TextChannel):
        return await interaction.response.send_message(
            "هذا الأمر للرومات النصية فقط / text channels only.", ephemeral=True)
    scope = (f"آخر **{amount}** رسالة / the last **{amount}** messages"
             if amount is not None
             else "**كل** الرسائل / **ALL** messages")
    warn = ("" if amount is not None
            else "\n⚠️ المسح الكامل يعيد إنشاء الروم (آي دي جديد) / "
                 "a full wipe re-creates the channel (new ID).")
    await interaction.response.send_message(
        f"🗑️ راح تنحذف {scope} من {ch.mention}.{warn}\n"
        "💡 سوّي **/backup_channel** قبل الحذف لو تريد نسخة / "
        "back it up first if you want a copy.",
        view=_ClearConfirm(interaction.user.id, channel=ch, amount=amount),
        ephemeral=True)


# --------------------------------------------------------------------------- #
#  /unban_all
# --------------------------------------------------------------------------- #
class _UnbanAllConfirm(discord.ui.View):
    """Yes/No confirmation for /unban_all — lifts every ban in the guild."""

    def __init__(self, author_id: int, guild: discord.Guild, bans: list):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.guild = guild
        self.bans = bans

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "هذا الزر مو إلك / not your button.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="نعم فك الحظر / Yes, unban all",
                       style=discord.ButtonStyle.danger, emoji="🔓")
    async def confirm(self, interaction: discord.Interaction,
                      button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"⏳ يفك الحظر عن **{len(self.bans)}** / "
                    f"unbanning **{len(self.bans)}**…", view=self)
        reason = f"/unban_all by {interaction.user}"
        done, failed = 0, 0
        for entry in self.bans:
            try:
                await self.guild.unban(entry.user, reason=reason)
                done += 1
            except discord.NotFound:
                done += 1          # already unbanned — count as success
            except Exception:
                failed += 1
        msg = (f"✅ تم فك الحظر عن **{done}** عضو، صاروا يقدرون يدخلون "
               f"السيرفر / unbanned **{done}** users — they can rejoin now.")
        if failed:
            msg += (f"\n⚠️ تعذّر فك الحظر عن **{failed}** / "
                    f"failed on **{failed}**.")
        await interaction.followup.send(msg, ephemeral=True)
        self.stop()

    @discord.ui.button(label="إلغاء / Cancel",
                       style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="❎ تم الإلغاء / cancelled.", view=self)
        self.stop()


@tree.command(
    name="unban_all",
    description="فك الحظر عن كل الأعضاء المحظورين / Unban every banned user")
async def unban_all_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        return
    if not _admin_only(interaction):
        return await interaction.response.send_message(
            "⛔ Manage Server required.", ephemeral=True)
    if not interaction.guild.me.guild_permissions.ban_members:
        return await interaction.response.send_message(
            "⛔ ماعندي صلاحية / I need the **Ban Members** permission.",
            ephemeral=True)

    # Paging the ban list can take a while — defer so we don't blow the
    # 3-second initial-response deadline, then prompt to confirm.
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        bans = [entry async for entry in interaction.guild.bans(limit=None)]
    except discord.Forbidden:
        return await interaction.followup.send(
            "⛔ ماعندي صلاحية / I need the **Ban Members** permission.",
            ephemeral=True)
    if not bans:
        return await interaction.followup.send(
            "✅ مافي أحد محظور بالسيرفر / there are no banned users.",
            ephemeral=True)

    await interaction.followup.send(
        f"🔓 راح يتفك الحظر عن **{len(bans)}** عضو محظور — بيقدرون يرجعون "
        f"يدخلون السيرفر / this will unban **{len(bans)}** banned users so "
        f"they can rejoin.\n**أكد / confirm:**",
        view=_UnbanAllConfirm(interaction.user.id, interaction.guild, bans),
        ephemeral=True)


# --------------------------------------------------------------------------- #
#  /msg  — DM a message to every member (admin broadcast)
# --------------------------------------------------------------------------- #
class _BroadcastConfirm(discord.ui.View):
    """Yes/No confirmation for /msg — DMs the message to every human member."""

    def __init__(self, author_id: int, members: list, text: str):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.members = members
        self.text = text

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "هذا الزر مو إلك / not your button.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="نعم أرسل للكل / Yes, DM everyone",
                       style=discord.ButtonStyle.danger, emoji="📨")
    async def confirm(self, interaction: discord.Interaction,
                      button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        total = len(self.members)
        await interaction.response.edit_message(
            content=f"⏳ يرسل الرسالة الخاصة لـ **{total}** عضو / "
                    f"DMing **{total}** members…", view=self)

        sent = failed = 0
        # Discord rate-limits DMs and flags bursty mass-DMs as spam. discord.py
        # already auto-sleeps on 429s; the extra delay keeps us well under the
        # limit so a big server doesn't trip abuse detection.
        for i, member in enumerate(self.members, 1):
            try:
                await member.send(self.text)
                sent += 1
            except discord.Forbidden:
                failed += 1          # DMs closed, or the member blocked the bot
            except discord.HTTPException:
                failed += 1
            await asyncio.sleep(1.0)
            # Refresh the progress note every 25 sends (avoid edit spam).
            if i % 25 == 0:
                try:
                    await interaction.edit_original_response(
                        content=f"⏳ {i}/{total} … ✅ {sent}  ⚠️ {failed}",
                        view=self)
                except discord.HTTPException:
                    pass

        msg = (f"✅ تم الإرسال لـ **{sent}** عضو / delivered to **{sent}** "
               f"members.")
        if failed:
            msg += (f"\n⚠️ تعذّر الإرسال لـ **{failed}** (مغلقين الخاص أو "
                    f"حاظرين البوت) / couldn't DM **{failed}** (DMs closed or "
                    f"blocked the bot).")
        try:
            await interaction.followup.send(msg, ephemeral=True)
        except discord.HTTPException:
            pass                     # interaction token may expire on huge sends
        self.stop()

    @discord.ui.button(label="إلغاء / Cancel",
                       style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="❎ تم الإلغاء / cancelled.", view=self)
        self.stop()


@tree.command(
    name="msg",
    description="أرسل رسالة خاصة لكل أعضاء السيرفر / DM a message to every member")
@app_commands.describe(
    message="نص الرسالة اللي يوصل خاص لكل عضو / the message to DM everyone")
async def msg_cmd(interaction: discord.Interaction, message: str):
    if not interaction.guild:
        return
    if not _admin_only(interaction):
        return await interaction.response.send_message(
            "⛔ Manage Server required.", ephemeral=True)

    await interaction.response.defer(ephemeral=True, thinking=True)

    # Recipients = every human member (bots can't be DMed). The cached member
    # list needs the Members intent, which this bot already enables; fall back
    # to fetching if the cache is cold.
    guild = interaction.guild
    members = [m for m in guild.members if not m.bot]
    if not members:
        try:
            members = [m async for m in guild.fetch_members(limit=None)
                       if not m.bot]
        except discord.HTTPException:
            members = []
    if not members:
        return await interaction.followup.send(
            "⚠️ ماقدرت أجيب قائمة الأعضاء / couldn't load the member list.",
            ephemeral=True)

    preview = message if len(message) <= 1500 else message[:1500] + "…"
    eta = len(members)               # ~1 second per DM
    await interaction.followup.send(
        f"📨 راح تنرسل هذي الرسالة كـ **رسالة خاصة (DM)** لـ "
        f"**{len(members)}** عضو (~{eta} ثانية / ~{eta}s):\n\n"
        f">>> {preview}\n\n**أكد / confirm:**",
        view=_BroadcastConfirm(interaction.user.id, members, message),
        ephemeral=True)


# --------------------------------------------------------------------------- #
#  /room  — open (unlock) or close (lock) a channel for @everyone
# --------------------------------------------------------------------------- #
@tree.command(
    name="room",
    description="افتح أو اقفل روم للجميع / Open or lock a channel for everyone")
@app_commands.describe(
    action="افتح أو اقفل / open or close",
    channel="الروم (افتراضي: الحالي) / channel (default: current)",
    all_channels="طبّقها على كل الرومات بالسيرفر / apply to EVERY channel")
@app_commands.choices(action=[
    app_commands.Choice(name="🔓 افتح / open", value="open"),
    app_commands.Choice(name="🔒 اقفل / close", value="close"),
])
async def room_cmd(interaction: discord.Interaction,
                   action: app_commands.Choice[str],
                   channel: discord.TextChannel | None = None,
                   all_channels: bool = False):
    if not interaction.guild:
        return
    if not _admin_only(interaction):
        return await interaction.response.send_message(
            "⛔ Manage Server required.", ephemeral=True)
    # Editing a channel's permission overwrites needs Manage Roles.
    if not interaction.guild.me.guild_permissions.manage_roles:
        return await interaction.response.send_message(
            "⛔ ماعندي صلاحية / I need the **Manage Roles** permission.",
            ephemeral=True)

    opening = action.value == "open"
    everyone = interaction.guild.default_role
    reason = f"/room {action.value} by {interaction.user}"

    async def _apply(ch: discord.TextChannel) -> bool:
        try:
            ow = ch.overwrites_for(everyone)
            ow.send_messages = True if opening else False
            await ch.set_permissions(everyone, overwrite=ow, reason=reason)
            return True
        except discord.HTTPException:
            return False

    if all_channels:
        await interaction.response.defer(ephemeral=True, thinking=True)
        done = failed = 0
        for ch in interaction.guild.text_channels:
            if await _apply(ch):
                done += 1
            else:
                failed += 1
            await asyncio.sleep(0.3)        # gentle on the rate limit
        head = ("🔓 تم فتح" if opening else "🔒 تم قفل")
        tail = ("صار الكل يقدر يكتب فيها / opened for @everyone"
                if opening else
                "صار ما حدا يكتب غير الإدارة / locked — only staff can post")
        msg = f"{head} **{done}** روم — {tail}."
        if failed:
            msg += f"\n⚠️ تعذّر على **{failed}** / failed on **{failed}**."
        return await interaction.followup.send(msg, ephemeral=True)

    ch = channel or interaction.channel
    if not isinstance(ch, discord.TextChannel):
        return await interaction.response.send_message(
            "هذا الأمر للرومات النصية فقط / text channels only.", ephemeral=True)
    if not await _apply(ch):
        return await interaction.response.send_message(
            f"💥 ماقدرت أعدّل {ch.mention} / couldn't update it.", ephemeral=True)
    if opening:
        await interaction.response.send_message(
            f"🔓 تم فتح {ch.mention} — الكل يقدر يكتب فيها الآن / "
            f"opened for @everyone.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"🔒 تم قفل {ch.mention} — ما حدا يقدر يكتب غير الإدارة / "
            f"locked — only staff can post now.", ephemeral=True)


# --------------------------------------------------------------------------- #
#  /openvoice  — open (unlock) a voice channel for @everyone
# --------------------------------------------------------------------------- #
@tree.command(
    name="openvoice",
    description="افتح روم صوتي للجميع / Open a voice channel for everyone")
@app_commands.describe(
    channel="الروم الصوتي (افتراضي: اللي أنت فيه) / voice channel (default: yours)",
    all_channels="طبّقها على كل الرومات الصوتية / apply to EVERY voice channel")
async def openvoice_cmd(interaction: discord.Interaction,
                        channel: discord.VoiceChannel | None = None,
                        all_channels: bool = False):
    if not interaction.guild:
        return
    if not _admin_only(interaction):
        return await interaction.response.send_message(
            "⛔ Manage Server required.", ephemeral=True)
    # Editing a channel's permission overwrites needs Manage Roles.
    if not interaction.guild.me.guild_permissions.manage_roles:
        return await interaction.response.send_message(
            "⛔ ماعندي صلاحية / I need the **Manage Roles** permission.",
            ephemeral=True)

    everyone = interaction.guild.default_role
    reason = f"/openvoice by {interaction.user}"

    async def _apply(ch: discord.VoiceChannel) -> bool:
        try:
            ow = ch.overwrites_for(everyone)
            ow.connect = True
            ow.speak = True
            ow.use_voice_activation = True
            await ch.set_permissions(everyone, overwrite=ow, reason=reason)
            return True
        except discord.HTTPException:
            return False

    if all_channels:
        await interaction.response.defer(ephemeral=True, thinking=True)
        done = failed = 0
        for ch in interaction.guild.voice_channels:
            if await _apply(ch):
                done += 1
            else:
                failed += 1
            await asyncio.sleep(0.3)        # gentle on the rate limit
        msg = (f"🔊 تم فتح **{done}** روم صوتي — الكل يقدر يدخل ويتكلم / "
               f"opened **{done}** voice channels for @everyone.")
        if failed:
            msg += f"\n⚠️ تعذّر على **{failed}** / failed on **{failed}**."
        return await interaction.followup.send(msg, ephemeral=True)

    ch = channel
    # Typed inside a voice channel's own text chat → target that voice room.
    if ch is None and isinstance(interaction.channel, discord.VoiceChannel):
        ch = interaction.channel
    if ch is None and isinstance(interaction.user, discord.Member) \
            and interaction.user.voice and isinstance(
                interaction.user.voice.channel, discord.VoiceChannel):
        ch = interaction.user.voice.channel
    if ch is None:
        return await interaction.response.send_message(
            "حدّد روم صوتي أو ادخل واحد أولاً / pick a voice channel "
            "or join one first.", ephemeral=True)
    if not await _apply(ch):
        return await interaction.response.send_message(
            f"💥 ماقدرت أعدّل {ch.mention} / couldn't update it.", ephemeral=True)
    await interaction.response.send_message(
        f"🔊 تم فتح {ch.mention} — الكل يقدر يدخل ويتكلم الآن / "
        f"opened for @everyone.", ephemeral=True)


# --------------------------------------------------------------------------- #
#  /help
# --------------------------------------------------------------------------- #
@tree.command(name="help", description="الأوامر / Commands")
async def help_cmd(interaction: discord.Interaction):
    desc = "\n".join(f"**{n}** `{a}` — {d}" if a else f"**{n}** — {d}"
                     for n, a, d in COMMANDS)
    embed = discord.Embed(
        title="💾 BackUp Bot — الأوامر / Commands",
        description=(
            desc + "\n\n"
            "Backups capture **everything**: channels, roles, members "
            "(incl. admins), every message, embeds, reactions, mentions, "
            "and downloaded attachments.\n\n"
            "💡 **/copy** = انسخ كل هذا كنص / copy all of this as text.\n\n"
            "👨‍💻 Programmed by **[@KhaledQ84Ever](https://x.com/KhaledQ84Ever)** · "
            "🌐 [discordbackupbot.vercel.app](https://discordbackupbot.vercel.app)"
        ),
        color=0x5865F2,
    )
    embed.set_footer(text="Programmed by @KhaledQ84Ever · x.com/KhaledQ84Ever")
    # Embeds aren't selectable on mobile — also send a tap-to-copy code block so
    # the full command list can be copied.
    await interaction.response.send_message(
        embed=embed,
        content="📋 انسخ الأوامر / tap & copy:\n```\n" + _commands_text() + "\n```",
        ephemeral=True)


if __name__ == "__main__":
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set — see .env.example")
    os.makedirs(config.DATA_DIR, exist_ok=True)
    bot.run(config.DISCORD_TOKEN)
