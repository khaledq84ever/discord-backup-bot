# Work Notes — Discord Backup Bot

## 2026-06-03 — Per-guild private links + dependency upgrade

- Committed pre-existing WIP: **per-guild HMAC download links**. Each server gets a unique
  HMAC-derived token (`/latest/<token>/<gid>`), constant-time compared — one server's link
  can't be tweaked to reach another's backup.
- Bumped deps: `discord.py>=2.7.1, aiohttp>=3.14.0, python-dotenv>=1.2.2` (built clean).

**Verified:** Railway deploy logs → actively archiving channels, *"✅ no channels skipped —
full read access"* (bot live and working).
**Shipped:** Railway `backup-bot` SUCCESS · GitHub `master` commit `1aea627`.
