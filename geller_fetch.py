#!/usr/bin/env python3
"""
geller_fetch.py — container-free Timeline retrieval.

Fetches your own Timeline directly from Google's Geller sync service and writes an
`odlh-storage.db`-compatible SQLite file, so the existing `odlh_export.py` decoder and the
whole downstream pipeline work unchanged — no Android container involved.

Protocol/крypto shapes are taken from microG GmsCore (Apache-2.0, PR #3331), which is a
public clean-room implementation of the same client:
  endpoint  https://geller-pa.googleapis.com
  rpc       /geller.oneplatform.GellerService/BatchSync   (gRPC over HTTP/2)
  corpus    GellerDataType.ENCRYPTED_ONDEVICE_LOCATION_HISTORY = 79
  crypto    AES-256-GCM, 12-byte IV || ciphertext+tag, 128-bit tag, 32-byte key
  payload   GellerElement.payload -> GellerAny -> GellerE2eeElement.encryptedData
            -> (decrypt) -> GellerAny(ExternalDbSync) -> ExternalDbSnapshot
            -> rows -> column "semantic_segment" -> LocationHistorySegmentProto

THIS SCRIPT PERFORMS NO AUTHENTICATION AND NO KEY EXTRACTION.
You supply both, from steps you run and approve yourself:
  --token   OAuth bearer with scope https://www.googleapis.com/auth/webhistory
  --key     base64 of the 32-byte AES-256 key for the
            "on_device_location_history" security domain
Neither is stored, logged, or echoed by this script.

Usage:
  python3 geller_fetch.py --token-file tok.txt --key-file key.b64 -o out/odlh-storage.db
  python3 odlh_export.py out/odlh-storage.db -o out/Timeline-latest.json --stats

Requires: httpx with HTTP/2  (pip install 'httpx[http2]')  and  cryptography.
"""
import argparse, base64, os, sqlite3, struct, sys

# ---------- minimal protobuf writer/reader (same wire discipline as odlh_export.py) ----------
def _v(n):
    out = b''
    while True:
        b = n & 0x7f; n >>= 7
        out += bytes([b | 0x80]) if n else bytes([b])
        if not n: return out

def tag(f, w): return _v((f << 3) | w)
def fld_varint(f, n): return tag(f, 0) + _v(n)
def fld_bytes(f, b):  return tag(f, 2) + _v(len(b)) + b
def fld_str(f, s):    return fld_bytes(f, s.encode())

def parse(buf):
    """field -> list of (wire, value); value is int or bytes."""
    out, i, n = {}, 0, len(buf)
    while i < n:
        t = 0; s = 0
        while True:
            x = buf[i]; i += 1; t |= (x & 0x7f) << s
            if not (x & 0x80): break
            s += 7
        f, w = t >> 3, t & 7
        if w == 0:
            v = 0; s = 0
            while True:
                x = buf[i]; i += 1; v |= (x & 0x7f) << s
                if not (x & 0x80): break
                s += 7
        elif w == 1: v = buf[i:i+8]; i += 8
        elif w == 5: v = buf[i:i+4]; i += 4
        elif w == 2:
            ln = 0; s = 0
            while True:
                x = buf[i]; i += 1; ln |= (x & 0x7f) << s
                if not (x & 0x80): break
                s += 7
            v = buf[i:i+ln]; i += ln
        else:
            raise ValueError(f'bad wire type {w}')
        out.setdefault(f, []).append((w, v))
    return out

def one(msg, f):
    v = msg.get(f)
    return v[0][1] if v else None

# ---------- request ----------
ODLH = 79   # GellerDataType.ENCRYPTED_ONDEVICE_LOCATION_HISTORY

def build_request(sync_token=None, client_id="SEMANTICLOCATION"):
    # SyncItem{ dataType=1, syncToken=2 }
    item = fld_varint(1, ODLH)
    if sync_token: item += fld_str(2, sync_token)
    # BatchSyncRequest{ items=1, clientId=2 }
    return fld_bytes(1, item) + fld_str(2, client_id)

def grpc_frame(msg):      return b'\x00' + struct.pack('>I', len(msg)) + msg
def grpc_unframe(buf):
    out, i = [], 0
    while i + 5 <= len(buf):
        comp = buf[i]; ln = struct.unpack('>I', buf[i+1:i+5])[0]; i += 5
        payload = buf[i:i+ln]; i += ln
        if comp: raise RuntimeError('compressed gRPC frame not supported')
        out.append(payload)
    return out

