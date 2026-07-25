#!/usr/bin/env bash
# setup.sh — one-shot bring-up of the whole Timeline export pipeline on a clean machine.
# Idempotent: safe to re-run. Env overrides: RD_CONTAINER, RD_IMAGE, RD_VOLUME, RD_PORT.
#
# It: checks tools, starts a resolution-pinned redroid container, installs the lmkd/display
# stability fixes (host supervisor + in-container Magisk boot script), waits for boot,
# pauses for you to sign into Google, imports your Timeline backup, runs the first export,
# optionally resolves place names, and optionally installs a nightly refresh.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; cd "$HERE"
CONTAINER="${RD_CONTAINER:-rd}"
IMAGE="${RD_IMAGE:-redroid/redroid:14.0.0_mindthegapps_magisk}"
VOLUME="${RD_VOLUME:-rd_data}"
PORT="${RD_PORT:-5555}"
say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m!! %s\033[0m\n' "$*" >&2; exit 1; }
ask() { local p="$1" d="$2" a; read -r -p "$p [$d]: " a; echo "${a:-$d}"; }

# ── 0. prerequisites (check + report only; nothing exotic to install) ──────────────
say "Checking tools"
MISSING=""
for t in docker adb python3 sqlite3 curl; do command -v "$t" >/dev/null || MISSING="$MISSING $t"; done
BROWSER=""
for b in chromium-browser chromium google-chrome google-chrome-stable chrome; do command -v "$b" >/dev/null && { BROWSER="$b"; break; }; done
[ -n "$MISSING" ] && die "Missing:$MISSING  — install these (your package manager) and re-run."
echo "ok: docker adb python3 sqlite3 curl${BROWSER:+, browser=$BROWSER}${BROWSER:+ (name resolution available)}"

# ── 1. a few questions ─────────────────────────────────────────────────────────────
say "Options"
CONTAINER="$(ask 'Container name' "$CONTAINER")"
echo "Place-name resolution:  1) browser (no API key)   2) Places API key   3) skip"
RES="$(ask 'Choose 1/2/3' '1')"
APIKEY=""
if [ "$RES" = "1" ]; then
  { command -v node >/dev/null && command -v npm >/dev/null && [ -n "$BROWSER" ]; } || { echo "  node/npm/chromium not all present → falling back to skip"; RES=3; }
elif [ "$RES" = "2" ]; then
  read -r -p "  Paste Google Maps Platform API key (Places API New): " APIKEY
  command -v python3 >/dev/null || RES=3
fi
CRON="$(ask 'Install nightly auto-refresh (reimport+export)? y/n' 'y')"

# ── 2. container (resolution pinned so tap coordinates are portable) ────────────────
say "redroid container '$CONTAINER'"
if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = "true" ]; then
  echo "already running — reusing it"
else
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker volume create "$VOLUME" >/dev/null
  docker run -d --privileged --name "$CONTAINER" --cgroupns=host \
    --security-opt systempaths=unconfined \
    -v "$VOLUME:/data" -p "$PORT:5555" "$IMAGE" \
    androidboot.redroid_gpu_mode=guest \
    androidboot.redroid_width=720 androidboot.redroid_height=1280 androidboot.redroid_dpi=320 \
    >/dev/null || die "docker run failed"
  echo "started ($IMAGE, 720x1280@320, volume $VOLUME)"
fi
adb connect "localhost:$PORT" >/dev/null 2>&1 || true

# ── 3. host stability supervisor (systemd --user, or nohup fallback) ────────────────
say "Stability supervisor"
if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/redroid-stability@.service" <<EOF
[Unit]
Description=redroid stability supervisor (%i)
[Service]
Environment=RD_CONTAINER=%i
ExecStart=$HERE/redroid/redroid-stability.sh
Restart=always
RestartSec=10
[Install]
WantedBy=default.target
EOF
  chmod +x "$HERE/redroid/redroid-stability.sh"
  systemctl --user daemon-reload
  systemctl --user enable --now "redroid-stability@${CONTAINER}.service"
  echo "systemd unit redroid-stability@${CONTAINER} enabled"
else
  chmod +x "$HERE/redroid/redroid-stability.sh"
  pgrep -f "redroid-stability.sh" >/dev/null || RD_CONTAINER="$CONTAINER" nohup "$HERE/redroid/redroid-stability.sh" >/tmp/redroid-stability-$CONTAINER.log 2>&1 &
  echo "supervisor running via nohup (no systemd --user); log /tmp/redroid-stability-$CONTAINER.log"
fi

