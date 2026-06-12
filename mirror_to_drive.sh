#!/usr/bin/env bash
# Mirror every guild's latest backup to Google Drive, ONE tidy file per server
# named by its guild id (gdrive:backupdiscord/by-guild/<gid>.zip), make each a
# shareable "anyone with the link" URL, and write a gid->link map the bot reads.
# Streamed via rclone copyurl — no local disk used. Re-runnable (overwrites).
set -uo pipefail

# Single-instance lock so a cron tick can't overlap a run already in flight.
exec 9>/tmp/mirror_to_drive.lock
flock -n 9 || { echo "another mirror run is active — exiting"; exit 0; }

DOWNLOAD_SECRET="10037853c9c398165248dbc481c8c2cb"
ADMIN_SECRET="75af3a234fb3e17f63e060633b55f37f"
BASE="https://backup-bot-production.up.railway.app"
DEST="gdrive:backupdiscord/by-guild"
MAP="/home/khaled/projects/discord-backup-bot/drive_links.json"

# Live guild list from the bot itself, so servers it joins later mirror too.
GUILDS=($(curl -s --max-time 30 "$BASE/admin/$ADMIN_SECRET/ping" \
  | python3 -c "import json,sys;print(' '.join(str(g['id']) for g in json.load(sys.stdin)['guilds']))" 2>/dev/null))
if [ "${#GUILDS[@]}" -eq 0 ]; then
  echo "ABORT — could not fetch guild list from $BASE/admin/…/ping"
  exit 1
fi
echo "mirroring ${#GUILDS[@]} guilds"

# Guilds whose newest zip contains members Google Drive flags as malware
# (executable Discord attachments — .exe/.bat/.jar/... — or nested archives).
# For these guilds we mirror a SANITIZED copy — same backup minus those member
# types — uploaded as a fresh Drive object so it scans clean and stays shareable.
# The complete originals stay on the Railway volume (restore uses those).
# The list comes LIVE from the bot's zipscan admin action, so newly risky guilds
# are sanitized BEFORE Google ever flags them; the static list is only a fallback
# for when the endpoint is unreachable (e.g. old bot build mid-deploy).
FLAGGED=$(curl -sf --max-time 120 "$BASE/admin/$ADMIN_SECRET/cmd?do=zipscan" \
  | python3 -c "import json,sys;g=json.load(sys.stdin)['risky_guilds'];print(' '.join(g) or 'NONE')" 2>/dev/null)
if [ -z "${FLAGGED:-}" ]; then
  FLAGGED="1378900499025367145 1512116155085488128 1512203310596362313 1512234194124800213"
  echo "zipscan unavailable — falling back to static flagged list"
elif [ "$FLAGGED" = "NONE" ]; then
  FLAGGED=""
fi
echo "guilds needing sanitized mirror: ${FLAGGED:-none}"
# Executables AND nested archives — Drive scans inside .rar/.zip members and
# kept flagging the cleaned zips until archive members were stripped too.
STRIP_TYPES='*.exe *.dll *.scr *.bat *.cmd *.msi *.vbs *.ps1 *.jar *.apk attachments/*.zip attachments/*.rar attachments/*.7z attachments/*.tar attachments/*.gz attachments/*.iso'

