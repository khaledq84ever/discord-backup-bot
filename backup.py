"""Backup engine — scrape every visible channel + every message + every
attachment for a guild, plus all roles/members/emojis/server metadata.

All disk + HTTP work uses asyncio so the bot's gateway never blocks.
Resumable: re-running /backup picks up where the previous run left off
per channel via storage.newest_message_id.
"""
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import discord

import config
import storage

log = logging.getLogger("backup")


@dataclass
class Progress:
    channels_total: int = 0
    channels_done: int = 0
    messages: int = 0
    attachments: int = 0
    bytes: int = 0
    current_channel: str = ""
    started: float = field(default_factory=time.time)
    error: Optional[str] = None
    cancelled: bool = False
    skipped: list = field(default_factory=list)   # channels denied (no access)

    def elapsed(self) -> float:
        return time.time() - self.started


# --------------------------------------------------------------------------- #
#  Guild metadata snapshot
# --------------------------------------------------------------------------- #
def snapshot_guild(guild: discord.Guild) -> dict:
    return {
        "id":            guild.id,
        "name":          guild.name,
        "description":   guild.description,
        "icon_url":      str(guild.icon.url) if guild.icon else None,
        "banner_url":    str(guild.banner.url) if guild.banner else None,
        "splash_url":    str(guild.splash.url) if guild.splash else None,
        "owner_id":      guild.owner_id,
        "member_count":  guild.member_count,
        "premium_tier":  guild.premium_tier,
        "preferred_locale":  str(guild.preferred_locale),
        "features":      list(guild.features),
        "created_at":    guild.created_at.isoformat(),
        "verification_level":  str(guild.verification_level),
        "explicit_content_filter": str(guild.explicit_content_filter),
        "default_notifications":  str(guild.default_notifications),
        "afk_timeout":   guild.afk_timeout,
        "afk_channel_id": guild.afk_channel.id if guild.afk_channel else None,
        "system_channel_id": guild.system_channel.id if guild.system_channel else None,
        "vanity_url":    guild.vanity_url,
        "snapshot_at":   time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def snapshot_channels(guild: discord.Guild) -> list:
    out = []
    for ch in guild.channels:
        row = {
            "id":          ch.id,
            "name":        ch.name,
            "type":        str(ch.type),
            "position":    getattr(ch, "position", 0),
            "category_id": ch.category_id,
            "category":    ch.category.name if ch.category else None,
        }
        for attr in ("topic", "nsfw", "slowmode_delay", "bitrate",
                     "user_limit", "rtc_region"):
            v = getattr(ch, attr, None)
            if v is not None:
                row[attr] = v if isinstance(v, (int, str, bool, float)) else str(v)
        # Per-role permission overrides
        overwrites = []
        for target, perms in getattr(ch, "overwrites", {}).items():
            allow, deny = perms.pair()
            overwrites.append({
                "target_id":   target.id,
                "target_name": getattr(target, "name", str(target)),
                "target_type": "role" if isinstance(target, discord.Role) else "member",
                "allow":       allow.value,
                "deny":        deny.value,
            })
        row["overwrites"] = overwrites
        out.append(row)
    return out


def snapshot_roles(guild: discord.Guild) -> list:
    return [{
        "id":          r.id,
        "name":        r.name,
        "color":       r.color.value,
        "hoist":       r.hoist,
        "mentionable": r.mentionable,
        "position":    r.position,
        "permissions": r.permissions.value,
        "managed":     r.managed,
        "icon_url":    str(r.icon.url) if getattr(r, "icon", None) else None,
        "member_ids":  [m.id for m in r.members],
        "created_at":  r.created_at.isoformat(),
    } for r in guild.roles]


def snapshot_members(guild: discord.Guild) -> list:
    """Requires the SERVER MEMBERS INTENT to be enabled."""
    return [{
        "id":         m.id,
        "name":       m.name,
        "global_name": getattr(m, "global_name", None),
        "display_name": m.display_name,
        "nick":       m.nick,
        "bot":        m.bot,
        "joined_at":  m.joined_at.isoformat() if m.joined_at else None,
        "created_at": m.created_at.isoformat(),
        "avatar_url": str(m.display_avatar.url) if m.display_avatar else None,
        "roles":      [r.id for r in m.roles if r.name != "@everyone"],
        "premium_since": m.premium_since.isoformat() if m.premium_since else None,
        "is_admin":   m.guild_permissions.administrator,
    } for m in guild.members]


def snapshot_emojis(guild: discord.Guild) -> list:
    return [{
        "id":          e.id,
        "name":        e.name,
        "animated":    e.animated,
        "available":   e.available,
        "managed":     e.managed,
        "require_colons": e.require_colons,
        "url":         str(e.url),
        "created_at":  e.created_at.isoformat() if e.created_at else None,
    } for e in guild.emojis]


# --------------------------------------------------------------------------- #
#  Message + attachment scrape
# --------------------------------------------------------------------------- #
def _message_row(m: discord.Message, channel_name: str) -> dict:
    return {
        "id":           m.id,
        "channel_id":   m.channel.id,
        "channel_name": channel_name,
        "author_id":    m.author.id,
        "author_name":  f"{m.author}",
        "content":      m.content or "",
        "created_at":   m.created_at.isoformat(),
        "edited_at":    m.edited_at.isoformat() if m.edited_at else None,
        "reply_to":     m.reference.message_id if m.reference else None,
        "pinned":       1 if m.pinned else 0,
        "type":         str(m.type),
        "embeds_json":  json.dumps([e.to_dict() for e in m.embeds], ensure_ascii=False),
        "reactions_json": json.dumps(
            [{"emoji": str(r.emoji), "count": r.count} for r in m.reactions],
            ensure_ascii=False),
        "mentions_json": json.dumps(
            {"users":   [u.id for u in m.mentions],
             "roles":   [r.id for r in m.role_mentions],
             "channels":[c.id for c in m.channel_mentions]},
            ensure_ascii=False),
    }


async def _download_attachment(session: aiohttp.ClientSession,
                                 a: discord.Attachment, dest_dir: str
                                 ) -> Optional[str]:
    """Download to <dest_dir>/<message_id>-<filename>. Returns local path."""
    if a.size > config.MAX_ATTACHMENT_MB * 1024 * 1024:
        return None
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in a.filename)
    fname = f"{a.id}-{safe_name}"
    out = os.path.join(dest_dir, fname)
    if os.path.exists(out) and os.path.getsize(out) == a.size:
        return out  # already downloaded
    try:
        async with session.get(a.url) as r:
            r.raise_for_status()
            tmp = out + ".part"
            with open(tmp, "wb") as f:
                async for chunk in r.content.iter_chunked(1 << 16):
                    f.write(chunk)
            os.replace(tmp, out)
        return out
    except Exception as e:
        log.warning("attachment %s download failed: %s", a.filename, e)
        return None


