#!/usr/bin/env python3
"""
odlh_export.py — Reproducible, headless export of the on-device Google Maps
Timeline (ODLH = On-Device Location History) to Google-Timeline-compatible JSON.

Reads GMS's odlh-storage.db (table semantic_segment_table) and emits a
{"semanticSegments": [...]} document matching the schema Google's own
"Export Timeline data" produces (visit / activity / timelinePath), with
coordinates as "lat°, lng°" strings and ISO-8601 times with UTC offset.

No UI automation, no network — just the SQLite file. See fetch_and_export.sh
for the end-to-end pull-from-container pipeline.
"""
import sqlite3, sys, struct, json, argparse
from datetime import datetime, timezone, timedelta

# ---------- protobuf wire reader (dependency-free) ----------
class PB:
    def __init__(self, b): self.b = b; self.i = 0; self.n = len(b)
    def varint(self):
        s = r = 0
        while True:
            x = self.b[self.i]; self.i += 1
            r |= (x & 0x7f) << s
            if not (x & 0x80): return r
            s += 7
    def parse(self):
        """field-number -> list of (wire, value) ; value is int (0/1/5) or bytes (2)."""
        out = {}
        while self.i < self.n:
            tag = self.varint(); f = tag >> 3; w = tag & 7
            if w == 0:   v = self.varint()
            elif w == 1: v = struct.unpack('<Q', self.b[self.i:self.i+8])[0]; self.i += 8
            elif w == 5: v = struct.unpack('<I', self.b[self.i:self.i+4])[0]; self.i += 4
            elif w == 2:
                ln = self.varint(); v = self.b[self.i:self.i+ln]; self.i += ln
            else:
                raise ValueError('bad wire type %d at %d' % (w, self.i))
            out.setdefault(f, []).append((w, v))
        return out

def fields(b):
    try: return PB(bytes(b)).parse()
    except Exception: return {}

def first(msg, fnum):
    """first (wire,value) for field, or None"""
    v = msg.get(fnum)
    return v[0] if v else None

def submsg(msg, fnum):
    x = first(msg, fnum)
    return fields(x[1]) if (x and x[0] == 2) else None

def i32_e7(v):
    """fixed32 E7 -> signed degrees"""
    if v >= 0x80000000: v -= 0x100000000
    return v / 1e7

def f32(v):
    return struct.unpack('<f', struct.pack('<I', v))[0]

def to_signed(v):
    """Protobuf sign-extends negative int32/int64 to a 10-byte varint, so a negative
    value arrives as a huge unsigned int. Western-hemisphere UTC offsets are negative —
    without this, timezone(timedelta(minutes=huge)) raises and the segment is dropped."""
    return v - (1 << 64) if v >= (1 << 63) else v

import base64
def chij_place_id(cell, fp):
    """FeatureId (cellId, fprint) -> standard Google Maps 'ChIJ...' placeId.
    placeId = base64url( {1:{1:fixed64 cell, 2:fixed64 fprint}} )."""
    feat = b'\x09' + struct.pack('<Q', cell) + b'\x11' + struct.pack('<Q', fp)
    return base64.urlsafe_b64encode(b'\x0a\x12' + feat).decode().rstrip('=')

def latlng_str(lat, lng):
    return f"{lat:.7f}°, {lng:.7f}°"

def point_from(msg):
    """msg with #1=latE7 fix32, #2=lngE7 fix32 -> 'lat°, lng°' or None"""
    if not msg: return None
    la, lo = first(msg, 1), first(msg, 2)
    if la and lo and la[0] == 5 and lo[0] == 5:
        return latlng_str(i32_e7(la[1]), i32_e7(lo[1]))
    return None

# ---------- time helpers ----------
def ts_of(msg):
    """{#1 seconds, #2 nanos} -> (seconds, nanos)"""
    if not msg: return None, 0
    s = first(msg, 1); ns = first(msg, 2)
    return (s[1] if s else None), (ns[1] if ns else 0)

