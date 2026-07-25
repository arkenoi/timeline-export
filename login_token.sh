#!/usr/bin/env bash
# login_token.sh — EXPERIMENTAL WebView-free sign-in via a Google master token (gpsoauth).
# The alternate path to login.sh: no sign-in screen, so no CAPTCHA to trip. It signs into
# your EXISTING account (never creates one).
#
# You provide: email + a Google APP PASSWORD (create at myaccount.google.com/apppasswords;
# requires 2-Step Verification). The script fetches a master token (gpsoauth) and writes
# the account into the device AccountManager DB; GMS then bootstraps its own tokens.
#
# ── STATUS: EXPERIMENTAL, untested on this image ──────────────────────────────────────
# This is the standard headless-Android method, but modern full GMS (Android 14 +
# Play Integrity / AccountManager tamper checks) may refuse a manually-injected account
# for Maps/Timeline. If it doesn't take, use ./login.sh (the tested WebView path). It
# REFUSES to run if an account already exists, so it can't clobber a working login.
set -uo pipefail
CONTAINER="${RD_CONTAINER:-rd}"
DB=/data/system_ce/0/accounts_ce.db
dx(){ docker exec "$CONTAINER" sh -c "$*" 2>/dev/null; }

dx 'dumpsys account' | grep -q 'type=com.google' && { echo "an account already exists — refusing (use ./login.sh to add another)"; exit 0; }
command -v python3 >/dev/null || { echo "python3 required" >&2; exit 2; }
command -v sqlite3 >/dev/null || { echo "host sqlite3 required (container's crashes on this DB)" >&2; exit 2; }
python3 -c 'import gpsoauth' 2>/dev/null || { echo "installing gpsoauth…"; python3 -m pip install --quiet --user gpsoauth || { echo "pip install gpsoauth failed" >&2; exit 2; }; }

EMAIL="${GMAIL_USER:-}"; [ -z "$EMAIL" ] && read -r -p "  Email: " EMAIL
APPPW="${GMAIL_APP_PASSWORD:-}"; [ -z "$APPPW" ] && { read -r -s -p "  App password: " APPPW; echo; }
{ [ -z "$EMAIL" ] || [ -z "$APPPW" ]; } && { echo "email and app password required" >&2; exit 2; }
AID="$(dx 'settings get secure android_id' | tr -cd 'a-f0-9')"
[ -z "$AID" ] && { echo "could not read device android_id" >&2; exit 2; }

echo "  fetching master token…"
MASTER="$(GU="$EMAIL" GP="$APPPW" AID="$AID" python3 - <<'PY'
import os, sys, gpsoauth
r = gpsoauth.perform_master_login(os.environ['GU'], os.environ['GP'], os.environ['AID'])
t = r.get('Token')
if not t: sys.stderr.write("  master login failed: %s\n" % (r.get('Error') or r)); sys.exit(1)
sys.stdout.write(t)
PY
)" || { echo "  couldn't get a master token — check the app password (and that 2-Step is on)." >&2; exit 1; }
unset APPPW

echo "  registering the account (framework restart so AccountManager re-reads)…"
META=$(dx "stat -c '%u %g %a' $DB"); read -r UID GID MODE <<<"$META"
CTX=$(dx "ls -Z $DB 2>/dev/null" | awk '{print $1}')
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT

dx 'stop'; sleep 4                                            # stop the framework
docker exec "$CONTAINER" cat "$DB" > "$WORK/db"
# INSERT the account (token passed via a 600-mode SQL file, never printed/argv'd)
umask 077; printf "INSERT OR REPLACE INTO accounts(name,type,password) VALUES('%s','com.google','%s');\n" "$EMAIL" "$MASTER" > "$WORK/sql"
unset MASTER
sqlite3 "$WORK/db" < "$WORK/sql"
docker exec -i "$CONTAINER" sh -c "cat > $DB" < "$WORK/db"
dx "chown ${UID}:${GID} $DB; chmod ${MODE} $DB; { command -v restorecon >/dev/null && restorecon $DB; } || chcon '$CTX' $DB 2>/dev/null || true"
dx 'start'                                                    # start the framework

echo "  waiting for boot + GMS to adopt the account…"
for i in $(seq 1 40); do
  [ "$(dx 'getprop sys.boot_completed')" = "1" ] && dx 'dumpsys account' | grep -q "$EMAIL" && { echo "  account registered ✓ (verify Maps/Timeline actually work — GMS may still gate it)"; exit 0; }
  sleep 5
done
echo "  the account did not register — modern full GMS likely rejected the manual injection." >&2
echo "  use ./login.sh (WebView) instead; it's the tested path." >&2
exit 1
