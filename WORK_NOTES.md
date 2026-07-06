## 2026-07-06 — owner guild lost all cmds but /run; live-healed + hourly watchdog

- User report: his server (KhaledQ8 1461292328252739768) showed ONLY /run —
  Discord API confirmed: 1 guild cmd there, 0 cmds in 2 other guilds
  (1335276019501895681, 1493512921177919539), plus a stray GLOBAL 'run'
  (code intends global to be empty). Healthy guilds have 19 cmds.
- Live fix via REST (no redeploy): bulk-overwrite PUT of the 19-cmd set
  (copied from a healthy guild) onto the 3 broken guilds + PUT [] global.
  Verified: all 30 guilds now 19/19, global empty.
- `tools/backupbot-cmdcheck` (also ~/bin/ on the VPS, cron hourly at :20):
  compares every guild's registered cmds against template guild
  1461292328252739768 and re-PUTs any drift; keeps global empty; refuses to
  heal if the template itself looks broken. Log: ~/.cache/backupbot-cmdcheck.log
- ⚠ REPO IS BEHIND DEPLOYED: live bot has /run + /pm2 which are NOT in this
  repo (19 live vs 17 here). Deploy source was on another machine. Do NOT
  `railway up` from this clone until bot.py is recovered from the deployment.
  (railway ssh blocked by host-key verification — to retry later.)

## 2026-06-12 (evening) — fresh-link freshness check + honest mirror stamps

- Bug (user-reported): after a manual /backup the bot still handed out the OLD
  Drive link — the mirror map only refreshed on the daily cron. Fix `bb20e1d`:
  `drive_links_ts.json` records WHEN each guild's link was mirrored
  (stamped in `set_drive_links`); `_drive_link_is_fresh()` compares the newest
  local zip mtime against it, and `_latest_link()` + the `/latest` redirect
  fall back to the bot's own always-newest link while the mirror is behind.
  No local zips + no stamp = trust Drive (volume-wiped case).
- `60a778c` mirror_to_drive.sh: HAVE branch now post_links too (stamps "verified
  current as of now"), and the end-of-run BULK map POST is gone — it stamped
  every guild fresh at run END, wrongly covering backups that landed mid-run.
  rclone stderr is now captured into RETRY/FAIL lines (the 2 morning FAILs were
  undiagnosable; both healed on the next run).
- `4a2c320` review fixes: stale Drive link still returned when PUBLIC_DOMAIN is
  unset (beats no link); smoke test never rmtree's a pre-existing guild dir.
- VPS crontab: mirror now runs HOURLY at :30 (was daily 05:30) so Drive lags a
  backup by ≤1h; the freshness fallback covers the gap with a working link.
- tests/smoke_drive_links.py extended over every freshness branch — GREEN.

## 2026-06-12 — kill the ~12GB member cache (Railway cost fix)

- Bot averaged ~12 GB RSS (≈$5.3/day, the main driver of the $50-limit countdown).
  Cause: `intents.members` makes discord.py chunk + permanently cache EVERY member
  of EVERY guild at startup; cache was only read during backup snapshots.
- `bot.py`: Client now gets `chunk_guilds_at_startup=False` +
  `member_cache_flags=MemberCacheFlags.none()`. `guild.me` is safe (discord.py
  always caches the bot's own member — verified in 2.7.1 source). `/msg` already
  falls back to `fetch_members` when the cache is empty (the only cached member,
  the bot itself, is filtered out as a bot).
- `backup.py`: new `fetch_member_list()` pulls the full list over HTTP per backup
  (freed right after); `snapshot_members(members)` + `snapshot_roles(guild, members)`
  take it as a param — role member_ids now derived from the fetched list instead of
  the cache-backed `r.members`. members.json format unchanged (restore unaffected).
- Verified: py_compile + tests/smoke_drive_links.py green (real bot.py import =
  constructor args validated).

## 2026-06-11 — zipscan: auto-detect risky zips before Google flags them

- Context: Google flagged 4 guild zips on Drive ("malware & similar harmful content")
  because backups contained executable Discord attachments. The sanitize fix worked but
  its FLAGGED list was hard-coded — the NEXT guild with an .exe would get flagged first.
- `bot.py`: new admin action `cmd?do=zipscan[&guild=]` — opens each guild's newest zip
  server-side and reports members matching the risky set (.exe/.dll/.bat/.jar/… or
  archives under attachments/). Returns `risky_guilds` for the mirror job.
- `mirror_to_drive.sh`: FLAGGED now fetched LIVE from zipscan (static 4 only as
  fallback when the endpoint is unreachable; "NONE" sentinel distinguishes a clean
  fleet from a failed fetch).
- **Live result:** zipscan found **8** risky guilds — the 4 Google flagged **plus 4
  more** (1055387573452816415, 1130054024838782988, 1278427210025271377,
  1461292328252739768) that would have been flagged next. Sanitize mirror run launched
  (nohup) to clean them proactively.
- Commit `85ddefa`, deployed via `railway up` 14:45.

# Work Notes — Discord Backup Bot

## 2026-06-11 (later) — Drive malware flags + sanitizer; integration DEPLOYED

- Google flagged 4 mirrored zips ("malware & similar harmful content") — recipients
  blocked from those share links. Cause: Discord attachment members (.exe/.bat/.jar/.rar)
  inside the backups; Drive scans shared zips.
- Fix: `mirror_to_drive.sh` keeps a `FLAGGED` gid list; those guilds get a SANITIZED
  Drive copy (7z-strips `*.exe *.dll *.scr *.bat *.cmd *.msi *.vbs *.ps1 *.jar *.apk`),
  uploaded as a fresh object so the flag clears and the link shares again. Originals
  stay complete on the Railway volume. If Google flags another guild → add its gid
  to `FLAGGED`.
- Bot deployed via `railway up` (Drive-link integration from the morning session is
  now LIVE). Per user: do NOT mass-unshare links, do NOT auto-request Google review.

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
