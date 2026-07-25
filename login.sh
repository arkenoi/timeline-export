#!/usr/bin/env bash
# login.sh — sign into your existing Google account on the container.
# The user only does three things: type email, type password, confirm on their phone.
# The script drives the on-device sign-in itself (the user never touches adb/scrcpy).
# It signs into an existing account only — it never creates one.
#
# Credentials come from $GMAIL_USER / $GMAIL_PASS or a prompt. The password is typed into
# the device via the input pipeline — run only on a device you control.
#
# Technical caveats (not shown to the user): Google challenges automated sign-in on
# rooted / non-Play-certified devices (redroid is both), so a CAPTCHA is possible; the
# script verifies the result and reports if it couldn't confirm. `adb input text` handles
# spaces but not every exotic symbol. Tap targets assume the 720x1280 sign-in screen.
set -uo pipefail
CONTAINER="${RD_CONTAINER:-rd}"
dx(){ docker exec "$CONTAINER" sh -c "$*" 2>/dev/null; }
tap(){ docker exec "$CONTAINER" input tap $1 2>/dev/null; }
typetext(){ docker exec "$CONTAINER" input text "$(printf '%s' "$1" | sed 's/ /%s/g')" 2>/dev/null; }
signed_in(){ dx 'dumpsys account' | grep -q 'type=com.google'; }

FIELD_INPUT="360 632"   # the text field on the sign-in screen (email, then password)
TAP_NEXT="590 1112"     # the "Next" button

if signed_in; then echo "Already signed in."; exit 0; fi

echo "Sign in to Google"
EMAIL="${GMAIL_USER:-}"; [ -z "$EMAIL" ] && read -r -p "  Email:    " EMAIL
PASS="${GMAIL_PASS:-}";  [ -z "$PASS" ]  && { read -r -s -p "  Password: " PASS; echo; }
{ [ -z "$EMAIL" ] || [ -z "$PASS" ]; } && { echo "email and password required" >&2; exit 2; }

dx 'input keyevent KEYCODE_WAKEUP'
dx 'am start -a android.settings.ADD_ACCOUNT_SETTINGS --es account_types com.google >/dev/null'
sleep 9
tap "$FIELD_INPUT"; sleep 1; typetext "$EMAIL"; sleep 1; tap "$TAP_NEXT"; sleep 7
tap "$FIELD_INPUT"; sleep 1; typetext "$PASS";  sleep 1; tap "$TAP_NEXT"; sleep 7
unset PASS

echo
read -r -p "  Approve the sign-in on your phone if Google asks, then press Enter… " _

for i in $(seq 1 18); do signed_in && { echo "  Signed in ✓"; exit 0; }; sleep 5; done
echo "  Couldn't confirm the sign-in — Google may have shown an extra challenge." >&2
echo "  Re-run, or complete it once on the device; the container remembers it afterwards." >&2
exit 1