# ---------- crypto ----------
def decrypt(blob, key):
    """GellerE2eeElement.encryptedData = 12-byte IV || ciphertext+tag (AES-256-GCM)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv, ct = blob[:12], blob[12:]
    aead = AESGCM(key)
    for aad in (None, b''):          # microG tries both; AAD is empty-or-absent
        try: return aead.decrypt(iv, ct, aad)
        except Exception: continue
    raise RuntimeError('AES-GCM decrypt failed (wrong key, or format changed)')

# ---------- response walking ----------
def snapshots(resp_msg, key):
    """yield ExternalDbSnapshot field-maps from a BatchSyncResponse."""
    for _, item_b in resp_msg.get(1, []):                     # SyncItem(s)
        item = parse(item_b)
        for _, el_b in item.get(4, []) + item.get(5, []):      # mutations / deletions
            el = parse(el_b)
            any_b = one(el, 3)                                 # GellerAny payload
            if not any_b: continue
            ga = parse(any_b)
            type_url = (one(ga, 1) or b'').decode('utf8', 'replace')
            value = one(ga, 2) or b''
            if 'GellerE2eeElement' in type_url:
                e2 = parse(value)
                enc = one(e2, 1)
                if not enc: continue
                inner = parse(decrypt(enc, key))               # -> GellerAny
                type_url = (one(inner, 1) or b'').decode('utf8', 'replace')
                value = one(inner, 2) or b''
            if 'ExternalDbSync' not in type_url: continue
            sync = parse(value)
            snap_b = one(sync, 3)
            if snap_b: yield parse(snap_b)

def rows_of(snap):
    cols = [(c[1]).decode('utf8', 'replace') for c in snap.get(4, [])]
    for _, row_b in snap.get(5, []):
        row = parse(row_b)
        vals = []
        for _, v_b in row.get(1, []):
            sv = parse(v_b)
            if 1 in sv:   vals.append(sv[1][0][1])                              # int
            elif 3 in sv: vals.append(sv[3][0][1].decode('utf8', 'replace'))    # string
            elif 4 in sv: vals.append(sv[4][0][1])                              # bytes
            elif 5 in sv: vals.append(bool(sv[5][0][1]))                        # bool
            elif 2 in sv: vals.append(struct.unpack('<d', sv[2][0][1])[0])      # double
            else: vals.append(None)
        yield dict(zip(cols, vals))

# ---------- output: an odlh-storage.db the existing decoder understands ----------
SCHEMA = """CREATE TABLE IF NOT EXISTS semantic_segment_table(
 _id INTEGER PRIMARY KEY, timestamp_millis INTEGER NOT NULL DEFAULT 0,
 database_id INTEGER NOT NULL DEFAULT 0, origin_id INTEGER, segment_id TEXT NOT NULL UNIQUE,
 semantic_segment BLOB NOT NULL, obfuscated_gaia_id TEXT NOT NULL DEFAULT '',
 shown_in_timeline INTEGER, is_finalized INTEGER,
 start_timestamp_seconds INTEGER NOT NULL, end_timestamp_seconds INTEGER NOT NULL,
 segment_type INTEGER NOT NULL, hierarchy_level INTEGER, fprint INTEGER);"""

def write_db(path, records):
    con = sqlite3.connect(path); con.execute(SCHEMA)
    n = 0
    for r in records:
        try:
            con.execute(
                "INSERT OR REPLACE INTO semantic_segment_table"
                "(database_id,segment_id,semantic_segment,start_timestamp_seconds,"
                " end_timestamp_seconds,segment_type,hierarchy_level) VALUES (?,?,?,?,?,?,?)",
                (r.get('database_id') or 0, r['segment_id'], r['semantic_segment'],
                 r.get('start_timestamp_seconds') or 0, r.get('end_timestamp_seconds') or 0,
                 r.get('segment_type') or 0, r.get('hierarchy_level')))
            n += 1
        except Exception as e:
            print(f"  warn: row {r.get('segment_id')!r} skipped: {e}", file=sys.stderr)
    con.commit(); con.close(); return n

# ---------- main ----------
def read_secret(inline, path, what):
    if path:
        with open(path) as f: return f.read().strip()
    if inline: return inline.strip()
    sys.exit(f"error: {what} required (--{what} or --{what}-file)")

def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument("--token"); ap.add_argument("--token-file")
    ap.add_argument("--key");   ap.add_argument("--key-file")
    ap.add_argument("--sync-token", help="resume token from a previous run (incremental)")
    ap.add_argument("-o", "--out", default="out/odlh-storage.db")
    ap.add_argument("--raw", help="also dump the raw BatchSyncResponse here (debugging)")
    a = ap.parse_args()

    token = read_secret(a.token, a.token_file, "token")
    key = base64.b64decode(read_secret(a.key, a.key_file, "key"))
    if len(key) != 32: sys.exit(f"error: key must be 32 bytes (got {len(key)})")

    try:
        import httpx
    except ImportError:
        sys.exit("error: pip install 'httpx[http2]' cryptography")

    body = grpc_frame(build_request(a.sync_token))
    with httpx.Client(http2=True, timeout=60) as c:
        r = c.post("https://geller-pa.googleapis.com/geller.oneplatform.GellerService/BatchSync",
                   content=body,
                   headers={"content-type": "application/grpc+proto",
                            "authorization": f"Bearer {token}",
                            "te": "trailers"})
    status = r.headers.get("grpc-status", "0")
    if status != "0":
        sys.exit(f"gRPC error {status}: {r.headers.get('grpc-message','(no message)')}")
    if a.raw:
        with open(a.raw, "wb") as f: f.write(r.content)

    recs, snaps = [], 0
    for snap in snapshots(parse(grpc_unframe(r.content)[0]), key):
        snaps += 1
        table = (one(snap, 1) or b'').decode('utf8', 'replace')
        dbid = one(snap, 6)
        if table != 'semantic_segment_table': continue
        for row in rows_of(snap):
            if row.get('semantic_segment'):
                row.setdefault('database_id', dbid)
                recs.append(row)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    n = write_db(a.out, recs)
    print(f"wrote {a.out}: {n} segments from {snaps} snapshot(s)", file=sys.stderr)
    print("next: python3 odlh_export.py %s -o out/Timeline-latest.json --stats" % a.out, file=sys.stderr)

if __name__ == "__main__":
    main()
