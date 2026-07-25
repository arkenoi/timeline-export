#!/usr/bin/env bash
# get_consent_cookie.sh — obtain a Google "SOCS" consent cookie so the headless
# resolver can skip the EU cookie-consent interstitial. No login, no personal data:
# it just accepts the anonymous cookie-consent form. Output is a Netscape cookie jar.
#
# Usage:  ./get_consent_cookie.sh [JARFILE]      (default: out/consent_cookies.txt)
set -euo pipefail
JAR="${1:-out/consent_cookies.txt}"; mkdir -p "$(dirname "$JAR")"; rm -f "$JAR"
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36'
PROBE='https://www.google.com/maps?cid=1'
TMP="$(mktemp)"

curl -s -c "$JAR" -b "$JAR" -A "$UA" -L "$PROBE" -o "$TMP"
if ! grep -qi 'consent.google.com/save' "$TMP"; then
  # already consent-free (non-EU IP) — a dummy cookie is fine
  echo -e ".google.com\tTRUE\t/\tTRUE\t0\tSOCS\tCAI" >> "$JAR"
  echo "no consent wall on this IP; wrote placeholder -> $JAR"; rm -f "$TMP"; exit 0
fi
ESCS=$(grep -oiE 'name="escs" value="[^"]*"' "$TMP" | head -1 | sed -E 's/.*value="([^"]*)".*/\1/')
BL=$(grep -oiE 'name="bl" value="[^"]*"' "$TMP" | head -1 | sed -E 's/.*value="([^"]*)".*/\1/')
curl -s -c "$JAR" -b "$JAR" -A "$UA" -L \
  --data-urlencode gl=US --data-urlencode m=0 --data-urlencode app=0 --data-urlencode pc=m \
  --data-urlencode "continue=$PROBE" --data-urlencode x=6 --data-urlencode "bl=$BL" \
  --data-urlencode hl=en --data-urlencode src=1 --data-urlencode cm=2 \
  --data-urlencode "escs=$ESCS" --data-urlencode set_eom=true \
  'https://consent.google.com/save' -o /dev/null
rm -f "$TMP"
grep -qi SOCS "$JAR" && echo "SOCS consent cookie obtained -> $JAR" || { echo "FAILED to obtain SOCS cookie" >&2; exit 1; }
