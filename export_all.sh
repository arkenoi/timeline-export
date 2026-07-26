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
#   Output goes to $TIMELINE_OUT (default ~/timeline-data), never inside the checkout.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; cd "$HERE"
[ "${1:-}" = "--reimport" ] && { REIMPORT=1; shift; } || REIMPORT=0
# --cloud: fetch straight from Google (no Android container in the data path).
# Needs out/tok.txt (get_token.py) and out/key.b64 (extract_key.py, one-time).
[ "${1:-}" = "--cloud" ] && { CLOUD=1; shift; } || CLOUD=0
# Default output lives OUTSIDE the repo: a checkout must never hold real data,
# otherwise real values end up pasted into code comments and examples.
OUT="${1:-${TIMELINE_OUT:-$HOME/timeline-data}}"

[ "$REIMPORT" = "1" ] && { echo "[*] refreshing backup…"; ./reimport.sh || echo "  (reimport unverified — continuing with on-device data)"; }

if [ "$CLOUD" = "1" ]; then
  echo "[*] fetching from Google (container-free)…"
  [ -s "$OUT/tok.txt" ]  || { echo "!! $OUT/tok.txt missing — run get_token.py" >&2; exit 1; }
  [ -s "$OUT/key.b64" ] || { echo "!! $OUT/key.b64 missing — run extract_key.py once" >&2; exit 1; }
  python3 geller_fetch.py --token-file "$OUT/tok.txt" --key-file "$OUT/key.b64" \
    -o "$OUT/odlh-storage.db" || { echo "!! cloud fetch failed" >&2; exit 1; }
  TS="$(date +%Y%m%d-%H%M%S)"
  python3 odlh_export.py "$OUT/odlh-storage.db" -o "$OUT/Timeline-$TS.json" --stats || exit 1
  ln -sf "Timeline-$TS.json" "$OUT/Timeline-latest.json"
else
  echo "[*] decoding on-device Timeline…"
  ./fetch_and_export.sh "$OUT" >/dev/null || { echo "!! decode failed — not rebuilding records" >&2; exit 1; }
fi

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
