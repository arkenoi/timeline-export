#!/usr/bin/env python3
"""
travel_mode.py — deterministic travel-mode analyzer for the Timeline export.
No LLM, no network: pure arithmetic over activity segments.

Uses Google's own per-activity mode (activity.topCandidate.type / typeCode, emitted by
odlh_export.py) and the segment's distance + duration. Reports the modal split (how far
/ how long / how often by mode), the longest journeys, a per-trip breakdown, and an
independent speed cross-check that flags any activity whose speed is implausible for its
labelled mode.

Usage:
  python3 travel_mode.py out/Timeline-latest.json            # full report
  python3 travel_mode.py out/Timeline-latest.json --trips    # + per-trip breakdown
  python3 travel_mode.py out/Timeline-latest.json --json     # machine-readable summary
"""
import json, sys, argparse
from datetime import datetime
from collections import defaultdict

# plausible speed band (km/h) per mode — used only for the cross-check, not to relabel.
PLAUSIBLE = {
    "walking": (0, 12), "running": (4, 22), "cycling": (4, 45),
    "in passenger vehicle": (3, 170), "flying": (120, 1100), "unknown": (0, 1100),
}

def dt(s): return datetime.fromisoformat(s)

def parse(fn):
    doc = json.load(open(fn))
    acts, trips = [], []
    for s in doc["semanticSegments"]:
        if "activity" in s:
            a = s["activity"]; tc = a.get("topCandidate") or {}
            try:
                dur = (datetime.fromisoformat(s["endTime"]) - datetime.fromisoformat(s["startTime"])).total_seconds() / 3600
            except Exception:
                dur = 0
            km = (a.get("distanceMeters") or 0) / 1000
            acts.append({
                "start": s["startTime"], "end": s["endTime"], "hours": dur, "km": km,
                "mode": tc.get("type", "unknown"), "code": tc.get("typeCode"),
                "prob": tc.get("probability"), "kmh": (km / dur if dur > 0 else 0),
                "from": a.get("start", {}).get("latLng"), "to": a.get("end", {}).get("latLng"),
            })
        elif "trip" in s:
            trips.append({"name": s["trip"].get("name"), "start": s["startTime"], "end": s["endTime"]})
    return acts, trips

def median(xs):
    xs = sorted(xs)
    if not xs: return 0
    n = len(xs)
    return xs[n//2] if n % 2 else (xs[n//2 - 1] + xs[n//2]) / 2

def modal_split(acts):
    g = defaultdict(lambda: {"n": 0, "km": 0.0, "h": 0.0, "sp": []})
    for a in acts:
        m = g[a["mode"]]; m["n"] += 1; m["km"] += a["km"]; m["h"] += a["hours"]
        if a["kmh"] > 0: m["sp"].append(a["kmh"])
    return g

def fmt_dur(h):
    return f"{h:.0f}h" if h >= 1 else f"{h*60:.0f}m"

def report(fn, show_trips):
    acts, trips = parse(fn)
    if not acts:
        print("no activity segments found"); return
    tot_km = sum(a["km"] for a in acts); tot_h = sum(a["hours"] for a in acts)
    span = (min(a["start"] for a in acts)[:10], max(a["end"] for a in acts)[:10])
    g = modal_split(acts)

    print(f"TRAVEL-MODE ANALYSIS   {span[0]} → {span[1]}")
    print(f"{len(acts)} moves · {tot_km:,.0f} km · {tot_h:,.0f} h in motion\n")
    print(f"{'mode':<22} {'moves':>6} {'km':>9} {'% km':>6} {'time':>7} {'med km/h':>9}")
    for mode, m in sorted(g.items(), key=lambda x: -x[1]["km"]):
        print(f"{mode:<22} {m['n']:>6} {m['km']:>9,.0f} {100*m['km']/tot_km:>5.0f}% "
              f"{fmt_dur(m['h']):>7} {median(m['sp']):>9.1f}")

    print("\nLONGEST JOURNEYS")
    for a in sorted(acts, key=lambda x: -x["km"])[:8]:
        print(f"  {a['start'][:10]}  {a['km']:>6,.0f} km  {a['kmh']:>5.0f} km/h  {a['mode']:<22} {fmt_dur(a['hours'])}")

    # speed cross-check
    bad = []
    for a in acts:
        lo, hi = PLAUSIBLE.get(a["mode"], (0, 1e9))
        if a["kmh"] > 0 and not (lo <= a["kmh"] <= hi):
            bad.append(a)
    print(f"\nSPEED CROSS-CHECK: {len(bad)}/{len(acts)} moves outside their mode's plausible band")
    for a in sorted(bad, key=lambda x: -x["kmh"])[:6]:
        print(f"  {a['start'][:10]}  {a['mode']:<22} {a['kmh']:>5.0f} km/h  ({a['km']:.0f} km / {fmt_dur(a['hours'])})  code={a['code']}")

    if show_trips and trips:
        print("\nPER-TRIP MODE BREAKDOWN")
        for t in trips:
            _s, _e = dt(t["start"]).timestamp(), dt(t["end"]).timestamp()
            inside = [a for a in acts if _s <= dt(a["start"]).timestamp() <= _e]
            if not inside: continue
            by = defaultdict(float)
            for a in inside: by[a["mode"]] += a["km"]
            comp = ", ".join(f"{m} {k:.0f}km" for m, k in sorted(by.items(), key=lambda x: -x[1]))
            print(f"  {t['start'][:10]}→{t['end'][:10]}  {sum(by.values()):.0f}km: {comp}")

def summary_json(fn):
    acts, trips = parse(fn)
    g = modal_split(acts)
    out = {"totalKm": round(sum(a["km"] for a in acts), 1),
           "totalHours": round(sum(a["hours"] for a in acts), 1),
           "byMode": {m: {"moves": v["n"], "km": round(v["km"], 1), "hours": round(v["h"], 1),
                          "medianKmh": round(median(v["sp"]), 1)} for m, v in g.items()}}
    print(json.dumps(out, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("timeline_json")
    ap.add_argument("--trips", action="store_true", help="per-trip mode breakdown")
    ap.add_argument("--json", action="store_true", help="machine-readable summary")
    a = ap.parse_args()
    if a.json: summary_json(a.timeline_json)
    else: report(a.timeline_json, a.trips)
