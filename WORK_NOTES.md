# Work Notes — Discord Backup Bot

## 2026-06-11 — Google Drive mirror integration (NOT deployed yet)

- Goal: every guild's latest backup mirrored to `gdrive:backupdiscord/by-guild/<gid>.zip`
  ("anyone with link"), and the bot hands out the **Drive link** everywhere.
- `bot.py`: `_drive_links` map persisted at `DATA_DIR/drive_links.json`; `_latest_link()`
  prefers the Drive link (auto-covers /status, /download, /report, /copy);
  `/latest/<token>/<gid>` 302-redirects to Drive — `?raw=1` bypasses the redirect for
  callers needing zip bytes (mirror job). `/restore` resolves bot-issued Drive links back
  to the LOCAL snapshot (Drive serves an HTML viewer, not the zip) and adds raw=1 to our
  own /latest links. New `POST /admin/<secret>/set_drive_links` merges + persists the map.
- `mirror_to_drive.sh`: resumable (skips same-size files already on Drive), flock
  single-instance lock, POSTs the map to set_drive_links at the end, uses raw=1.
  Daily crontab @ 05:30 keeps Drive in sync.
- Known tradeoff: after a fresh /backup, links serve the previous Drive copy until the
  next 05:30 mirror run.
- **DEPLOY GATE:** do NOT `railway up` (or push, in case auto-deploy is on) while a mirror
  run is streaming from /latest — a restart kills the in-flight transfers. Deploy after
  `mirror.log` prints the `uploaded=… wrote map` summary, then POST `drive_links.json`.
- Commits (local): `73ce94f`, `a732ab2`, `c784e09`.

## 2026-06-03 — Per-guild private links + dependency upgrade

- Committed pre-existing WIP: **per-guild HMAC download links**. Each server gets a unique
  HMAC-derived token (`/latest/<token>/<gid>`), constant-time compared — one server's link
  can't be tweaked to reach another's backup.
- Bumped deps: `discord.py>=2.7.1, aiohttp>=3.14.0, python-dotenv>=1.2.2` (built clean).

**Verified:** Railway deploy logs → actively archiving channels, *"✅ no channels skipped —
full read access"* (bot live and working).
**Shipped:** Railway `backup-bot` SUCCESS · GitHub `master` commit `1aea627`.
