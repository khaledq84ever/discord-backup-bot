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
import logging
import os
import time
from typing import Optional

import discord
from discord import app_commands
from aiohttp import web

import backup
import config
import restore as restore_engine
import storage

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(name)s  %(message)s")
log = logging.getLogger("bot")

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
# Stable, unguessable secret. Set DOWNLOAD_SECRET to override; otherwise derived
# from the bot token so links survive restarts without extra config.
DOWNLOAD_SECRET = (os.getenv("DOWNLOAD_SECRET", "").strip()
                   or hashlib.sha256((config.DISCORD_TOKEN or "x").encode()).hexdigest()[:24])
_web_started = False


def _download_link(guild_id: int, filename: str) -> Optional[str]:
    if not _PUBLIC_DOMAIN:
        return None
    return f"https://{_PUBLIC_DOMAIN}/dl/{DOWNLOAD_SECRET}/{guild_id}/{filename}"


async def _h_health(request):
    return web.Response(text="BackUp Bot — OK")


async def _h_latest(request):
    """Always serve the NEWEST .zip snapshot for a guild — a stable link."""
    if request.match_info["token"] != DOWNLOAD_SECRET:
        return web.Response(status=403, text="forbidden")
    gid = request.match_info["gid"]
    if not gid.isdigit():
        return web.Response(status=400, text="bad request")
    bdir = storage.backups_dir(int(gid))
    try:
        zips = [f for f in os.listdir(bdir) if f.endswith(".zip")]
    except OSError:
        zips = []
    if not zips:
        return web.Response(status=404, text="no backup yet — run /backup first")
    newest = max(zips, key=lambda f: os.path.getmtime(os.path.join(bdir, f)))
    return web.FileResponse(
        os.path.join(bdir, newest),
        headers={"Content-Disposition": f'attachment; filename="{newest}"'})


async def _h_download(request):
    if request.match_info["token"] != DOWNLOAD_SECRET:
        return web.Response(status=403, text="forbidden")
    gid, fname = request.match_info["gid"], request.match_info["fname"]
    if not gid.isdigit() or "/" in fname or ".." in fname or not fname.endswith(".zip"):
        return web.Response(status=400, text="bad request")
    path = os.path.join(storage.backups_dir(int(gid)), fname)
    if not os.path.isfile(path):
        return web.Response(status=404, text="not found")
    return web.FileResponse(
        path, headers={"Content-Disposition": f'attachment; filename="{fname}"'})


async def _start_webserver():
    app = web.Application(client_max_size=0)
    app.router.add_get("/", _h_health)
    app.router.add_get("/latest/{token}/{gid}", _h_latest)
    app.router.add_get("/dl/{token}/{gid}/{fname}", _h_download)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", _PORT).start()
    log.info("download web server listening on :%d (public=%s)", _PORT, _PUBLIC_DOMAIN or "none")

# A single in-flight backup task per guild, so /backup can't be spammed.
in_flight: dict[int, backup.Progress] = {}
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
    global _web_started
    if not _web_started:
        _web_started = True
        bot.loop.create_task(_start_webserver())
    if config.AUTO_BACKUP_HOURS > 0:
        for g in bot.guilds:
            schedules[g.id] = config.AUTO_BACKUP_HOURS
        bot.loop.create_task(_auto_loop())


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


async def _auto_loop():
    """Background loop: re-runs /backup for every scheduled guild."""
    while True:
        for guild_id, hours in list(schedules.items()):
            try:
                guild = bot.get_guild(guild_id)
                if guild and guild_id not in in_flight:
                    progress = backup.Progress()
                    in_flight[guild_id] = progress
                    try:
                        await backup.run_backup(guild, progress)
                        storage.make_zip(guild_id, "auto")
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


def _admin_only(interaction: discord.Interaction) -> bool:
    """Only members with Manage Server or Administrator can run backups."""
    p = interaction.user.guild_permissions  # type: ignore[union-attr]
    return p.administrator or p.manage_guild


# --------------------------------------------------------------------------- #
#  /backup
# --------------------------------------------------------------------------- #
@tree.command(name="backup",
              description="نسخة احتياطية كاملة للسيرفر / Full server backup")
