#!/usr/bin/env python3
"""
build_records.py — turn the raw Timeline export + place-name cache into COMPREHENSIBLE
records: enriched place visits (name, proper address, town, country, category) and rich
trip records (metadata, human-readable description, stats, top places, full GPS track).

Deterministic, no LLM, no network — it only reads the export JSON and the place cache
(populated by resolve_names.js or place_names.py). Town/country are parsed from Google's
own formatted address (not independent reverse-geocoding).

Usage:  python3 build_records.py out/Timeline-latest.json -o out/Timeline-full.json
        (export_all.sh runs decode → resolve → this, as one command)
"""
import json, sys, re, argparse
from datetime import datetime
from collections import defaultdict, Counter

# ---------- location parsing (from Google's formatted address) ----------
_POSTAL = re.compile(r'^([A-Za-z]{1,3}[- ]?)?(\d{3,6})\s+(.+)$')
# trailing postcode: US 94043 / US ZIP+4 / UK 'NW1 6XE' / JP '100-0001' / CA 'K1A 0B1'
_POSTAL_TRAIL = re.compile(
    r'^(.*?)[, ]\s*('
    r'[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z][A-Z\d]'  # UK 'NW1 6XE' / CA 'M5E 1E5'
    r'|\d{3}-\d{4}'                            # JP
    r'|[A-Z]{0,2}[- ]?\d{4,6}(?:-\d{4})?'       # US/EU numeric (+ZIP+4)
    r')$', re.IGNORECASE)

def parse_location(addr):
    """'123 Main St, 90210 Springfield, Exampleland' -> {town:Springfield, country:Exampleland, postalCode:90210}"""
    if not addr:
        return {}
    parts = [p.strip() for p in addr.split(",") if p.strip()]
    if not parts:
        return {}
    loc = {"country": parts[-1]}
    if len(parts) >= 2:
        cand = parts[-2]
        m = _POSTAL.match(cand)                       # "90210 Springfield"  (postcode first)
        m_us = _POSTAL_TRAIL.match(cand)              # "CA 90210" / "Springfield 90210"
        if m:
            loc["postalCode"] = m.group(2); loc["town"] = m.group(3).strip()
        elif m_us:
            loc["postalCode"] = m_us.group(2)
            head = m_us.group(1).strip()
            # "CA 90210" -> state only; the real city is the preceding component
            if len(head) <= 3 and len(parts) >= 3:
                loc["adminArea"] = head; loc["town"] = parts[-3]
            else:
                loc["town"] = head or (parts[-3] if len(parts) >= 3 else None)
        else:
            loc["town"] = cand
    return loc

# ---------- helpers ----------
def dt(s): return datetime.fromisoformat(s)
def latlng(s):
    m = re.findall(r'-?\d+\.\d+', s or "")
    return (float(m[0]), float(m[1])) if len(m) >= 2 else None

MODE_SHORT = {"in passenger vehicle": "car", "flying": "air", "walking": "on foot",
              "cycling": "by bike", "running": "running"}

def fmt_min(h):
    m = round(h * 60)
    if m < 60: return f"{m} min"
    return f"{m//60} h {m%60} min" if m % 60 else f"{m//60} h"

def daterange(a, b):
    a, b = dt(a), dt(b)
    if a.year == b.year and a.month == b.month:
        return f"{a:%b} {a.day}–{b.day}, {a.year}" if a.day != b.day else f"{a:%b %-d, %Y}"
    if a.year == b.year:
        return f"{a:%b %-d} – {b:%b %-d, %Y}"
    return f"{a:%b %-d, %Y} – {b:%b %-d, %Y}"

