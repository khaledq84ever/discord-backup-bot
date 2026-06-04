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
import hashlib
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


# --------------------------------------------------------------------------- #
#  Content-addressed attachment store (sha256 dedup)
# --------------------------------------------------------------------------- #
#  Files are stored by their content hash under attachments/sha/<aa>/<sha256><ext>
#  so the SAME bytes (a reposted image, repeated sticker, shared meme) live on
#  disk exactly once even when posted across many messages/channels.
def content_path(guild_id: int, sha256: str, filename: str = "") -> str:
    """Where an attachment with this content hash is stored on disk.

    Sharded by the first 2 hex chars to avoid one huge flat directory. The
    original extension is preserved so the file is still openable/serveable."""
    ext = os.path.splitext(filename or "")[1][:16]
    sub = os.path.join(attachments_dir(guild_id), "sha", sha256[:2])
    os.makedirs(sub, exist_ok=True)
    return os.path.join(sub, f"{sha256}{ext}")


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def dedup_attachments(guild_id: int) -> dict:
    """One-time / repeatable migration: collapse duplicate attachment files into
    the content-addressed store and reclaim disk.

    Walks every file already in attachments/ (the old flat <id>-<name> layout AND
    any already-hashed files), hashes it, and keeps exactly ONE copy per unique
    sha256 under attachments/sha/<aa>/<sha><ext>. Duplicates are deleted. The DB's
    attachments.local_path + sha256 are rewritten to point at the kept copy.

    Safe & idempotent: re-running does nothing once everything is deduped. Returns
    stats: files_before, files_after, bytes_reclaimed, unique."""
    adir = attachments_dir(guild_id)
    sha_root = os.path.join(adir, "sha")
    # path-on-disk -> sha256, computed once.
    file_hash: dict = {}
    files_before = 0
    for root, _, files in os.walk(adir):
        for name in files:
            fp = os.path.join(root, name)
            if fp.endswith(".part"):
                try:
                    os.remove(fp)
                except OSError:
                    pass
                continue
            files_before += 1
            try:
                file_hash[fp] = _hash_file(fp)
            except OSError:
                pass

    reclaimed = 0
    # old absolute path -> new canonical path, to rewrite the DB afterwards.
    remap: dict = {}
    seen_basenames: dict = {}  # sha -> kept canonical path
    for fp, sha in sorted(file_hash.items()):
        # Derive a filename (for the extension) from the existing name.
        base = os.path.basename(fp)
        # old flat layout was "<id>-<filename>"; recover the original name part.
        orig = base.split("-", 1)[1] if "-" in base and not fp.startswith(sha_root) else base
        target = content_path(guild_id, sha, orig)
        if os.path.abspath(fp) == os.path.abspath(target):
            seen_basenames.setdefault(sha, target)
            continue  # already canonical
        if sha in seen_basenames or os.path.exists(target):
            # duplicate content — drop this copy, point it at the canonical file.
            try:
                size = os.path.getsize(fp)
                os.remove(fp)
                reclaimed += size
            except OSError:
                size = 0
            remap[fp] = seen_basenames.get(sha, target)
        else:
            try:
                os.replace(fp, target)
                remap[fp] = target
                seen_basenames[sha] = target
            except OSError:
                pass

    # Rewrite DB local_path + sha256 so restore finds the canonical files.
    conn = open_db(guild_id)
    try:
        rows = conn.execute(
            "SELECT id, local_path FROM attachments WHERE local_path IS NOT NULL"
        ).fetchall()
        for aid, lp in rows:
            new_lp = remap.get(lp)
            sha = None
            if new_lp:
                sha = _sha_from_path(new_lp)
            elif lp and os.path.isfile(lp) and lp.startswith(sha_root):
                new_lp, sha = lp, _sha_from_path(lp)
            if new_lp:
                conn.execute(
                    "UPDATE attachments SET local_path = ?, sha256 = ? WHERE id = ?",
                    (new_lp, sha, aid))
        conn.commit()
    finally:
        conn.close()

    # Clean up now-empty old subdirs (best effort).
    files_after = sum(len(fs) for _, _, fs in os.walk(adir))
    return {"files_before": files_before, "files_after": files_after,
            "bytes_reclaimed": reclaimed, "unique": len(seen_basenames)}


def _sha_from_path(path: str) -> Optional[str]:
    """Extract the sha256 from a content-addressed path (the filename stem)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem if len(stem) == 64 and all(c in "0123456789abcdef" for c in stem) else None


def dedup_all() -> dict:
    """Run sha256 dedup across EVERY backed-up server. Returns aggregate stats."""
    base = config.DATA_DIR
    try:
        guilds = [int(d) for d in os.listdir(base) if d.isdigit()]
    except OSError:
        return {"guilds": 0, "bytes_reclaimed": 0, "files_removed": 0}
    total_reclaimed = files_removed = 0
    for g in guilds:
        try:
            s = dedup_attachments(g)
            total_reclaimed += s["bytes_reclaimed"]
            files_removed += max(0, s["files_before"] - s["files_after"])
        except Exception:
            pass
    return {"guilds": len(guilds), "bytes_reclaimed": total_reclaimed,
            "files_removed": files_removed}


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
    # Migrations: add any columns introduced after the original schema shipped, so
    # old per-guild DBs transparently gain them. Must run BEFORE any index that
    # references a new column (SCHEMA above no longer indexes new columns directly).
    for table, cols in _MIGRATIONS.items():
        for col, decl in cols.items():
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
    # Now the sha256 column is guaranteed to exist — safe to index it.
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_attachments_sha ON attachments(sha256)")
        conn.commit()
    except sqlite3.OperationalError:
        pass
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
    content_type TEXT,
    sha256     TEXT
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
    error      TEXT,
    integrity_json TEXT
);
"""


# Columns added after the original schema shipped — applied on every open_db so
# old per-guild DBs migrate transparently. (col_name -> "TYPE" definition.)
_MIGRATIONS = {
    "attachments": {"sha256": "TEXT"},
    "backup_runs": {"integrity_json": "TEXT"},
}


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
    row = {"sha256": None, **row}  # sha256 optional for older callers
    conn.execute(
        """INSERT OR REPLACE INTO attachments
           (id, message_id, channel_id, filename, url, size, local_path,
            content_type, sha256)
           VALUES (:id, :message_id, :channel_id, :filename, :url, :size,
                   :local_path, :content_type, :sha256)""",
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
               error: Optional[str] = None,
               integrity_json: Optional[str] = None) -> None:
    conn.execute(
        """UPDATE backup_runs
           SET ended_at = ?, channels = ?, messages = ?, attachments = ?,
               bytes = ?, error = ?, integrity_json = ?
           WHERE id = ?""",
        (time.strftime("%Y-%m-%dT%H:%M:%S"), channels, messages, attachments,
         byte_count, error, integrity_json, run_id),
    )
    conn.commit()


def latest_run(conn: sqlite3.Connection) -> Optional[dict]:
    r = conn.execute(
        """SELECT id, started_at, ended_at, channels, messages, attachments,
                  bytes, error, integrity_json
           FROM backup_runs ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    if not r:
        return None
    return dict(zip(
        ["id", "started_at", "ended_at", "channels", "messages",
         "attachments", "bytes", "error", "integrity_json"], r))


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
