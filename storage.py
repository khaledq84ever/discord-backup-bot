"""On-disk storage layout for each backed-up guild.

Layout (under config.DATA_DIR):
  <guild_id>/
    guild.json         — guild metadata (name, icon, banner, features, ...)
    channels.json      — every channel/category (topic, type, position, perms)
    roles.json         — every role with permissions + members
    members.json       — every member (id, name, nick, joined_at, roles, avatar)
    emojis.json        — custom emojis (id, name, animated, url)
    backup.db          — SQLite with FTS-ready messages table
    attachments/       — downloaded files keyed by message id
    backups/           — periodic .zip snapshots
    last_backup.json   — pointer to latest snapshot + counters
"""
import json
import os
import sqlite3
import time
import zipfile
from typing import Optional

import config


def guild_dir(guild_id: int) -> str:
    p = os.path.join(config.DATA_DIR, str(guild_id))
    os.makedirs(p, exist_ok=True)
    return p


def attachments_dir(guild_id: int) -> str:
    p = os.path.join(guild_dir(guild_id), "attachments")
    os.makedirs(p, exist_ok=True)
    return p


def backups_dir(guild_id: int) -> str:
    p = os.path.join(guild_dir(guild_id), "backups")
    os.makedirs(p, exist_ok=True)
    return p


def write_json(guild_id: int, name: str, data) -> None:
    path = os.path.join(guild_dir(guild_id), name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_json(guild_id: int, name: str, default=None):
    path = os.path.join(guild_dir(guild_id), name)
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
#  SQLite — one DB per guild, holds every message
# --------------------------------------------------------------------------- #
def db_path(guild_id: int) -> str:
    return os.path.join(guild_dir(guild_id), "backup.db")


def open_db(guild_id: int) -> sqlite3.Connection:
    # check_same_thread=False so batch writes can be flushed via asyncio.to_thread
    # (off the event loop) during backup — access is serialized (awaited one at a
    # time), so there is no concurrent use.
    conn = sqlite3.connect(db_path(guild_id), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY,
    channel_id    INTEGER NOT NULL,
    channel_name  TEXT,
    author_id     INTEGER NOT NULL,
    author_name   TEXT,
    content       TEXT,
    created_at    TEXT,
    edited_at     TEXT,
    reply_to      INTEGER,
    pinned        INTEGER DEFAULT 0,
    type          TEXT,
    embeds_json   TEXT,
    reactions_json TEXT,
    mentions_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id);
CREATE INDEX IF NOT EXISTS idx_messages_author  ON messages(author_id);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);

CREATE TABLE IF NOT EXISTS attachments (
    id         INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    filename   TEXT,
    url        TEXT,
    size       INTEGER,
    local_path TEXT,
    content_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id);

CREATE TABLE IF NOT EXISTS backup_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    channels   INTEGER DEFAULT 0,
    messages   INTEGER DEFAULT 0,
    attachments INTEGER DEFAULT 0,
    bytes      INTEGER DEFAULT 0,
    error      TEXT
);
"""


def newest_message_id(conn: sqlite3.Connection, channel_id: int) -> Optional[int]:
    """Latest stored message in a channel — used to incrementally resume."""
    r = conn.execute(
        "SELECT MAX(id) FROM messages WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    return r[0] if r and r[0] else None


def upsert_message(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO messages
           (id, channel_id, channel_name, author_id, author_name, content,
            created_at, edited_at, reply_to, pinned, type,
            embeds_json, reactions_json, mentions_json)
           VALUES (:id, :channel_id, :channel_name, :author_id, :author_name,
                   :content, :created_at, :edited_at, :reply_to, :pinned,
                   :type, :embeds_json, :reactions_json, :mentions_json)""",
        row,
    )