# ---------- main ----------
def build(infile, outfile):
    doc = json.load(open(infile))
    segs = doc["semanticSegments"]
    outdir = __import__("os").path.dirname(__import__("os").path.abspath(outfile or infile))
    cache = {}
    for cf in ("place_cache_browser.json", "place_cache_api.json"):
        p = __import__("os").path.join(outdir, cf)
        if __import__("os").path.exists(p):
            cache.update({k: v for k, v in json.load(open(p)).items() if v})

    def place_of(fid):
        c = cache.get(fid) or {}
        loc = parse_location(c.get("address"))   # browser path: parse Google's address string
        return {"name": c.get("name"), "address": c.get("address"), "category": c.get("category"),
                # API path (place_names.py) provides these structured; else parse the address
                "town": c.get("town") or loc.get("town"),
                "country": c.get("country") or loc.get("country"),
                "postalCode": c.get("postalCode") or loc.get("postalCode")}

    # 1. enrich visits in place
    home_town = None
    for s in segs:
        tc = s.get("visit", {}).get("topCandidate")
        if not tc or not tc.get("featureId"):
            continue
        p = place_of(tc["featureId"])
        if p["name"]:      tc["placeName"] = p["name"]
        if p["address"]:   tc["placeAddress"] = p["address"]
        if p["category"]:  tc["placeCategory"] = p["category"]
        if p["town"] or p["country"]:
            tc["placeLocation"] = tc.get("placeLocation", {})
            if p["town"]:    tc["placeLocation"]["town"] = p["town"]
            if p["country"]: tc["placeLocation"]["country"] = p["country"]
            if p["postalCode"]: tc["placeLocation"]["postalCode"] = p["postalCode"]
        if tc.get("semanticType") == "HOME" and p["town"]:
            home_town = p["town"]

    visits = [s for s in segs if "visit" in s]
    acts   = [s for s in segs if "activity" in s]
    paths  = [s for s in segs if "timelinePath" in s]

    # flatten every timelinePath point to (epoch, [lat,lng,iso]) once, so both trips and
    # movements can slice their GPS track out of it by time window.
    import bisect
    allpts = []
    for p in paths:
        base = dt(p["startTime"])
        for pt in p["timelinePath"]:
            ll = latlng(pt.get("point"))
            if not ll: continue
            off = int(pt.get("durationMinutesOffsetFromStartTime", 0) or 0)
            ts = base.timestamp() + off * 60
            allpts.append((ts, [round(ll[0], 6), round(ll[1], 6), datetime.fromtimestamp(ts, base.tzinfo).isoformat()]))
    allpts.sort(key=lambda x: x[0])
    _pts_ts = [x[0] for x in allpts]
    def track_between(t0, t1):
        lo = bisect.bisect_left(_pts_ts, dt(t0).timestamp())
        hi = bisect.bisect_right(_pts_ts, dt(t1).timestamp())
        return [allpts[i][1] for i in range(lo, hi)]

    # 2. build rich trip records
    trips = []
    for s in segs:
        if "trip" not in s:
            continue
        t0, t1 = s["startTime"], s["endTime"]
        e0, e1 = dt(t0).timestamp(), dt(t1).timestamp()
        # compare instants, not ISO strings: offsets differ across a timezone-crossing trip
        inV = [v for v in visits if e0 <= dt(v["startTime"]).timestamp() <= e1]
        inA = [a for a in acts if e0 <= dt(a["startTime"]).timestamp() <= e1]
        inP = [p for p in paths if e0 <= dt(p["startTime"]).timestamp() <= e1]

        # destination = town with most dwell among trip visits, excluding home town
        dwell = defaultdict(float); place_hours = defaultdict(lambda: {"h": 0.0, "n": 0, "town": None})
        for v in inV:
            tc = v["visit"].get("topCandidate", {})
            fid = tc.get("featureId"); nm = tc.get("placeName")
            town = tc.get("placeLocation", {}).get("town")
            hrs = (dt(v["endTime"]) - dt(v["startTime"])).total_seconds() / 3600
            if town and town != home_town:
                dwell[(town, tc.get("placeLocation", {}).get("country"))] += hrs
            if nm:
                ph = place_hours[nm]; ph["h"] += hrs; ph["n"] += 1; ph["town"] = town
        dest = max(dwell, key=dwell.get) if dwell else None
        destination = (f"{dest[0]}, {dest[1]}" if dest and dest[1] else (dest[0] if dest else None))

        # stats
        km_by_mode = defaultdict(float)
        for a in inA:
            km_by_mode[a["activity"].get("topCandidate", {}).get("type", "unknown")] += (a["activity"].get("distanceMeters") or 0) / 1000
        total_km = sum(km_by_mode.values())
        dom_mode = max(km_by_mode, key=km_by_mode.get) if km_by_mode else None
        days = (dt(t1).date() - dt(t0).date()).days + 1

        top_places = sorted(place_hours.items(), key=lambda x: -x[1]["h"])[:5]
        top_names = [n for n, _ in top_places[:3]]

        # human description
        bits = [daterange(t0, t1), f"{days} day{'s' if days != 1 else ''}"]
        if destination: bits.append(destination)
        if total_km:    bits.append(f"{total_km:.0f} km" + (f" (mostly {MODE_SHORT.get(dom_mode, dom_mode)})" if dom_mode else ""))
        bits.append(f"{len(inV)} stops" + (f": {', '.join(top_names)}" if top_names else ""))
        description = " · ".join(bits)

        track = track_between(t0, t1)   # GPS track from timelinePath in the trip window

        rec = {
            "id": s["trip"].get("name"), "startTime": t0, "endTime": t1,
            "startDate": t0[:10], "endDate": t1[:10], "durationDays": days,
            "destination": destination, "description": description,
            "stats": {"stops": len(inV), "distanceKm": round(total_km, 1),
                      "kmByMode": {k: round(v, 1) for k, v in sorted(km_by_mode.items(), key=lambda x: -x[1])},
                      "trackPoints": len(track)},
            "topPlaces": [{"name": n, "town": d["town"], "visits": d["n"], "hours": round(d["h"], 1)} for n, d in top_places],
            "track": track,
        }
        trips.append(rec)
        # also fold summary (minus the big track) into the segment itself
        s["trip"].update({k: rec[k] for k in ("startDate", "endDate", "durationDays", "destination", "description", "stats", "topPlaces")})

    # 3b. rich transportation segments (movements): link each activity to the visit
    #     before (origin) and after (destination), and attach its own GPS route.
    vis_sorted = sorted(visits, key=lambda v: dt(v["startTime"]).timestamp())
    _vstart = [dt(v["startTime"]).timestamp() for v in vis_sorted]
    def brief(v):
        tc = v["visit"].get("topCandidate", {})
        return {k: val for k, val in {
            "name": tc.get("placeName"), "town": tc.get("placeLocation", {}).get("town"),
            "latLng": tc.get("placeLocation", {}).get("latLng"),
            "semanticType": tc.get("semanticType")}.items() if val is not None}
    def prev_visit(t):
        i = bisect.bisect_right(_vstart, dt(t).timestamp()) - 1
        return vis_sorted[i] if i >= 0 else None
    def next_visit(t):
        i = bisect.bisect_left(_vstart, dt(t).timestamp())
        return vis_sorted[i] if i < len(vis_sorted) else None

    movements = []
    for a in acts:
        act = a["activity"]; tc = act.get("topCandidate", {})
        dur_h = (dt(a["endTime"]) - dt(a["startTime"])).total_seconds() / 3600
        km = (act.get("distanceMeters") or 0) / 1000
        pv = prev_visit(a["startTime"]); nv = next_visit(a["endTime"])
        frm = brief(pv) if pv else ({"latLng": act.get("start", {}).get("latLng")} if act.get("start") else {})
        to  = brief(nv) if nv else ({"latLng": act.get("end", {}).get("latLng")} if act.get("end") else {})
        mode = tc.get("type", "unknown")
        desc = f"{fmt_min(dur_h)} · {MODE_SHORT.get(mode, mode)} · {km:.1f} km · {frm.get('name') or '?'} → {to.get('name') or '?'}"
        movements.append({
            "startTime": a["startTime"], "endTime": a["endTime"],
            "mode": mode, "modeCode": tc.get("typeCode"), "modeProbability": tc.get("probability"),
            "distanceKm": round(km, 2), "durationMin": round(dur_h * 60),
            "speedKmh": round(km / dur_h if dur_h > 0 else 0, 1),
            "from": frm, "to": to, "description": desc,
            "track": track_between(a["startTime"], a["endTime"]),
        })
        act["from"] = frm; act["to"] = to; act["description"] = desc   # inline enrichment

    out = {"semanticSegments": segs, "trips": trips, "movements": movements}
    json.dump(out, open(outfile, "w"), ensure_ascii=False, indent=2)
    resolved = sum(1 for s in visits if s["visit"].get("topCandidate", {}).get("placeAddress"))
    with_town = sum(1 for s in visits if s["visit"].get("topCandidate", {}).get("placeLocation", {}).get("town"))
    print(f"wrote {outfile}: {len(trips)} trips, {len(movements)} movements, {len(visits)} visits "
          f"({resolved} with address, {with_town} with town)", file=sys.stderr)
    return out

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("timeline_json")
    ap.add_argument("-o", "--out", default="out/Timeline-full.json")
    a = ap.parse_args()
    build(a.timeline_json, a.out)