def _flush_batch(conn, batch_msgs, batch_atts) -> None:
    """Synchronous DB write — run via asyncio.to_thread so the SQLite commit never
    blocks the gateway heartbeat (heartbeat-block was disconnecting us mid-channel,
    truncating big channels to ~300-500 msgs / ~10% of the server)."""
    for row in batch_msgs:
        storage.upsert_message(conn, row)
    for row in batch_atts:
        storage.upsert_attachment(conn, row)
    conn.commit()


# Transient errors worth resuming a channel on (gateway drop, CDN/API hiccup).
_RESUMABLE = (discord.HTTPException, discord.DiscordServerError,
              aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, OSError)


async def _scrape_channel(channel, conn, session, attachments_dir,
                          progress: Progress) -> None:
    """Pull EVERY message we don't already have (day 1 -> now), download attachments.

    Resilient + complete:
      - resumes from the last saved message id, so a gateway drop mid-channel does
        NOT lose the channel — it retries and continues where it stopped;
      - flushes DB batches OFF the event loop (asyncio.to_thread) and yields, so a
        big channel can't block the heartbeat and trigger a disconnect;
      - no message cap (limit=None) → full history.
    """
    progress.current_channel = channel.name
    limit = None if config.MAX_MESSAGES_PER_CHANNEL == 0 else \
        config.MAX_MESSAGES_PER_CHANNEL
    total_new = 0

    for attempt in range(8):                 # resume up to 8x across disconnects
        if progress.cancelled:
            break
        # Resume point: newest id we've already stored for this channel.
        after_id = storage.newest_message_id(conn, channel.id)
        after = discord.Object(id=after_id) if after_id else None
        batch_msgs, batch_atts = [], []
        try:
            async for m in channel.history(limit=limit, after=after, oldest_first=True):
                if progress.cancelled:
                    break
                batch_msgs.append(_message_row(m, channel.name))
                for a in m.attachments:
                    local = await _download_attachment(session, a, attachments_dir)
                    if local:
                        progress.bytes += a.size
                    batch_atts.append({
                        "id":         a.id,
                        "message_id": m.id,
                        "channel_id": channel.id,
                        "filename":   a.filename,
                        "url":        a.url,
                        "size":       a.size,
                        "local_path": local,
                        "content_type": a.content_type,
                    })
                    progress.attachments += 1
                total_new += 1
                progress.messages += 1
                # Flush every 200 messages, OFF the loop, then yield to the heartbeat.
                if len(batch_msgs) >= 200:
                    await asyncio.to_thread(_flush_batch, conn, batch_msgs, batch_atts)
                    batch_msgs, batch_atts = [], []
                    await asyncio.sleep(0)
            # Reached the end of history cleanly — flush remainder and we're done.
            await asyncio.to_thread(_flush_batch, conn, batch_msgs, batch_atts)
            break
        except discord.Forbidden:
            log.info("no access to #%s, skipping", channel.name)
            progress.skipped.append(channel.name)
            await asyncio.to_thread(_flush_batch, conn, batch_msgs, batch_atts)
            return
        except _RESUMABLE as e:
            # Save what we got, then retry — the next pass resumes from the new
            # newest-id, so we never lose or re-fetch what's already stored.
            await asyncio.to_thread(_flush_batch, conn, batch_msgs, batch_atts)
            log.warning("channel #%s interrupted (%s) — resuming (attempt %d/8)",
                        channel.name, type(e).__name__, attempt + 1)
            await asyncio.sleep(min(2 ** attempt, 20))
            continue
    log.info("channel #%s: +%d new messages", channel.name, total_new)