ok=0; fail=0; total=0
declare -A LINKS
# Push one link to the bot immediately (endpoint merges). Without this, a run
# dying between "deletefile old object" and the end-of-run map POST leaves the
# bot 302-redirecting to a dead Drive link until the next cron pass.
post_link() {  # $1=gid $2=link
  curl -s --max-time 30 -X POST -H 'Content-Type: application/json' \
       -d "{\"$1\": \"$2\"}" "$BASE/admin/$ADMIN_SECRET/set_drive_links" >/dev/null 2>&1 || true
}
for gid in "${GUILDS[@]}"; do
  tok=$(python3 -c "import hmac,hashlib;print(hmac.new(b'$DOWNLOAD_SECRET',b'$gid',hashlib.sha256).hexdigest()[:24])")
  # raw=1: get the zip bytes even after the bot starts 302-redirecting /latest
  # to the Drive mirror (otherwise we'd re-upload Drive's HTML viewer page).
  url="$BASE/latest/$tok/$gid?raw=1"
  # Status BEFORE size: a 404 error body has a non-zero content-length, which used
  # to slip past the size check and burn 3 retries on a guild with no snapshot.
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -I "$url")
  if [ "$code" = "404" ]; then
    echo "SKIP  $gid  (no backup snapshot yet — kicking one for the next run)"
    curl -s --max-time 30 -X POST "$BASE/admin/$ADMIN_SECRET/backup?guild=$gid" >/dev/null 2>&1 || true
    fail=$((fail+1)); continue
  fi
  size=$(curl -sIL --max-time 30 "$url" | awk 'tolower($0) ~ /^content-length/ {v=$2} END{gsub(/\r/,"",v); print v+0}')
  if [ "${size:-0}" -le 0 ]; then
    echo "SKIP  $gid  (no backup yet / 404)"; fail=$((fail+1)); continue
  fi
  have=$(rclone lsjson "$DEST/$gid.zip" 2>/dev/null | python3 -c "import json,sys;d=json.load(sys.stdin);print(d[0]['Size'] if d else 0)" 2>/dev/null)
  if [[ " $FLAGGED " == *" $gid "* ]]; then
    tmp="/tmp/clean-$gid.zip"
    echo "CLEAN $gid  (rebuilding Drive copy without executable members)"
    if ! curl -sf --max-time 1800 -o "$tmp" "$url"; then
      echo "FAIL  $gid  (download for sanitize failed)"; fail=$((fail+1)); rm -f "$tmp"; continue
    fi
    set -f   # pass the *.exe patterns to 7z literally, never shell-expanded
    7z d -tzip "$tmp" $STRIP_TYPES -r >/dev/null 2>&1
    set +f
    csize=$(stat -c%s "$tmp" 2>/dev/null || echo 0)
    if [ "${have:-0}" -eq "$csize" ]; then
      echo "HAVE  $gid  (clean copy already current)"
    else
      rclone deletefile "$DEST/$gid.zip" >/dev/null 2>&1   # fresh object id → fresh scan, flag clears
      if ! rcerr=$(rclone copyto "$tmp" "$DEST/$gid.zip" --drive-chunk-size 64M 2>&1 >/dev/null); then
        echo "FAIL  $gid  (clean upload failed: $(echo "$rcerr" | grep -m1 'ERROR\|Failed\|error' || echo "$rcerr" | tail -1))"
        fail=$((fail+1)); rm -f "$tmp"; continue
      fi
    fi
    rm -f "$tmp"
    link=$(rclone link "$DEST/$gid.zip" 2>/dev/null)
    [ -n "$link" ] && { LINKS[$gid]="$link"; post_link "$gid" "$link"; }
    ok=$((ok+1)); total=$((total+csize))
    continue
  fi
  if [ "${have:-0}" -eq "$size" ]; then
    echo "HAVE  $gid  (already on Drive, same size)"
    link=$(rclone link "$DEST/$gid.zip" 2>/dev/null)
    # post_link here too: it stamps the guild's mirrored-at time on the bot, so
    # the bot knows the Drive copy is current as of NOW (freshness check).
    [ -n "$link" ] && { LINKS[$gid]="$link"; post_link "$gid" "$link"; }
    ok=$((ok+1)); total=$((total+size)); continue
  fi
  echo "PUSH  $gid  ($(python3 -c "print(f'{$size/1048576:.0f} MB')")) → $DEST/$gid.zip"
  pushed=0
  for attempt in 1 2 3; do   # stream drops are transient — retry with back-off
    if rcerr=$(rclone copyurl "$url" "$DEST/$gid.zip" --drive-chunk-size 64M 2>&1 >/dev/null); then
      pushed=1; break
    fi
    echo "RETRY $gid  (attempt $attempt failed: $(echo "$rcerr" | grep -m1 'ERROR\|Failed\|error' || echo "$rcerr" | tail -1))"
    sleep $((attempt * 20))
  done
  if [ "$pushed" -eq 1 ]; then
    link=$(rclone link "$DEST/$gid.zip" 2>/dev/null)
    [ -n "$link" ] && { LINKS[$gid]="$link"; post_link "$gid" "$link"; }
    ok=$((ok+1)); total=$((total+size))
  else
    echo "FAIL  $gid  (rclone error after 3 attempts)"; fail=$((fail+1))
  fi
done

# Emit the gid->link JSON map (ops record only — the bot is updated per-guild
# via post_link at the moment each Drive copy is verified/uploaded, so its
# mirrored-at timestamps stay honest. A single end-of-run bulk POST would stamp
# every guild "fresh as of run END", wrongly covering backups that landed
# mid-run).
{
  printf '{\n'
  first=1
  for gid in "${!LINKS[@]}"; do
    [ $first -eq 1 ] || printf ',\n'; first=0
    printf '  "%s": "%s"' "$gid" "${LINKS[$gid]}"
  done
  printf '\n}\n'
} > "$MAP"

echo "------------------------------------------------------------"
echo "uploaded=$ok  skipped/failed=$fail  total=$(python3 -c "print(f'{$total/1073741824:.2f} GiB')")"
echo "wrote map: $MAP ($(python3 -c "import json;print(len(json.load(open('$MAP'))))" 2>/dev/null) links)"
rclone size "$DEST" 2>/dev/null
