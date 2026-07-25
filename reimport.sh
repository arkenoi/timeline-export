#!/usr/bin/env bash
# reimport.sh — headless, deterministic re-import of the cloud Timeline backup
# into the redroid container, with NO LLM/vision component.
#
# Why this exists: the new Google Timeline does not sync between devices; the only
# way to refresh the on-device store from your phone's ongoing cloud backup is the
# manual "Import" flow. This script drives that flow with fixed-coordinate taps
# (uiautomator is non-functional in redroid) and VERIFIES success by watching GMS's
# own LocationHistory log — so a missed tap fails loudly instead of silently.
#
# Cadence: phones back up ~daily (Wi-Fi + charging + idle), so importing more than
# once a day yields nothing new. A daily cron is plenty.
#
# Usage:  ./reimport.sh [--export]        (--export also runs fetch_and_export.sh)
# Exit:   0 = import completed (see "new segments" count); non-zero = could not verify.
set -uo pipefail

CONTAINER="${RD_CONTAINER:-rd}"
HERE="$(cd "$(dirname "$0")" && pwd)"
RETRIES="${RETRIES:-3}"
DO_EXPORT=0; [ "${1:-}" = "--export" ] && DO_EXPORT=1

# Tap targets for a 720x1280 screen with exactly ONE backup device.
# Re-check with `screencap` if Google changes the Maps layout or you add devices.
TAP_CLOUD="455 120"      # Timeline top bar: backup/cloud icon
TAP_OVERFLOW="648 915"   # ⋮ next to the (single) backup device row
TAP_IMPORT_MENU="615 804"  # "Import" item in the ⋮ popup
TAP_IMPORT_OK="528 1091"   # "Import" button in the confirm dialog

dx(){ docker exec "$CONTAINER" sh -c "$*" 2>/dev/null; }
tap(){ docker exec "$CONTAINER" input tap $1 2>/dev/null; }
log(){ echo "$(date '+%F %T') $*"; }

precheck(){
  [ "$(dx 'getprop sys.boot_completed')" = "1" ] || { log "FAIL: container not booted"; exit 3; }
  dx 'input keyevent KEYCODE_WAKEUP'
  # confirm the backup exists server-side before bothering
  :
}

drive_import(){
  dx 'logcat -c'                                   # isolate this run's log
  dx 'input keyevent KEYCODE_HOME'; sleep 1
  dx 'am start -a android.intent.action.VIEW -d "https://www.google.com/maps/timeline" com.google.android.apps.maps >/dev/null'
  sleep 7
  tap "$TAP_CLOUD";       sleep 5
  tap "$TAP_OVERFLOW";    sleep 2
  tap "$TAP_IMPORT_MENU"; sleep 3
  tap "$TAP_IMPORT_OK";   sleep 2
}

# Poll GMS LocationHistory log for the terminal signal; echo the #segments imported.
wait_for_result(){
  local deadline=$(( $(date +%s) + 40 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    local L; L="$(dx 'logcat -d -s LocationHistory:I' | grep -E 'BackupRunner|BackupPreprocessor|OdlhGellerState] Downloaded')"
    if echo "$L" | grep -q 'BackupRunner'; then
      # sum "inserting N segments" (0..N) and note downloaded elements
      local inserted downloaded
      inserted=$(echo "$L" | grep -oE 'inserting [0-9]+ segments' | grep -oE '[0-9]+' | awk '{s+=$1} END{print s+0}')
      downloaded=$(echo "$L" | grep -oE 'Downloaded [0-9]+ elements' | grep -oE '[0-9]+' | awk '{s+=$1} END{print s+0}')
      echo "$inserted $downloaded"
      return 0
    fi
    sleep 3
  done
  return 1
}

precheck
for attempt in $(seq 1 "$RETRIES"); do
  log "import attempt $attempt/$RETRIES ..."
  drive_import
  if res="$(wait_for_result)"; then
    read -r inserted downloaded <<<"$res"
    log "OK: import completed — downloaded=$downloaded elements, inserted=$inserted new segments"
    dx 'input keyevent KEYCODE_HOME'
    if [ "$DO_EXPORT" = "1" ]; then
      log "running export ..."
      "$HERE/fetch_and_export.sh" "$HERE/out"
    fi
    exit 0
  fi
  log "no completion signal (attempt $attempt) — the layout may have shifted; retrying"
done
log "FAIL: import could not be verified after $RETRIES attempts. Re-check tap targets with screencap."
exit 1
