"""Restore engine — rebuild a server from a saved backup.

Discord limits the truth here:
  • Channels, categories, roles (+ permissions & overwrites) and emojis can be
    recreated faithfully.
  • Messages are *replayed* through per-channel webhooks that mimic each original
    author's name + avatar (a visual replica — not real authorship). Attachments
    are re-uploaded from the saved files. Reactions / edit-history / pins can't
    be reproduced.
  • Members can't be restored (you can't force users to rejoin).

Usage: await restore(source_gid, target_guild, with_messages=..., progress=cb)
"""
import asyncio
import json
import logging
import os
import random
import shutil
import zipfile
from typing import Callable, Optional

import discord

import storage

log = logging.getLogger("restore")

# How many messages per channel to replay (0 = all). Webhook sends are slow and
# rate-limited, so a huge server (100k+ msgs) would take many hours — cap by default.
MSG_LIMIT = int(os.getenv("RESTORE_MSG_LIMIT", "300"))
SEND_DELAY = float(os.getenv("RESTORE_SEND_DELAY", "0.15"))  # 0 = fastest (discord.py self-throttles)

_TEXT_TYPES = ("text", "news")
_VOICE_TYPES = ("voice", "stage_voice")


class RProgress:
    """Mutable counters for a live progress embed."""
    def __init__(self):
        self.roles = 0
        self.categories = 0
        self.channels = 0
        self.emojis = 0
        self.messages = 0
        self.stage = "starting"
        self.done = False
        self.error: Optional[str] = None


def _overwrites(chan_row: dict, role_map: dict, guild: discord.Guild) -> dict:
    """Translate saved permission overwrites onto the new roles (members skipped)."""
    ow = {}
    for o in chan_row.get("overwrites", []):
        if o.get("target_type") != "role":
            continue
        role = role_map.get(o["target_id"])
        if role is None:
            continue
        ow[role] = discord.PermissionOverwrite.from_pair(
            discord.Permissions(o.get("allow", 0)),
            discord.Permissions(o.get("deny", 0)))
    return ow


