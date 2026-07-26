# timeline-export

Export your Google Maps Timeline (the 2024+ on-device "Location History") to JSON —
fetched straight from Google and decoded locally.

The point of the project: recover Google's own **`placeId`** for every visit — its
building-level place match, which raw GPS coordinates cannot reproduce — plus semantic
classification, transport modes and trips, as machine-readable JSON.

Nothing here needs an Android device. An optional path via an Android container also
exists (see [Appendix: the container path](#appendix-the-container-path)) but is no longer
required for anything.

## Quick start

```bash
pip install 'httpx[http2]' cryptography gpsoauth

# 1. sign in normally (2FA and all), in your own browser, at:
#    https://accounts.google.com/embedded/setup/android?source=com.google.android.gms&xoauth_display_name=Android%20Device
pip install browser-cookie3        # lets step 1's cookie be picked up automatically
python3 get_token.py --email you@example.com --from-browser \
  --save-master ~/timeline-data/master.txt --out ~/timeline-data/tok.txt

# 2. one-time: fetch the decryption key with a browser (answer the password prompt)
python3 web_key.py --email you@example.com \
  --master-token-file ~/timeline-data/master.txt --headful -o ~/timeline-data/key.b64

# 3. fetch + decode + build records
./export_all.sh --cloud
```

`--from-browser` reads the `oauth_token` cookie your browser received — the same value you
would otherwise copy from DevTools (`--oauth-token-file` still works if you prefer that, or
use another browser with `--from-browser firefox`). The **sign-in itself is not automated**:
Google blocks automation-driven sign-in, and this tool does not try to get around that.

`--from-browser` reads a cookie store **on the same machine**. If your browser is
elsewhere — you signed in on your laptop but run this on a server, or the box is headless —
there is nothing local to read. Sign in wherever you like, copy the `oauth_token` value
across (DevTools → Application → Cookies → `accounts.google.com`), and use
`--oauth-token-file` instead. Only that one short-lived, single-use value needs to travel;
your password and second factor never leave the machine you signed in on.

No `--android-id` needed: the tokens bind to a 64-bit id, and one is generated on first
run and saved to `$TIMELINE_OUT/android_id` for reuse. Keep that file — every later token
exchange must present the same value. Pass `--android-id` explicitly only if you want to
bind to a device you already own.

Afterwards only step 3 is routine. The bearer from step 1 lasts about an hour and is
refreshed from the saved master token without the browser:

```bash
python3 get_token.py --email you@example.com \
  --master-token-file ~/timeline-data/master.txt --out ~/timeline-data/tok.txt
```

## Where output goes — and why it matters

Generated data (the fetched DB, exports, name caches, tokens, the key) is written to
**`$TIMELINE_OUT`, default `~/timeline-data` — deliberately outside the checkout.**

That is a safety property, not a preference. If a working copy holds real location data,
real values end up pasted into comments, docstrings and examples while you edit — which is
exactly how a real city and postcode once reached this repo's history. Keep the checkout
data-free and any example you write is necessarily synthetic, because the real values
simply are not there.

Before pushing, run `tools/precommit-scan.sh` (case-insensitive, scans history too, and
reads a git-ignored `.private-denylist` of your own terms).

⚠️ `key.b64` decrypts your entire location history and `master.txt` is a long-lived
account credential. Both are written mode 600; treat them like passwords. Revoke the
master token at <https://myaccount.google.com/device-activity>.

## How retrieval works

Google's own Play services fetch this data over a private gRPC service, and this client
speaks the same protocol. Wire shapes come from
[microG GmsCore](https://github.com/microg/GmsCore/pull/3331) (Apache-2.0), an
open-source clean-room implementation:

| | |
|---|---|
| endpoint | `geller-pa.googleapis.com` |
| RPC | `/geller.oneplatform.GellerService/BatchSync` (gRPC over HTTP/2) |
| corpus | `GellerDataType.ENCRYPTED_ONDEVICE_LOCATION_HISTORY` (79) |
| scope | `https://www.googleapis.com/auth/webhistory` |
| crypto | AES-256-GCM, 12-byte IV ‖ ciphertext+tag, 32-byte key |

The response carries `GellerElement`s whose payloads unwrap
`GellerAny → GellerE2eeElement → (decrypt) → ExternalDbSync → ExternalDbSnapshot → rows`;
the `semantic_segment` column of those rows is the protobuf the decoder reads.
`content-type` must be exactly `application/grpc` — `application/grpc+proto` returns 404.

**The key** lives in the `on_device_location_history` security domain. `web_key.py` gets it
with a browser: it drives the same `accounts.google.com/encryption/unlock/android` page
Play services uses, standing in a `window.mm` shim where Android has its WebView JS
bridge, and captures the material the page delivers via `setVaultSharedKeys`. Google
interposes a password re-auth challenge (not a lock-screen factor), so run `--headful` and
answer it. `extract_key.py` is an alternative if you have a device already enrolled in the
domain — it reads the same key from `files/folsom/shared/FolsomKeyStore.pb`, where this
domain stores it unwrapped.

The key is domain-wide and stable across devices; re-run only if Google rotates it (the
store records an epoch).

## Where the data comes from

**ODLH = On-Device Location History.** `geller_fetch.py` writes it as
`$TIMELINE_OUT/odlh-storage.db`; on an Android device Play services keeps the same store at
`/data/data/com.google.android.gms/databases/odlh-storage.db`. Table `semantic_segment_table`, one row per segment; the
`semantic_segment` column is a protobuf blob (Google's `SemanticSegment` message).
This is the same data Google's own *Export Timeline data* button emits — but that
button is a fragile SAF-picker UI flow (and is missing from some Maps builds), useless
for a pipeline. Reading the DB is the robust path.

`segment_type`: 1 = visit, 2 = activity, 3 = timelinePath (raw GPS, ~2 h buckets),
4 = trip.

## Output schema

Top-level `{"semanticSegments": [...]}`, matching Google's on-device export
(`visit` / `activity` / `timelinePath`). Times are ISO-8601 with the segment's UTC
offset; coordinates are `"lat°, lng°"` strings (E7 → degrees). *(Values below are
synthetic placeholders.)*

```jsonc
// visit — the valuable one
{
  "startTime": "2025-01-02T14:01:06.171+02:00",
  "endTime":   "2025-01-02T16:30:50.234+02:00",
  "startTimeTimezoneUtcOffsetMinutes": 120,
  "visit": {
    "probability": 0.81,
    "topCandidate": {
      "placeLocation": { "latLng": "12.3456789°, 98.7654321°" },
      "semanticType": "HOME",          // see caveats below
      "semanticTypeCode": 1,           // raw enum — authoritative
      "placeTypeCode": 100,            // Google place-type taxonomy id (uncategorised here)
      "probability": 0.72,
      "placeId": "ChIJ<base64url of FeatureId>",           // standard Google Maps placeId
      "placeUrl": "https://www.google.com/maps/place/?q=place_id:ChIJ<...>",
      "featureId": "0x<cellId hex>:0x<fprint hex>"
    }
  }
}
// activity: { start:{latLng}, end:{latLng}, distanceMeters,
//             topCandidate:{ type:"in passenger vehicle", typeCode:29, probability:0.99 } }
// timelinePath: [ { point:"lat°, lng°", durationMinutesOffsetFromStartTime:"59" }, ... ]
// trip: { name:"trip_<unix>" }
```

See `sample-output.json` for a complete synthetic example.

### placeId — how it's derived (the core feature)

The blob stores Google's internal **FeatureId** = `(cellId, fprint)`, two 64-bit ints.
The public `ChIJ…` placeId used by Maps URLs and the Places API is just those bytes
re-wrapped:

```
placeId = base64url( 0x0a 0x12  0x09 <cellId as little-endian fixed64>  0x11 <fprint LE fixed64> )
```

So `featureId` and `placeId` are the same identity in two encodings. Resolve a
`placeId` to a name/address/category with:
- a browser (logged in): open `placeUrl`;
- the **Places API** (needs a key): `GET places/{placeId}?fields=displayName,formattedAddress,types`;
- `?cid=` URL: `https://maps.google.com/?cid=<decimal fprint>`.

Names/categories are **not** cached on-device in any joinable form (checked — the only
exception is your manually-*saved* places in `gmm_myplaces.db`). So name resolution is a
separate enrichment step over the placeId — see **Resolving place names** below. Two
paths: a browser renderer (no API key) or the Places API (key).

### Caveats on `semanticType`

- `semanticTypeCode` is the raw protobuf enum — always trust it.
- `semanticType` label: `1 → HOME` is **confirmed** — its location matches the
  on-device HOME alias in `gmm_myplaces.db → sync_item` key `0:0`. The rest are
  best-effort: `0 = UNKNOWN`/inferred (the bulk of visits), `4 = INFERRED`
  (a frequently-visited place that is **not** the labelled WORK alias),
  `5 = SEARCHED_ADDRESS` (rare). `2 = WORK` per Google's convention (may not appear).
  The enum is Google's, so `1 = HOME` holds for any account.
- `placeTypeCode` (#1000) is Google's numeric place-type taxonomy (restaurant/park/…)
  — hundreds of codes, no public mapping table shipped here; emitted raw. It is **coarser
  than a category name** and is *not* a substitute for name resolution: measured against
  resolved categories, only a small minority of observed codes map to a single category ≥80% of the time (a few are reliable; many are not). Use it for grouping, not labelling.
- `visit.isConfirmed` is set when you explicitly confirmed the visit in Maps
  (`VisitProto.is_confirmed`) — a high-trust subset.
- `PlaceCandidate.top_place_type` (#7) exists but is **empty in practice** (empty on essentially every visit observed), so it is not emitted.

### Schema provenance

The field numbering here was originally derived from the raw protobuf wire format, then
cross-checked against **microG GmsCore's `segment.proto`** (Apache-2.0, [PR #3331](https://github.com/microg/GmsCore/pull/3331)),
an independent clean-room definition of the same message. They agree on
`LocationHistorySegmentProto{start_time=1, end_time=2, segment_data=3, is_deleted=4,
segment_id=6, finalization_status=9, display_mode=10, source=11, metadata=12}`, on the
`visit=1 / activity=2 / path=3` union, and on
`PlaceCandidate{feature_id=1, semantic_type=2, probability=3, location=5}`.

One documented disagreement: microG names fields **7/8** `hierarchy_level` /
`finalization_state`. Observed data contradicts that — they are consistently *equal to each other* and only ever the local standard-time or DST offset,
switching with the season. This decoder therefore reads them as start/end **UTC offset
minutes**, which is also what makes the emitted timestamps line up with reality.

Segments flagged `is_deleted` are **skipped** (you deleted them; they shouldn't reappear
in an export). Pass `--include-deleted` to keep them.

### Other decoding notes

- `timelinePath` buckets carry no UTC offset in the store; each inherits the nearest
  neighbouring segment's offset. The seed default is `+120`; change it in
  `odlh_export.py` (`last_off`) to your account's base offset if needed.
- `durationMinutesOffsetFromStartTime` is one byte per point; treated as minutes from
  the bucket start. A few large values suggest the unit isn't always plain minutes —
  the **points themselves are exact**, the per-point time is approximate.
- `--stats` prints per-type counts plus `errors` (decode failures are also logged to
  stderr). Note a blob that parses but is empty still counts as decoded, so `errors=0`
  means "nothing threw", not "everything was rich".

## Resolving place names

Turn placeIds into Google's real place names/addresses. Both paths resolve **by
placeId** (Google's own building-level identity) — never by reverse-geocoding
coordinates, which would throw away exactly the disambiguation this project exists for.

### Option A — headless browser, no API key (`resolve_names.js`)

Google serves place names only via client-side JS, so render the public place page in
headless Chromium and read the resolved `/maps/place/<NAME>/…!1s<ftid>…` URL. It
cross-checks the resolved ftid against the one requested, so a merged/moved place is
flagged, not mislabelled.

```bash
npm install                      # puppeteer-core (needs a system Chromium/Chrome)
./get_consent_cookie.sh $TIMELINE_OUT/consent_cookies.txt   # anonymous EU cookie-consent
node resolve_names.js "$TIMELINE_OUT/Timeline-latest.json" -o "$TIMELINE_OUT/Timeline-named.json"
```
Adds `placeName` + `placeAddress` to every visit; caches in `$TIMELINE_OUT/place_cache_browser.json`
(resumable). Env knobs: `CHROME_PATH`, `RESOLVE_DELAY_MS` (default 2500), `RESOLVE_LIMIT`
(0 = all). **Pace it** — rendering 300+ Maps pages fast from one IP can trip Google
throttling/CAPTCHA; the default delay is deliberately conservative, and the cache means
you resolve each place only once, ever. This is scraping, so it's on you to keep it
gentle and within Google's ToS.

### Option B — Places API, keyed (`place_names.py`)

Official, fast, richer (adds `placeCategory`/types). Needs a Google Maps Platform key
with *Places API (New)*; 300-odd cached lookups sit inside the free tier. See the script
header for setup. Use this if you want categories or a fully supported path.

Either way, resolution sends Google **its own placeIds** for your own visited places —
no new disclosure (unlike shipping coordinates to a third party).

## Comprehensive records (`build_records.py` → `Timeline-full.json`)

`export_all.sh` runs this after resolution to produce human-legible records — deterministic,
no LLM:

- **Enriched visits** — each visit's `topCandidate` gains `placeName`, a proper
  `placeAddress` (street/number/postcode, not the name repeated), `placeCategory`, and
  `placeLocation.{town,country,postalCode}` (town & country parsed from Google's own
  address, or taken structured from the Places API's `addressComponents`).
- **Rich trips** — a top-level `trips[]`, each with metadata, a `destination` (dominant
  away-town), a one-line human `description`, `stats` (stops / distanceKm / kmByMode),
  `topPlaces`, and the full **GPS `track`** assembled from the timelinePath in the trip
  window:

```jsonc
"trips": [{                                     // values below are illustrative placeholders
  "id": "...", "startDate": "2025-01-05", "endDate": "2025-01-08", "durationDays": 4,
  "destination": "Springfield, Exampleland",
  "description": "Jan 5–8, 2025 · 4 days · Springfield, Exampleland · 600 km (mostly car) · 15 stops: …",
  "stats": { "stops": 15, "distanceKm": 600.0,
             "kmByMode": {"in passenger vehicle": 590.0, "walking": 10.0}, "trackPoints": 300 },
  "topPlaces": [{ "name": "…", "town": "Springfield", "visits": 3, "hours": 48.0 }],
  "track": [[12.345678, 98.765432, "2025-01-05T12:08:00+02:00"], "…"]
}]
```

- **Movement segments** — a top-level `movements[]`, one per transport leg, linked to its
  origin and destination **places** (from the adjacent visits) with mode, distance,
  duration, speed, a human `description`, and its own GPS route:

```jsonc
"movements": [{
  "startTime": "…", "endTime": "…",
  "mode": "in passenger vehicle", "modeCode": 29, "speedKmh": 22.4,
  "distanceKm": 4.2, "durationMin": 12,
  "from": { "name": "Home", "town": "Springfield", "semanticType": "HOME" },
  "to":   { "name": "Central Park", "town": "Springfield" },
  "description": "12 min · car · 4.2 km · Home → Central Park",
  "track": [[12.345678, 98.765432, "…"], "…"]
}]
```

(The same `from`/`to`/`description` are also folded into each `activity` segment inline.)

## Travel-mode analysis (`travel_mode.py`)

Deterministic modal-split report over the activity segments — no LLM, no network:

```bash
python3 travel_mode.py "$TIMELINE_OUT/Timeline-latest.json"          # modal split + longest + cross-check
python3 travel_mode.py "$TIMELINE_OUT/Timeline-latest.json" --trips  # + per-trip mode breakdown
python3 travel_mode.py "$TIMELINE_OUT/Timeline-latest.json" --json    # machine-readable summary
```

Uses Google's own per-activity mode (`activity.topCandidate.type`/`typeCode`, emitted by
`odlh_export.py`) plus each segment's distance and duration. Reports km/time/count per
mode, the longest journeys, a per-trip breakdown, and an **independent speed cross-check**
that flags any move whose speed is implausible for its labelled mode (surfaces both
Google misclassifications and GPS-jitter artifacts — it's how the `code 7 = vehicle, not
cycling` label was pinned). Mode labels: `2/5/29` are speed-verified (walking/flying/
vehicle); the rest are best-effort over Google's raw enum, and `typeCode` is always kept.

## Appendix: the container path

Before the direct fetch existed, this project drove an Android container (redroid) to make
Play services sync the Timeline, then read the resulting `odlh-storage.db`. That still
works and needs no token or key, but it is slower, far heavier, and lags behind the cloud
by however long Google's backup/import cycle takes.

`setup.sh` automates the whole bring-up: a resolution-pinned redroid container, the
lmkd/display stability fixes it needs on a KVM-less host, optional Play Integrity
(`redroid/play_integrity.sh`), sign-in (`login.sh`), the backup import (`reimport.sh`,
fixed-coordinate taps verified against the GMS log), and a nightly refresh. `export_all.sh`
without `--cloud` decodes from the container.

redroid runs Android on the **host Linux kernel** and needs `binder`/`ashmem`, so the
container path is Linux-only; Docker Desktop on macOS/Windows will not run it. The
processing tools are pure Python/Node and run anywhere.

See `docs/microg-path.md` for notes on replacing that stack with microG, kept for
reference — the direct fetch made it unnecessary.

## Files

- `get_token.py` — browser sign-in → OAuth bearer for the Timeline scope.
- `web_key.py` — one-time: fetch the security-domain key with a browser.
- `extract_key.py` — one-time alternative: read it from an already-enrolled device.
- `geller_fetch.py` — fetch from Google → `odlh-storage.db`.
- `odlh_export.py` — decode that DB → `{"semanticSegments": [...]}` (stdlib only).
- `build_records.py` — enriched visits + movements + trips.
- `resolve_names.js` / `get_consent_cookie.sh` — place names via headless Chromium (no key).
- `place_names.py` — place names via the Places API (keyed).
- `travel_mode.py` — travel-mode / modal-split analyzer.
- `export_all.sh` — one command: fetch (or decode) → resolve → records.
- `setup.sh`, `login.sh`, `reimport.sh`, `fetch_and_export.sh`, `redroid/*` — the container path.
- `tools/precommit-scan.sh` — local leak scan before pushing.
- `sample-output.json` — synthetic example of the output schema.
