#!/usr/bin/env bash
# fetch_and_export.sh — reproducible, headless Google Maps Timeline export.
#
# Pulls the on-device location-history store (odlh-storage.db) out of the redroid
# container and decodes it to Google-Timeline-compatible JSON. No UI automation,
# no network, no clicking. Run it any time the on-device Timeline has the data you
# want (e.g. after importing the backup once via the Maps UI).
#
# Usage:  ./fetch_and_export.sh [OUTDIR]     (default OUTDIR = ./out)
set -euo pipefail

CONTAINER="${RD_CONTAINER:-rd}"
DB_PATH="/data/data/com.google.android.gms/databases/odlh-storage.db"
OUTDIR="${1:-./out}"
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$OUTDIR"

echo "[*] container: $CONTAINER"
if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
  echo "!! container '$CONTAINER' is not running" >&2; exit 1
fi

# 1. Checkpoint any WAL so the copy is consistent, then stream the DB out.
#    (docker cp fails on this image's overlay; docker exec cat is reliable.)
echo "[*] pulling $DB_PATH ..."
docker exec "$CONTAINER" sh -c "command -v sqlite3 >/dev/null && sqlite3 '$DB_PATH' 'PRAGMA wal_checkpoint(TRUNCATE);' >/dev/null 2>&1 || true"
docker exec "$CONTAINER" cat "$DB_PATH" > "$OUTDIR/odlh-storage.db"
for ext in -wal -shm; do
  docker exec "$CONTAINER" sh -c "test -f '${DB_PATH}${ext}' && cat '${DB_PATH}${ext}'" > "$OUTDIR/odlh-storage.db${ext}" 2>/dev/null || rm -f "$OUTDIR/odlh-storage.db${ext}"
done
sz=$(wc -c < "$OUTDIR/odlh-storage.db" | tr -d ' ')   # POSIX; works on GNU + BSD/macOS
echo "[*] got odlh-storage.db (${sz} bytes)"
if [ "$sz" -lt 100000 ]; then
  echo "!! DB is small (<100KB) — the on-device Timeline may be empty." >&2
  echo "   Import the backup first: Maps > Your Timeline > cloud icon > Your backups > <your device> > Import." >&2
fi

# 2. Decode to JSON.
TS="$(date +%Y%m%d-%H%M%S)"
OUT="$OUTDIR/Timeline-${TS}.json"
python3 "$HERE/odlh_export.py" "$OUTDIR/odlh-storage.db" -o "$OUT" --stats
ln -sf "$(basename "$OUT")" "$OUTDIR/Timeline-latest.json"
echo "[*] wrote $OUT"
echo "[*] symlink: $OUTDIR/Timeline-latest.json"