def iso(seconds, nanos, offset_min):
    if seconds is None: return None
    tz = timezone(timedelta(minutes=offset_min or 0))
    dt = datetime.fromtimestamp(seconds, tz)
    if nanos:
        dt = dt.replace(microsecond=(nanos // 1000) % 1_000_000)
        base = dt.strftime('%Y-%m-%dT%H:%M:%S') + f".{nanos:09d}"[:4]
        return base + dt.strftime('%z')[:3] + ':' + dt.strftime('%z')[3:]
    z = dt.strftime('%z')
    return dt.strftime('%Y-%m-%dT%H:%M:%S') + z[:3] + ':' + z[3:]

# ---------- per-type decoders ----------
# semanticType enum -> label. Code 1=HOME is confirmed (its location matches the
# on-device HOME alias in gmm_myplaces.db, sync_item key "0:0"). Others are best-effort:
#  0 = inferred/unknown (the bulk of visits); 4 = a frequently-inferred place that is
#  NOT the labelled WORK alias; 5 = rare/scattered (searched addresses).
# Consumers who need certainty should use semanticTypeCode (the raw enum int).
SEMANTIC_TYPE = {0: "UNKNOWN", 1: "HOME", 2: "WORK", 4: "INFERRED", 5: "SEARCHED_ADDRESS"}

def dec_visit(p):
    """p = payload fields; visit detail at #3.#1"""
    d3 = submsg(p, 3)
    cand = submsg(d3, 1) if d3 else None
    out = {"probability": round(f32(first(cand,2)[1]),4)} if cand and first(cand,2) and first(cand,2)[0]==5 else {}
    top = submsg(cand, 4) if cand else None
    tc = {}
    if top:
        loc = point_from(submsg(top, 5))
        if loc: tc["placeLocation"] = {"latLng": loc}
        st = first(top, 2)                                  # #2 = semanticType enum
        if st and st[0] == 0:
            tc["semanticTypeCode"] = st[1]
            tc["semanticType"] = SEMANTIC_TYPE.get(st[1], f"CODE_{st[1]}")
        ptc = first(top, 1000)                              # #1000 = Google place-type taxonomy id
        if ptc and ptc[0] == 0 and ptc[1]: tc["placeTypeCode"] = ptc[1]
        pr = first(top, 3)
        if pr and pr[0] == 5: tc["probability"] = round(f32(pr[1]), 4)
        fid = submsg(top, 1)  # FeatureId — Google's building-level place match
        if fid and first(fid,1) and first(fid,1)[0]==1 and first(fid,2) and first(fid,2)[0]==1:
            # ODLH stores the two 64-bit halves in the REVERSE of Google's ftid/placeId
            # convention. Verified against gmm_myplaces (the saved-HOME ftid): the canonical
            # order is 0x{proto#2}:0x{proto#1}, i.e. cellId = odlh field #2, fprint = #1.
            cell, fp = first(fid,2)[1], first(fid,1)[1]
            pid = chij_place_id(cell, fp)
            tc["placeId"] = pid                                       # standard Google Maps placeId
            tc["placeUrl"] = f"https://www.google.com/maps/place/?q=place_id:{pid}"
            tc["featureId"] = f"0x{cell:016x}:0x{fp:016x}"            # internal id / for ?cid= lookup
    if tc: out["topCandidate"] = tc
    return {"visit": out}

# Activity-mode enum (activity detail #6.#1) -> label. Google's own classification.
# Codes 2/5/29 are speed-VERIFIED across this dataset (walking ~3.6, flying ~353,
# vehicle ~21 km/h median); the rest are best-effort over Google's raw enum — trust
# `typeCode` + the computed speed. High/low-count codes fold into the nearest class.
ACTIVITY_TYPE = {
    2: "walking", 6: "running", 11: "cycling",
    29: "in passenger vehicle", 7: "in passenger vehicle", 8: "in passenger vehicle",
    9: "in passenger vehicle", 30: "in passenger vehicle", 5: "flying", 0: "unknown",
}
# Label basis (speed cross-checked): 2=walking(~3.6), 6=running(~9, tight), 11=cycling
# (~15, caps ~27), 29/7/8/9/30=road vehicle (wide, to highway speed), 5=flying(~350).
# code 7 caps near highway speed (p90~78 km/h) so it is a vehicle class, NOT cycling.

def dec_activity(p):
    d3 = submsg(p, 3)
    a = submsg(d3, 2) if d3 else None
    out = {}
    if a:
        s = point_from(submsg(a, 1)); e = point_from(submsg(a, 2))
        if s: out["start"] = {"latLng": s}
        if e: out["end"] = {"latLng": e}
        dm = first(a, 3)
        if dm and dm[0] == 5:
            fv = f32(dm[1])
            if fv == fv and 0 <= fv < 1e8: out["distanceMeters"] = round(fv, 2)
        top = submsg(a, 6)  # topCandidate mode: {#1 enum, #2 probability}
        if top and first(top, 1) and first(top, 1)[0] == 0:
            code = first(top, 1)[1]
            tc = {"type": ACTIVITY_TYPE.get(code, f"code_{code}"), "typeCode": code}
            pr = first(top, 2)
            if pr and pr[0] == 5: tc["probability"] = round(f32(pr[1]), 4)
            out["topCandidate"] = tc
    return {"activity": out}

def _packed_e7(field):
    """A raw packed fixed32 E7 array stored in a length-delimited field."""
    if not field or field[0] != 2: return []
    bs = field[1]
    return [i32_e7(struct.unpack('<I', bs[i:i+4])[0]) for i in range(0, len(bs) - len(bs) % 4, 4)]

def dec_path(p, start_sec, offset_min):
    d3 = submsg(p, 3)
    inner = submsg(d3, 3) if d3 else None
    pts = []
    if inner:
        # #4 = packed latitudes (E7 fixed32), #5 = packed longitudes (E7 fixed32),
        # #6 = one byte per point = minute offset from the segment start.
        lats = _packed_e7(first(inner, 4))
        lngs = _packed_e7(first(inner, 5))
        off = first(inner, 6)
        # field 6 is one minute-offset per point. It is a packed varint array: values
        # <128 are a single byte (the common case), larger ones use continuation bytes.
        offs = []
        if off and off[0] == 2:
            bs = off[1]; k = 0
            while k < len(bs):
                v = 0; sh = 0
                while k < len(bs):
                    x = bs[k]; k += 1
                    v |= (x & 0x7f) << sh
                    if not (x & 0x80): break
                    sh += 7
                offs.append(v)
        m = min(len(lats), len(lngs))
        for k in range(m):
            e = {"point": latlng_str(lats[k], lngs[k])}
            if k < len(offs): e["durationMinutesOffsetFromStartTime"] = str(offs[k])
            pts.append(e)
    return {"timelinePath": pts}

def dec_trip(p, seg_id):
    return {"trip": {"name": seg_id}}

# ---------- main ----------
def build(db, include_paths=True):
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT segment_id, segment_type, start_timestamp_seconds, end_timestamp_seconds, semantic_segment "
        "FROM semantic_segment_table ORDER BY start_timestamp_seconds").fetchall()
    segs = []; stats = {1:0,2:0,3:0,4:0,'err':0,'other':0}
    # timelinePath rows store no UTC offset; carry the nearest neighbour's. Seed with
    # +120 (a standard-time offset — set to your timezone) so leading path rows aren't emitted as UTC; the first
    # offset-bearing row overwrites it.
    last_off = 120
    for seg_id, stype, ts0, ts1, blob in rows:
        try:
            p = fields(blob)
            offs = first(p, 7); offe = first(p, 8)
            # signed: negative offsets (the Americas) arrive sign-extended
            off0 = to_signed(offs[1]) if offs else last_off
            off1 = to_signed(offe[1]) if offe else off0
            if offs: last_off = off0
            st_s, st_ns = ts_of(submsg(p, 1))
            en_s, en_ns = ts_of(submsg(p, 2))
            st_s = st_s if st_s is not None else ts0
            en_s = en_s if en_s is not None else ts1
            seg = {"startTime": iso(st_s, st_ns, off0), "endTime": iso(en_s, en_ns, off1),
                   "startTimeTimezoneUtcOffsetMinutes": off0, "endTimeTimezoneUtcOffsetMinutes": off1}
            if stype == 1: seg.update(dec_visit(p)); stats[1]+=1
            elif stype == 2: seg.update(dec_activity(p)); stats[2]+=1
            elif stype == 3:
                if not include_paths: continue
                seg.update(dec_path(p, st_s, off0)); stats[3]+=1
            elif stype == 4: seg.update(dec_trip(p, seg_id)); stats[4]+=1
            else: stats['other']+=1; continue
            segs.append(seg)
        except Exception as ex:
            stats['err'] += 1
            print(f"  warn: segment {seg_id} (type {stype}) failed to decode: {ex}", file=sys.stderr)
            continue
    return segs, stats

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("-o", "--out", default="-")
    ap.add_argument("--no-paths", action="store_true", help="omit high-volume timelinePath segments")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    segs, stats = build(a.db, include_paths=not a.no_paths)
    doc = {"semanticSegments": segs}
    out = json.dumps(doc, ensure_ascii=False, indent=2)
    if a.out == "-": sys.stdout.write(out + "\n")
    else:
        open(a.out, "w").write(out)
        print(f"wrote {a.out}: {len(segs)} segments", file=sys.stderr)
    if a.stats:
        print(f"stats: visits={stats[1]} activities={stats[2]} paths={stats[3]} trips={stats[4]} errors={stats['err']}", file=sys.stderr)
