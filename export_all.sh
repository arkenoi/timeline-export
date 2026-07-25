#!/usr/bin/env bash
# export_all.sh — ONE command → comprehensive Timeline records.
#   decode on-device DB → resolve place names/addresses (cached) → build enriched
#   visits + rich trip records (metadata, human description, GPS track).
# Output: out/Timeline-full.json  (and the intermediate out/Timeline-latest.json).
#
# Name resolution auto-selects: Places API if GOOGLE_MAPS_API_KEY is set, else the
# headless-browser resolver if node+chromium are present, else skip (records still build,
# minus names). Everything is cached, so only new places cost anything on re-runs.
#
# Usage:  ./export_all.sh [OUTDIR]              (default out)
#         RESOLVE=0 ./export_all.sh             (skip name resolution)
#         ./export_all.sh --reimport           (pull a fresh backup first)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; cd "$HERE"
[ "${1:-}" = "--reimport" ] && { REIMPORT=1; shift; } || REIMPORT=0
OUT="${1:-out}"

[ "$REIMPORT" = "1" ] && { echo "[*] refreshing backup…"; ./reimport.sh || echo "  (reimport unverified — continuing with on-device data)"; }

echo "[*] decoding on-device Timeline…"
./fetch_and_export.sh "$OUT" >/dev/null

if [ "${RESOLVE:-1}" = "1" ]; then
  if [ -n "${GOOGLE_MAPS_API_KEY:-}" ]; then
    echo "[*] resolving place names via Places API…"
    python3 place_names.py "$OUT/Timeline-latest.json" -o /dev/null || echo "  (API resolve failed — check key)"
  else
    BROWSER=""; for b in chromium-browser chromium google-chrome google-chrome-stable chrome; do command -v "$b" >/dev/null && { BROWSER="$b"; break; }; done
    if [ -n "$BROWSER" ] && command -v node >/dev/null && command -v npm >/dev/null; then
      echo "[*] resolving place names via headless browser (no key)…"
      [ -d node_modules ] || npm install >/dev/null 2>&1
      ./get_consent_cookie.sh "$OUT/consent_cookies.txt" >/dev/null 2>&1 || true
      CHROME_PATH="$(command -v "$BROWSER")" node resolve_names.js "$OUT/Timeline-latest.json" -o /dev/null 2>>"$OUT/resolve.log" || echo "  (browser resolve incomplete — see $OUT/resolve.log; resumable)"
    else
      echo "[*] no API key and no node+chromium — skipping name resolution"
    fi
  fi
fi

echo "[*] building comprehensive records…"
python3 build_records.py "$OUT/Timeline-latest.json" -o "$OUT/Timeline-full.json"
echo "[*] done → $OUT/Timeline-full.json"