async def restore(source_gid: int, guild: discord.Guild, *,
                  with_messages: bool, progress: Callable[[RProgress], None] = None,
                  ) -> RProgress:
    p = RProgress()

    def tick():
        if progress:
            try:
                progress(p)
            except Exception:
                pass

    try:
        roles = storage.read_json(source_gid, "roles.json", []) or []
        channels = storage.read_json(source_gid, "channels.json", []) or []
        emojis = storage.read_json(source_gid, "emojis.json", []) or []
        members = {m["id"]: m for m in (storage.read_json(source_gid, "members.json", []) or [])}

        # ---- 1. Roles (low position first; skip @everyone + managed/bot roles) ----
        p.stage = "roles"; tick()
        role_map: dict[int, discord.Role] = {}
        existing_roles = {r.name: r for r in guild.roles}
        everyone = next((r for r in roles if r["name"] == "@everyone"), None)
        if everyone:
            role_map[everyone["id"]] = guild.default_role
        for r in sorted(roles, key=lambda x: x.get("position", 0)):
            if r["name"] == "@everyone" or r.get("managed"):
                continue
            if r["name"] in existing_roles:           # already restored — reuse & skip
                role_map[r["id"]] = existing_roles[r["name"]]
                continue
            try:
                nr = await guild.create_role(
                    name=r["name"], colour=discord.Colour(r.get("color", 0)),
                    hoist=r.get("hoist", False), mentionable=r.get("mentionable", False),
                    permissions=discord.Permissions(r.get("permissions", 0)),
                    reason="BackUp Bot restore")
                role_map[r["id"]] = nr
                existing_roles[r["name"]] = nr
                p.roles += 1; tick()
            except discord.HTTPException as e:
                log.warning("role %s failed: %s", r.get("name"), e)

        # ---- 2. Categories, then channels under them ----
        p.stage = "channels"; tick()
        cat_map: dict[int, discord.CategoryChannel] = {}
        existing_cats = {c.name: c for c in guild.categories}
        for c in sorted([c for c in channels if "categor" in c["type"]],
                        key=lambda x: x.get("position", 0)):
            if c["name"] in existing_cats:            # already there — reuse & skip
                cat_map[c["id"]] = existing_cats[c["name"]]
                continue
            try:
                nc = await guild.create_category(
                    c["name"], overwrites=_overwrites(c, role_map, guild),
                    reason="BackUp Bot restore")
                cat_map[c["id"]] = nc
                existing_cats[c["name"]] = nc
                p.categories += 1; tick()
            except discord.HTTPException as e:
                log.warning("category %s failed: %s", c.get("name"), e)

        chan_map: dict[int, discord.abc.GuildChannel] = {}
        created_channel_ids: set = set()             # only NEW channels get message replay
        existing_chans = {c.name: c for c in guild.channels
                          if not isinstance(c, discord.CategoryChannel)}
        for c in sorted([c for c in channels if "categor" not in c["type"]],
                        key=lambda x: x.get("position", 0)):
            if c["name"] in existing_chans:           # already restored — reuse, skip replay
                chan_map[c["id"]] = existing_chans[c["name"]]
                continue
            parent = cat_map.get(c.get("category_id"))
            ow = _overwrites(c, role_map, guild)
            ctype = c["type"]
            try:
                if any(t in ctype for t in _VOICE_TYPES):
                    nc = await guild.create_voice_channel(
                        c["name"], category=parent, overwrites=ow,
                        bitrate=int(min(int(c.get("bitrate", 64000)), guild.bitrate_limit)),
                        user_limit=int(c.get("user_limit", 0)),
                        reason="BackUp Bot restore")
                elif "forum" in ctype:
                    continue  # forum restore is unsupported; skip cleanly
                else:
                    nc = await guild.create_text_channel(
                        c["name"], category=parent, overwrites=ow,
                        topic=c.get("topic"), nsfw=bool(c.get("nsfw", False)),
                        slowmode_delay=int(c.get("slowmode_delay", 0)),
                        reason="BackUp Bot restore")
                chan_map[c["id"]] = nc
                created_channel_ids.add(c["id"])
                existing_chans[c["name"]] = nc
                p.channels += 1; tick()
            except discord.HTTPException as e:
                log.warning("channel %s failed: %s", c.get("name"), e)

        # ---- 3. Emojis (best-effort: fetch saved URL, re-upload) ----
        p.stage = "emojis"; tick()
        existing_emoji = {em.name for em in guild.emojis}
        for e in emojis:
            url = e.get("url")
            if not url or e["name"] in existing_emoji:   # already there — skip
                continue
            try:
                async with config_session() as sess:
                    async with sess.get(url) as resp:
                        if resp.status != 200:
                            continue
                        img = await resp.read()
                await guild.create_custom_emoji(name=e["name"], image=img,
                                                 reason="BackUp Bot restore")
                existing_emoji.add(e["name"])
                p.emojis += 1; tick()
            except discord.HTTPException as e2:
                log.warning("emoji %s failed: %s", e.get("name"), e2)
            except Exception:
                pass

        # ---- 4. Messages via webhooks — ONLY into newly-created channels ----
        # (re-restore skips channels that already exist → no duplicates, much faster)
        if with_messages and created_channel_ids:
            p.stage = "messages"; tick()
            conn = storage.open_db(source_gid)
            limit_sql = f"LIMIT {MSG_LIMIT}" if MSG_LIMIT > 0 else ""
            for old_cid in created_channel_ids:
                new_ch = chan_map.get(old_cid)
                if not isinstance(new_ch, discord.TextChannel):
                    continue
                try:
                    wh = await new_ch.create_webhook(name="BackUp Restore")
                except discord.HTTPException:
                    continue
                rows = conn.execute(
                    f"""SELECT id, author_name, author_id, content, embeds_json
                        FROM messages WHERE channel_id = ?
                        ORDER BY id ASC {limit_sql}""", (old_cid,)).fetchall()
                for mid, aname, aid, content, embeds_json in rows:
                    files = _load_attachments(conn, mid, source_gid)
                    embeds = _load_embeds(embeds_json)
                    if not content and not files and not embeds:
                        continue
                    avatar = (members.get(aid) or {}).get("avatar_url")
                    try:
                        await wh.send(
                            content=(content or "")[:2000] or None,
                            username=(aname or "user")[:80],
                            avatar_url=avatar,
                            files=files, embeds=embeds[:10],
                            allowed_mentions=discord.AllowedMentions.none())
                        p.messages += 1
                        if p.messages % 20 == 0:
                            tick()
                    except discord.HTTPException as e:
                        log.warning("msg replay failed: %s", e)
                    if SEND_DELAY:               # 0 = max speed (discord.py self-throttles)
                        await asyncio.sleep(SEND_DELAY)
                try:
                    await wh.delete()
                except discord.HTTPException:
                    pass
            conn.close()

        p.stage = "done"; p.done = True; tick()
    except Exception as e:  # noqa: BLE001
        p.error = str(e)[:300]
        p.done = True
        log.exception("restore failed")
        tick()
    return p


