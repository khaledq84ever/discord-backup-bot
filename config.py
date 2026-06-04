"""Environment-driven config for the backup bot."""
import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
APPLICATION_ID = os.getenv("APPLICATION_ID", "")

# Invite with ADMINISTRATOR (8) so the bot can read EVERY channel the moment it's
# added — no "channel skipped / denied access" gaps in backups. A bot cannot grant
# itself admin after joining (Discord forbids self-elevation), so it must be on the
# invite link. Override with INVITE_PERMISSIONS to request narrower perms instead.
INVITE_PERMISSIONS = os.getenv("INVITE_PERMISSIONS", "8")

# Optional dev guild ID for instant slash-command sync during development.
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID", "")

# Where backups live on disk (Railway volume = /data).
DATA_DIR = os.getenv("DATA_DIR", "/data") if os.path.exists("/data") \
    else os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Cap message scrape per channel (0 = no cap, scrape EVERYTHING from the very first
# message the server ever had). Default 0 — full history, no limit.
MAX_MESSAGES_PER_CHANNEL = int(os.getenv("MAX_MESSAGES_PER_CHANNEL", "0"))

# Skip attachments larger than this many MB (0 = no per-file size cap).
# 500 = Discord's own largest possible attachment, so effectively no per-file limit.
MAX_ATTACHMENT_MB = int(os.getenv("MAX_ATTACHMENT_MB", "500"))

# Per-SERVER total backup size cap, in GB (0 = unlimited). Once a guild's stored data
# reaches this, attachment downloads stop (text/messages still archived) so one huge
# server can't fill the shared Railway volume. Messages + time are never capped.
MAX_SERVER_BACKUP_GB = float(os.getenv("MAX_SERVER_BACKUP_GB", "5"))

# Auto-backup interval in hours (0 = off).
AUTO_BACKUP_HOURS = int(os.getenv("AUTO_BACKUP_HOURS", "0"))

# How many days a .zip snapshot is kept on the server before auto-delete.
# Only the newest zip per guild is ever kept (duplicates are pruned).
# 3 days — small storage (dedup = 1 zip/server) but the restore link stays alive
# long enough to download + /restore later.
BACKUP_RETENTION_DAYS = float(os.getenv("BACKUP_RETENTION_DAYS", "3"))


def invite_url() -> str:
    if not APPLICATION_ID:
        return ""
    return ("https://discord.com/oauth2/authorize"
            f"?client_id={APPLICATION_ID}"
            f"&permissions={INVITE_PERMISSIONS}"
            "&scope=bot%20applications.commands")