def upsert_attachment(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO attachments
           (id, message_id, channel_id, filename, url, size, local_path, content_type)
           VALUES (:id, :message_id, :channel_id, :filename, :url, :size,
                   :local_path, :content_type)""",
        row,
    )


def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO backup_runs (started_at) VALUES (?)",
        (time.strftime("%Y-%m-%dT%H:%M:%S"),),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, *, channels: int,
               messages: int, attachments: int, byte_count: int,
               error: Optional[str] = None) -> None:
    conn.execute(
        """UPDATE backup_runs
           SET ended_at = ?, channels = ?, messages = ?, attachments = ?,
               bytes = ?, error = ?
           WHERE id = ?""",
        (time.strftime("%Y-%m-%dT%H:%M:%S"), channels, messages, attachments,
         byte_count, error, run_id),
    )
    conn.commit()


def latest_run(conn: sqlite3.Connection) -> Optional[dict]:
    r = conn.execute(
        """SELECT id, started_at, ended_at, channels, messages, attachments,
                  bytes, error
           FROM backup_runs ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    if not r:
        return None
    return dict(zip(
        ["id", "started_at", "ended_at", "channels", "messages",
         "attachments", "bytes", "error"], r))


# --------------------------------------------------------------------------- #
#  Zip snapshot — one .zip per /backup call, easy to download/share
# --------------------------------------------------------------------------- #
def make_zip(guild_id: int, label: str) -> str:
    """Bundle the current backup contents into a ZIP. Returns the path.

    After writing, prunes the backups dir so only this newest snapshot remains
    (no duplicate zips piling up when /backup is run repeatedly)."""
    src = guild_dir(guild_id)
    out = os.path.join(backups_dir(guild_id),
                       f"{label}-{int(time.time())}.zip")
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as z:
        for root, _, files in os.walk(src):
            # Skip the backups dir itself so we don't recurse into our own output.
            if os.path.relpath(root, src).startswith("backups"):
                continue
            for name in files:
                fp = os.path.join(root, name)
                z.write(fp, arcname=os.path.relpath(fp, src))
    # Dedup: keep only this newest zip; drop older/duplicate snapshots.
    prune_backups(guild_id, keep=1)
    return out


def prune_all(keep: int = 1, max_age_days: Optional[float] = None) -> int:
    """Run retention across EVERY server folder on the volume — so expired/dup
    zips are removed even when no one hits a link or runs a new backup.
    Returns total files removed."""
    base = config.DATA_DIR
    try:
        guilds = [d for d in os.listdir(base) if d.isdigit()]
    except OSError:
        return 0
    removed = 0
    for g in guilds:
        try:
            removed += prune_backups(int(g), keep=keep, max_age_days=max_age_days)
        except Exception:
            pass
    return removed


def prune_backups(guild_id: int, keep: int = 1,
                  max_age_days: Optional[float] = None) -> int:
    """Enforce retention on a guild's .zip snapshots:
      • keep only the newest `keep` zips (dedup — removes duplicate backups), and
      • delete any zip older than `max_age_days` (default config.BACKUP_RETENTION_DAYS),
        so a backup the user requested is only stored for that many days.
    Returns the number of files removed."""
    if max_age_days is None:
        max_age_days = float(getattr(config, "BACKUP_RETENTION_DAYS", 3))
    bdir = backups_dir(guild_id)
    try:
        zips = [os.path.join(bdir, f) for f in os.listdir(bdir)
                if f.endswith(".zip")]
    except OSError:
        return 0
    zips.sort(key=os.path.getmtime, reverse=True)   # newest first
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for i, fp in enumerate(zips):
        beyond_keep = i >= keep
        try:
            too_old = os.path.getmtime(fp) < cutoff
        except OSError:
            too_old = False
        if beyond_keep or too_old:
            try:
                os.remove(fp)
                removed += 1
            except OSError:
                pass
    return removed


def dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


import shutil


def storage_stats() -> dict:
    """Fast, real-time storage numbers across ALL backed-up servers:
      guilds     — how many server folders exist (one per Discord server)
      snapshots  — total .zip backups currently stored
      used_bytes — disk used on the volume (≈ all backup data)
      total_bytes— volume capacity
    Cheap: lists folders + one statvfs, no full tree walk."""
    base = config.DATA_DIR
    try:
        guilds = [d for d in os.listdir(base)
                  if d.isdigit() and os.path.isdir(os.path.join(base, d))]
    except OSError:
        guilds = []
    snapshots = 0
    for g in guilds:
        bd = os.path.join(base, g, "backups")
        try:
            snapshots += sum(1 for f in os.listdir(bd) if f.endswith(".zip"))
        except OSError:
            pass
    try:
        du = shutil.disk_usage(base)
        used, total = du.used, du.total
    except OSError:
        used = total = 0
    return {"guilds": len(guilds), "snapshots": snapshots,
            "used_bytes": used, "total_bytes": total,
            "updated": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}


def guild_file_count(guild_id: int) -> int:
    """How many individual files are stored for this server (attachments + json
    + db + zips). Cheap enough for a single guild folder."""
    n = 0
    for _, _, files in os.walk(guild_dir(guild_id)):
        n += len(files)
    return n


def snapshot_age_seconds(guild_id: int) -> Optional[float]:
    """Seconds since this server's newest .zip was written (None if no zip)."""
    bdir = backups_dir(guild_id)
    try:
        zips = [os.path.join(bdir, f) for f in os.listdir(bdir)
                if f.endswith(".zip")]
    except OSError:
        return None
    if not zips:
        return None
    newest = max(zips, key=os.path.getmtime)
    return time.time() - os.path.getmtime(newest)
