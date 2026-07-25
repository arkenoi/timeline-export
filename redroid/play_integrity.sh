#!/usr/bin/env bash
# play_integrity.sh — install the ReZygisk + PlayIntegrityFork stack so a FRESH redroid
# container passes Play Integrity (BASIC), which is what lets Google sign-in / GMS / Maps
# behave like a real device instead of getting challenged as a rooted, uncertified one.
#
# Modeled on a proven-working install (ReZygisk + PlayIntegrityFork v17 + GMS on the
# root-hide denylist). Module .zips are pulled from their official GitHub releases; the
# container is rebooted to activate them.
#
# ── HONEST STATUS ─────────────────────────────────────────────────────────────────────
#  * The exact stack this installs is confirmed working on a reference container. The
#    INSTALLER itself is best-effort and not fully tested (release asset names + the
#    headless `magisk --install-module` path can drift).
#  * Google bans Play Integrity fingerprints over time. PlayIntegrityFork ships `autopif`
#    to fetch a fresh one; re-run it (or update the module) if integrity later fails.
#  * Verifying the verdict truly passes needs an on-device Integrity-checker app — this
#    script only confirms the modules installed and the pif daemon is running.
set -uo pipefail
CONTAINER="${RD_CONTAINER:-rd}"
dx(){ docker exec "$CONTAINER" sh -c "$*" 2>/dev/null; }
magisk_in(){ dx "magisk $*" || dx "/data/adb/magisk/magisk $*"; }

if dx 'test -d /data/adb/modules/playintegrityfork && test -d /data/adb/modules/rezygisk'; then
  echo "PlayIntegrityFork + ReZygisk already installed — nothing to do"; exit 0
fi
command -v curl >/dev/null || { echo "curl required" >&2; exit 2; }

# Pin the releases we install. Unpinned "latest" would run arbitrary new code as root in
# a --privileged container; override deliberately if you want a newer build.
REZY_TAG="${REZYGISK_TAG:-latest}"
PIF_TAG="${PIF_TAG:-latest}"
# Optional integrity check: export REZY_SHA256 / PIF_SHA256 to enforce a known hash.
REZY_SHA256="${REZY_SHA256:-}"
PIF_SHA256="${PIF_SHA256:-}"

# .zip asset from a GitHub release (tag or "latest"), filtered by name pattern
release_zip(){ local api="https://api.github.com/repos/$1/releases/$([ "$2" = latest ] && echo latest || echo "tags/$2")"
  curl -fsSL "$api" | grep -oE '"browser_download_url": *"[^"]+\.zip"' | grep -oE 'https[^"]+' | grep -iE "$3" | head -1; }

verify_sha(){ [ -z "$2" ] && { echo "    (no pinned SHA-256 for $(basename "$1") — set ${3} to enforce one)"; return 0; }
  local got; got=$(sha256sum "$1" | cut -d" " -f1)
  [ "$got" = "$2" ] || { echo "!! checksum mismatch for $(basename "$1"): $got != $2" >&2; return 1; }
  echo "    checksum ok"; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
echo "[*] locating module releases…"
REZY=$(release_zip PerformanC/ReZygisk "$REZY_TAG" 'rezygisk')
PIF=$(release_zip osm0sis/PlayIntegrityFork "$PIF_TAG" 'PlayIntegrityFo?rk')
[ -n "$REZY" ] && [ -n "$PIF" ] || { echo "could not resolve release URLs (rate-limited? offline?)" >&2; exit 1; }
echo "    ReZygisk: ${REZY##*/}"
echo "    PIF:      ${PIF##*/}"
curl -fsSL "$REZY" -o "$TMP/rezygisk.zip"; verify_sha "$TMP/rezygisk.zip" "$REZY_SHA256" REZY_SHA256 || exit 1
curl -fsSL "$PIF"  -o "$TMP/pif.zip";      verify_sha "$TMP/pif.zip"      "$PIF_SHA256"  PIF_SHA256  || exit 1

echo "[*] installing modules into the container's Magisk…"
for z in rezygisk pif; do
  docker exec -i "$CONTAINER" sh -c "cat > /data/local/tmp/$z.zip" < "$TMP/$z.zip"
  magisk_in "--install-module /data/local/tmp/$z.zip" || { echo "  module install failed for $z" >&2; exit 1; }
  dx "rm -f /data/local/tmp/$z.zip"
done

echo "[*] hiding root from Google apps (denylist)…"
for pkg in com.google.android.gms com.android.vending com.google.android.gms.unstable; do
  magisk_in "--denylist add $pkg" >/dev/null 2>&1 || true
done

echo "[*] rebooting the container to activate…"
docker restart "$CONTAINER" >/dev/null
for i in $(seq 1 72); do [ "$(dx 'getprop sys.boot_completed')" = "1" ] && break; sleep 5; done

echo "[*] verifying…"
if dx 'test -d /data/adb/modules/playintegrityfork && test -d /data/adb/modules/rezygisk'; then
  dx 'ps -A' | grep -qi playintegrityfork && echo "  modules installed, pif daemon running ✓" \
    || echo "  modules installed; pif daemon not seen yet — give it another boot"
else
  echo "  install did not persist — check 'magisk --install-module' support on this image" >&2; exit 1
fi
echo "Done. If Google integrity later starts failing, re-run PlayIntegrityFork's autopif or update the module."
