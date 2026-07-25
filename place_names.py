#!/usr/bin/env python3
"""
place_names.py — resolve each visit's placeId to Google's OWN name + address + category
via the Places API (New), and cache it for build_records.py.

Resolves BY placeId (Google's authoritative, building-level identity) — never by
reverse-geocoding coordinates. Returns the same cache schema as the no-key browser
resolver (keyed by featureId) so either can feed build_records.py: name, address,
category, town, country, adminArea, countryCode, postalCode.

Needs a Places API (New) key:
    export GOOGLE_MAPS_API_KEY=AIza...
    python3 place_names.py out/Timeline-latest.json -o out/Timeline-named.json

Cache: out/place_cache_api.json (resumable; ~300 one-time lookups sit in the free tier).
"""
import json, sys, os, time, argparse, urllib.request, urllib.error

FIELDS = "displayName,formattedAddress,addressComponents,primaryTypeDisplayName,types,businessStatus"
ENDPOINT = "https://places.googleapis.com/v1/places/"

def parse_components(comps):
    out = {}
    for c in comps or []:
        ty = c.get("types", [])
        if "country" in ty:
            out["country"] = c.get("longText"); out["countryCode"] = c.get("shortText")
        elif ("locality" in ty or "postal_town" in ty) and "town" not in out:
            out["town"] = c.get("longText")
        elif "administrative_area_level_1" in ty:
            out["adminArea"] = c.get("longText")
        elif "postal_code" in ty:
            out["postalCode"] = c.get("longText")
    out.setdefault("town", out.get("adminArea"))   # fall back to admin area if no locality
    return out

def resolve(place_id, key):
    req = urllib.request.Request(ENDPOINT + place_id,
        headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELDS})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.load(r)
        rec = {"name": j.get("displayName", {}).get("text"),
               "address": j.get("formattedAddress"),
               "category": j.get("primaryTypeDisplayName", {}).get("text"),
               "types": j.get("types", []),
               "businessStatus": j.get("businessStatus")}
        rec.update(parse_components(j.get("addressComponents")))
        return rec
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "detail": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:
        return {"error": str(e)}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("timeline_json")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--sleep", type=float, default=0.05)
    a = ap.parse_args()

    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    doc = json.load(open(a.timeline_json))
    segs = doc["semanticSegments"]
    outdir = os.path.dirname(os.path.abspath(a.out or a.timeline_json))
    cache_path = os.path.join(outdir, "place_cache_api.json")
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}

    # collect (featureId -> placeId); cache is keyed by featureId (same as browser path)
    pairs = {}
    for s in segs:
        tc = s.get("visit", {}).get("topCandidate", {})
        fid, pid = tc.get("featureId"), tc.get("placeId")
        if fid and pid and fid not in pairs:
            pairs[fid] = pid
    todo = [f for f in pairs if f not in cache]
    print(f"{len(pairs)} distinct places; {len(cache)} cached; {len(todo)} to resolve", file=sys.stderr)
    if todo and not key:
        print("ERROR: set GOOGLE_MAPS_API_KEY (Places API New).", file=sys.stderr); sys.exit(2)

    for i, fid in enumerate(todo, 1):
        cache[fid] = resolve(pairs[fid], key)
        if i % 25 == 0 or i == len(todo):
            json.dump(cache, open(cache_path, "w"), ensure_ascii=False, indent=1)
            print(f"  resolved {i}/{len(todo)}", file=sys.stderr)
        time.sleep(a.sleep)
    json.dump(cache, open(cache_path, "w"), ensure_ascii=False, indent=1)

    if a.out and a.out != "/dev/null":
        named = 0
        for s in segs:
            tc = s.get("visit", {}).get("topCandidate")
            if not tc or not tc.get("featureId"):
                continue
            r = cache.get(tc["featureId"])
            if r and r.get("name"):
                tc["placeName"] = r["name"]
                if r.get("address"): tc["placeAddress"] = r["address"]
                if r.get("category"): tc["placeCategory"] = r["category"]
                if r.get("town") or r.get("country"):
                    tc.setdefault("placeLocation", {})
                    for k in ("town", "country", "postalCode"):
                        if r.get(k): tc["placeLocation"][k] = r[k]
                named += 1
        json.dump(doc, open(a.out, "w"), ensure_ascii=False, indent=2)
        print(f"wrote {a.out}: {named} visits named", file=sys.stderr)

if __name__ == "__main__":
    main()