# ── 4. wait for boot, then persist the in-container fix ─────────────────────────────
say "Waiting for Android to boot (first boot can take a few minutes)"
for i in $(seq 1 120); do
  [ "$(docker exec "$CONTAINER" getprop sys.boot_completed 2>/dev/null)" = "1" ] && break; sleep 5
done
[ "$(docker exec "$CONTAINER" getprop sys.boot_completed 2>/dev/null)" = "1" ] || die "container did not boot"
echo "booted"
# populate Magisk busybox (some images ship /data/adb/magisk empty → service.d never runs)
docker exec "$CONTAINER" sh -c 'test -s /data/adb/magisk/busybox || cp /system/etc/init/magisk/busybox /data/adb/magisk/busybox 2>/dev/null; chmod 755 /data/adb/magisk/busybox 2>/dev/null' || true
docker exec -i "$CONTAINER" sh -c 'cat > /data/adb/service.d/99-redroid-stability.sh && chmod 755 /data/adb/service.d/99-redroid-stability.sh' < "$HERE/redroid/99-redroid-stability.sh" && echo "installed in-container boot script"

# ── 5. Google sign-in (your existing account) ──────────────────────────────────────
say "Sign in to your Google account"
if docker exec "$CONTAINER" dumpsys account 2>/dev/null | grep -q 'type=com.google'; then
  echo "a Google account is already signed in — good"
else
  LOGIN="$(ask 'Sign in: [a]utomated (enter credentials) or [m]anual on-screen?' 'a')"
  if [ "${LOGIN:0:1}" = "a" ] || [ "${LOGIN:0:1}" = "A" ]; then
    RD_CONTAINER="$CONTAINER" ./login.sh || echo "  automated login not confirmed — you can finish it on screen, then continue"
  else
    docker exec "$CONTAINER" am start -a android.settings.ADD_ACCOUNT_SETTINGS --es account_types com.google >/dev/null 2>&1 || true
    echo "  Complete the sign-in on the device (scrcpy localhost:$PORT, or the redroid display)."
    read -r -p "  Press Enter once you are signed in... " _
  fi
  docker exec "$CONTAINER" dumpsys account 2>/dev/null | grep -q 'type=com.google' \
    || echo "  (warning: no Google account detected yet — continuing anyway)"
  echo "  Make sure Google Maps is installed (Play Store) and Timeline is on."
fi

# ── 6. import the Timeline backup + first export ───────────────────────────────────
say "Importing your Timeline backup (drives Maps → Import; verifies via GMS log)"
chmod +x reimport.sh fetch_and_export.sh get_consent_cookie.sh export_all.sh build_records.py 2>/dev/null || true
./reimport.sh || cat <<EOF
  Automatic import could not be verified. Do it once by hand:
    Maps → profile → Your Timeline → cloud icon → Your backups → <your device> → ⋮ → Import
  then re-run: ./export_all.sh
EOF

# ── 7. comprehensive export (decode → resolve names → rich trip/visit records) ──────
say "Building comprehensive export (this resolves place names — can take a while first time)"
case "$RES" in
  2) GOOGLE_MAPS_API_KEY="$APIKEY" ./export_all.sh ;;
  3) RESOLVE=0 ./export_all.sh ;;
  *) ./export_all.sh ;;
esac

# ── 8. nightly refresh ─────────────────────────────────────────────────────────────
if [ "${CRON:0:1}" = "y" ] || [ "${CRON:0:1}" = "Y" ]; then
  say "Nightly auto-refresh"
  LINE="30 3 * * * cd $HERE && RD_CONTAINER=$CONTAINER ./export_all.sh --reimport >> out/refresh.log 2>&1"
  ( crontab -l 2>/dev/null | grep -v 'timeline-export.*export_all'; echo "$LINE" ) | crontab - && echo "cron installed: refresh + comprehensive export nightly at 03:30"
fi

# ── done ───────────────────────────────────────────────────────────────────────────
say "Done"
python3 -c "import json;d=json.load(open('out/Timeline-full.json'));s=d['semanticSegments'];ts=sorted(x['startTime'] for x in s);print(f'  export: {len(s)} segments, {len(d[\"trips\"])} trips, {ts[0][:10]} → {ts[-1][:10]}')" 2>/dev/null || true
cat <<EOF
  Pipeline is live. Comprehensive export → out/Timeline-full.json. Useful commands:
    ./export_all.sh --reimport                 # refresh backup + rebuild everything
    python3 travel_mode.py out/Timeline-latest.json --trips
  Outputs in out/ (git-ignored). See README.md for details.
EOF