def _load_embeds(embeds_json: Optional[str]) -> list:
    if not embeds_json:
        return []
    try:
        data = json.loads(embeds_json)
        return [discord.Embed.from_dict(d) for d in data if isinstance(d, dict)]
    except Exception:
        return []


def _load_attachments(conn, message_id: int, source_gid: int) -> list:
    files = []
    try:
        rows = conn.execute(
            "SELECT filename, local_path FROM attachments WHERE message_id = ?",
            (message_id,)).fetchall()
    except Exception:
        return files
    adir = storage.attachments_dir(source_gid)
    for filename, local_path in rows:
        # Try the stored path, then fall back to <source>/attachments/<basename>
        # (covers backups restored from a downloaded .zip, where absolute paths differ).
        candidates = []
        if local_path:
            candidates.append(local_path)
            candidates.append(os.path.join(adir, os.path.basename(local_path)))
        path = next((c for c in candidates if c and os.path.isfile(c)), None)
        if path and os.path.getsize(path) < 8 * 1024 * 1024:
            try:
                files.append(discord.File(path, filename=filename or os.path.basename(path)))
            except Exception:
                pass
        if len(files) >= 10:
            break
    return files


def config_session():
    import aiohttp
    return aiohttp.ClientSession()


def _extract_zip(zpath: str, dest: str, base: str) -> None:
    """Synchronous zip extraction (run via asyncio.to_thread, with zip-slip guard)."""
    with zipfile.ZipFile(zpath) as z:
        for member in z.namelist():
            tgt = os.path.normpath(os.path.join(dest, member))
            if tgt != base and not tgt.startswith(base + os.sep):
                continue  # zip-slip guard
            if member.endswith("/"):
                os.makedirs(tgt, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(tgt), exist_ok=True)
            with z.open(member) as src, open(tgt, "wb") as out:
                shutil.copyfileobj(src, out)


async def restore_from_zip(url: str, guild: discord.Guild, *,
                           with_messages: bool,
                           progress: Callable[["RProgress"], None] = None,
                           ) -> "RProgress":
    """Download a backup .zip from a URL, extract it, and restore from it.

    Lets the user just paste a download link to /restore — the bot does the rest.
    """
    import aiohttp

    p = RProgress()
    p.stage = "downloading"
    if progress:
        progress(p)

    temp_gid = random.randint(10 ** 17, 10 ** 18)   # throwaway source id under DATA_DIR
    dest = storage.guild_dir(temp_gid)              # creates DATA_DIR/<temp_gid>/
    base = os.path.normpath(dest)
    zpath = dest.rstrip("/") + ".zip"
    try:
        timeout = aiohttp.ClientTimeout(total=1800)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url) as r:
                if r.status != 200:
                    p.error = f"download failed: HTTP {r.status}"
                    p.done = True
                    if progress:
                        progress(p)
                    return p
                with open(zpath, "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 16):
                        f.write(chunk)

        p.stage = "extracting"
        if progress:
            progress(p)
        # Extracting a ~1GB zip is heavy + synchronous — run it OFF the event loop
        # so the bot doesn't freeze / drop its heartbeat during a restore.
        await asyncio.to_thread(_extract_zip, zpath, dest, base)

        if not storage.read_json(temp_gid, "channels.json"):
            p.error = "that .zip isn't a valid backup (no channels.json inside)"
            p.done = True
            if progress:
                progress(p)
            return p

        return await restore(temp_gid, guild, with_messages=with_messages,
                             progress=progress)
    except Exception as e:  # noqa: BLE001
        p.error = str(e)[:300]
        p.done = True
        log.exception("restore_from_zip failed")
        if progress:
            progress(p)
        return p
    finally:
        try:
            os.remove(zpath)
        except OSError:
            pass
        # Deleting a ~1GB extracted folder can block too — do it off the loop.
        await asyncio.to_thread(shutil.rmtree, dest, True)
