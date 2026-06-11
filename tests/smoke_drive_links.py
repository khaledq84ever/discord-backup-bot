"""Smoke test for the Drive-mirror link plumbing — run with:
    .venv/bin/python tests/smoke_drive_links.py
Imports bot.py for real (catches runtime errors py_compile can't), then checks
_guild_token determinism and every _resolve_restore_link branch.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402  (module import itself is the first assertion)

GID = 787744065801945089
DRIVE_URL = "https://drive.google.com/open?id=TESTONLY"

# _guild_token: deterministic, 24 hex chars, differs per guild.
t1, t2 = bot._guild_token(GID), bot._guild_token(GID + 1)
assert t1 == bot._guild_token(GID) and len(t1) == 24 and t1 != t2, "token derivation broke"

bot._PUBLIC_DOMAIN = "backup-bot-production.up.railway.app"
bot._drive_links.clear()
bot._drive_links[str(GID)] = DRIVE_URL

# Bot-issued Drive link → resolves to the local guild id (no URL download).
assert bot._resolve_restore_link(DRIVE_URL) == (None, GID)

# Our own /latest link → raw=1 appended so the Drive 302 doesn't serve HTML.
latest = bot._latest_link(GID + 1)  # guild WITHOUT a drive link → /latest URL
url, lgid = bot._resolve_restore_link(latest)
assert lgid is None and url == latest + "?raw=1", f"unexpected: {url}"
# Already has raw → untouched.
assert bot._resolve_restore_link(url) == (url, None)

# Foreign URL → passed through untouched.
foreign = "https://cdn.discordapp.com/attachments/1/2/backup.zip"
assert bot._resolve_restore_link(foreign) == (foreign, None)

# _latest_link prefers the Drive link when one exists.
assert bot._latest_link(GID) == DRIVE_URL
assert bot._latest_link(GID + 1).startswith("https://backup-bot-production")

print("OK — bot imports clean, token + link resolution all pass")