# --------------------------------------------------------------------------- #
#  Full guild backup
# --------------------------------------------------------------------------- #
TEXTLIKE = (discord.ChannelType.text,
            discord.ChannelType.news,
            discord.ChannelType.public_thread,
            discord.ChannelType.private_thread,
            discord.ChannelType.news_thread,
            discord.ChannelType.forum,
            discord.ChannelType.voice)  # voice channels can have text in newer Discord


async def _all_message_channels(guild: discord.Guild) -> list:
    """Every place that can hold messages: text/news/voice channels PLUS all
    threads — active and public/private *archived* (often most of an old server).
    """
    seen, out = set(), []

    def add(c):
        if c.id not in seen:
            seen.add(c.id)
            out.append(c)

    for c in guild.channels:
        if c.type in TEXTLIKE:
            add(c)
    for t in guild.threads:                      # currently-active threads
        add(t)
    # Archived threads must be fetched per parent channel.
    for c in guild.channels:
        if c.type not in (discord.ChannelType.text, discord.ChannelType.news,
                          discord.ChannelType.forum):
            continue
        for private in (False, True):
            try:
                async for t in c.archived_threads(limit=None, private=private):
                    add(t)
            except Exception:  # noqa: BLE001 — forbidden/forum-no-private/etc.; best-effort
                pass
    return out


async def run_backup(guild: discord.Guild, progress: Progress,
                     specific_channel: Optional[discord.abc.GuildChannel] = None
                     ) -> dict:
    """Full guild backup. Returns the latest_run dict."""
    storage.write_json(guild.id, "guild.json",    snapshot_guild(guild))
    storage.write_json(guild.id, "channels.json", snapshot_channels(guild))
    storage.write_json(guild.id, "roles.json",    snapshot_roles(guild))
    storage.write_json(guild.id, "members.json",  snapshot_members(guild))
    storage.write_json(guild.id, "emojis.json",   snapshot_emojis(guild))

    conn = storage.open_db(guild.id)
    run_id = storage.start_run(conn)
    atts_dir = storage.attachments_dir(guild.id)

    if specific_channel is not None:
        channels = [specific_channel]
    else:
        channels = await _all_message_channels(guild)
    progress.channels_total = len(channels)

    timeout = aiohttp.ClientTimeout(total=120, connect=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for ch in channels:
            if progress.cancelled:
                break
            try:
                await _scrape_channel(ch, conn, session, atts_dir, progress)
            except Exception as e:
                log.warning("channel %s failed: %s", getattr(ch, "name", "?"), e)
            progress.channels_done += 1

    storage.finish_run(conn, run_id,
                       channels=progress.channels_done,
                       messages=progress.messages,
                       attachments=progress.attachments,
                       byte_count=progress.bytes,
                       error=progress.error)
    run = storage.latest_run(conn)
    conn.close()

    if progress.skipped:
        log.warning("⚠️ %d channel(s) SKIPPED — bot denied access (give it Administrator): %s",
                    len(progress.skipped), ", ".join("#" + s for s in progress.skipped))
    else:
        log.info("✅ no channels skipped — full read access.")

    # Persist a quick-glance summary outside the DB too.
    storage.write_json(guild.id, "last_backup.json", run)
    return run