async def backup_cmd(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message(
            "هذا الأمر داخل السيرفر فقط / server-only command.", ephemeral=True)
    if not _admin_only(interaction):
        return await interaction.response.send_message(
            "⛔ تحتاج صلاحية Manage Server / you need Manage Server.",
            ephemeral=True)
    if interaction.guild.id in in_flight:
        return await interaction.response.send_message(
            "⏳ في نسخة قيد التشغيل / a backup is already running.",
            ephemeral=True)

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
        zip_path = storage.make_zip(interaction.guild.id, "manual")
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
    e.add_field(name="📁 Server", value=guild.name, inline=False)
    e.add_field(name="📊 Progress",
                value=f"`{bar}` {pct:.0f}%\n"
                      f"{p.channels_done} / {p.channels_total} channels",
                inline=False)
    e.add_field(name="💬 Messages",    value=f"{p.messages:,}",     inline=True)
    e.add_field(name="📎 Attachments", value=f"{p.attachments:,}",  inline=True)
    e.add_field(name="💾 Downloaded",  value=_fmt_size(p.bytes),    inline=True)
    e.add_field(name="⏱️ Elapsed",     value=f"{p.elapsed():.0f} s", inline=True)
    if not done and p.current_channel:
        e.add_field(name="🔄 Channel", value=f"#{p.current_channel}", inline=True)
    if done and zip_path:
        e.add_field(name="📦 Snapshot",
                    value=f"`{os.path.basename(zip_path)}` ({_fmt_size(zip_size or 0)})",
                    inline=False)
        e.set_footer(text="Use /download to fetch the archive.")
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
    conn.close()
    if not run:
        return await interaction.response.send_message(
            "ماكو نسخة بعد / no backups yet. Use **/backup** to create one.",
            ephemeral=True)
    folder_bytes = storage.dir_size(storage.guild_dir(interaction.guild.id))
    e = discord.Embed(title="📊 آخر نسخة احتياطية / Latest backup",
                      color=0x5865F2)
    e.add_field(name="🕒 Started", value=run["started_at"], inline=True)
    e.add_field(name="🕒 Ended",   value=run["ended_at"] or "—", inline=True)
    e.add_field(name="📁 Channels", value=str(run["channels"]), inline=True)
    e.add_field(name="💬 Messages", value=f"{run['messages']:,}", inline=True)
    e.add_field(name="📎 Attachments", value=f"{run['attachments']:,}", inline=True)
    e.add_field(name="💾 Bytes (DL)", value=_fmt_size(run["bytes"] or 0),
                inline=True)
    e.add_field(name="🗄️ On-disk total",
                value=_fmt_size(folder_bytes), inline=True)
    if run["error"]:
        e.add_field(name="⚠️ Error", value=str(run["error"]), inline=False)
    e.set_footer(text="Use /download to fetch the .zip snapshot.")
    await interaction.response.send_message(embed=e, ephemeral=True)


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
    # Discord limit for a normal bot upload is 25 MB; nitro-boosted servers
    # raise it. Past that, point at the on-disk path instead.
    if size > 25 * 1024 * 1024:
        link = _download_link(interaction.guild.id, os.path.basename(path))
        if link:
            return await interaction.response.send_message(
                f"📦 الأرشيف كبير ({_fmt_size(size)}) — حمّله من هنا (رابط خاص):\n{link}\n"
                f"⬇️ Large archive — download via this private link.",
                ephemeral=True)
        return await interaction.response.send_message(
            f"📦 الأرشيف كبير ({_fmt_size(size)}) — حمّله من السيرفر:\n"
            f"`{path}`\nfile too large for direct upload ({_fmt_size(size)}).",
            ephemeral=True)
    await interaction.response.send_message(
        content="📦 آخر نسخة / latest archive:",
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
    link="رابط ملف .zip للنسخة (الأسهل) / a backup .zip download link (easiest)",
    source="أو ID سيرفر نسخة محفوظة / OR a saved backup's guild id (blank = here)",
    target="ID السيرفر الوجهة (فاضي = هنا) / target guild id (blank = here)",
    messages="استرجاع الرسائل أيضاً؟ بطيء / replay messages too (slow)")
async def restore_cmd(interaction: discord.Interaction,
                      link: Optional[str] = None,
                      source: Optional[str] = None,
                      target: Optional[str] = None,
                      messages: bool = True):
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
        if not link.lower().startswith(("http://", "https://")) or ".zip" not in link.lower():
            return await interaction.response.send_message(
                "❌ لازم رابط .zip مباشر / link must be a direct .zip URL.", ephemeral=True)
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
            except discord.HTTPException:
                pass
    p = await task
    try:
        await interaction.edit_original_response(
            embed=_restore_embed(p, target_guild.name))
    except discord.HTTPException:
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
#  /help
# --------------------------------------------------------------------------- #
@tree.command(name="help", description="الأوامر / Commands")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="💾 BackUp Bot — الأوامر / Commands",
        description=(
            "**/backup** — نسخة كاملة للسيرفر / full server backup\n"
            "**/backup_channel** `<channel>` — روم واحد فقط / one channel only\n"
            "**/status** — معلومات آخر نسخة / last backup info\n"
            "**/download** — حمّل آخر `.zip` / fetch the latest archive\n"
            "**/schedule** `<hours>` — نسخ تلقائي / auto-backup every N h\n"
            "**/search** `<query>` — ابحث في الرسائل / search archived msgs\n\n"
            "Backups capture **everything**: channels, roles, members "
            "(incl. admins), every message, embeds, reactions, mentions, "
            "and downloaded attachments.\n\n"
            "**Required intents** (Developer Portal → Bot):\n"
            "• Server Members Intent\n"
            "• Message Content Intent"
        ),
        color=0x5865F2,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


if __name__ == "__main__":
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set — see .env.example")
    os.makedirs(config.DATA_DIR, exist_ok=True)
    bot.run(config.DISCORD_TOKEN)
