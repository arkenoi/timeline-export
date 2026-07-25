# timeline-export

Reliable, **headless, reproducible** export of a Google Maps Timeline (the 2024+
on-device "Location History") to JSON — decoded directly from the device's
`odlh-storage.db`, with **no UI clicking** and no network.

Runs against a redroid Android container. The point of the project: recover Google's
own **`placeId`** for every visit — Google's building-level place match that raw GPS
tracks can't reproduce — plus semantic classification, and dump it as machine-readable
JSON on demand.

## Quick start (one script)

On a clean machine with `docker`, `adb`, `python3`, `sqlite3`, `curl` (and `node`+`npm`+
a Chromium for no-key name resolution):

```bash
git clone <this repo> && cd timeline-export
./setup.sh
```

`setup.sh` is idempotent and does the whole bring-up: checks tools, starts a
resolution-pinned redroid container, installs the lmkd/display stability fixes (host
supervisor + in-container Magisk boot script + busybox), waits for boot, signs you into
Google (automated with your credentials, or manually on-screen), imports your Timeline
backup, runs the first
export, optionally resolves place names, and optionally installs a nightly refresh. It
asks three things: container name, name-resolution method (browser / API key / skip), and
whether to install the cron. Re-run it any time — it detects and reuses existing state.

The sections below document what it wires up, and the day-to-day commands.

### Platform: redroid needs a Linux host

redroid runs Android on the **host Linux kernel** and requires the `binder`/`ashmem`
kernel modules, so the container step only works on Linux (bare metal or a Linux VM with
those modules). **Docker Desktop on macOS/Windows won't run it** — its LinuxKit VM lacks
binder/ashmem. Options off Linux:
- run redroid inside a Linux VM (UTM/Lima/multipass/VirtualBox; use the `arm64` image on
  Apple Silicon) and run `setup.sh` there, or on a remote/cloud Linux box;
- or skip the container entirely and point the **processing half** at an
  `odlh-storage.db` pulled from *any* Linux redroid or a rooted Android phone —
  `odlh_export.py` / `resolve_names.js` / `place_names.py` / `travel_mode.py` are pure
  Python/Node and run natively on macOS/Windows/Linux.

## Usage

```bash
./export_all.sh                # ONE command: decode → resolve names → comprehensive records
                               #   → out/Timeline-full.json (enriched visits + rich trips)
./export_all.sh --reimport     # refresh from the cloud backup first
./fetch_and_export.sh [OUTDIR] # just decode the on-device DB → out/Timeline-latest.json
```

Produces `OUTDIR/Timeline-<timestamp>.json` (+ a `Timeline-latest.json` symlink) and
keeps a copy of the raw `odlh-storage.db`. Re-run any time; it's idempotent.

To decode a DB you already have:
```bash
python3 odlh_export.py path/to/odlh-storage.db -o Timeline.json --stats
python3 odlh_export.py odlh-storage.db --no-paths -o visits_activities_only.json
```

Requirements: `docker` (running the container), `python3` (stdlib only), and
`sqlite3` on the host. Container name defaults to `rd`; override with `RD_CONTAINER=...`.

## Where the data comes from

`/data/data/com.google.android.gms/databases/odlh-storage.db`  ← **ODLH = On-Device
Location History** (GMS). Table `semantic_segment_table`, one row per segment; the
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
  — hundreds of codes, no public mapping table shipped here; emitted raw.

### Other decoding notes

- `timelinePath` buckets carry no UTC offset in the store; each inherits the nearest
  neighbouring segment's offset. The seed default is `+120`; change it in
  `odlh_export.py` (`last_off`) to your account's base offset if needed.
- `durationMinutesOffsetFromStartTime` is one byte per point; treated as minutes from
  the bucket start. A few large values suggest the unit isn't always plain minutes —
  the **points themselves are exact**, the per-point time is approximate.
- Self-check printed by `--stats`: per-type counts and `errors=0`. The decoder also
  keeps coordinates as signed E7 so out-of-region data would show up as wild lat/lng.

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
./get_consent_cookie.sh          # accept the anonymous EU cookie-consent → out/consent_cookies.txt
node resolve_names.js out/Timeline-latest.json -o out/Timeline-named.json
```
Adds `placeName` + `placeAddress` to every visit; caches in `out/place_cache_browser.json`
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
python3 travel_mode.py out/Timeline-latest.json          # modal split + longest + cross-check
python3 travel_mode.py out/Timeline-latest.json --trips  # + per-trip mode breakdown
python3 travel_mode.py out/Timeline-latest.json --json    # machine-readable summary
```

Uses Google's own per-activity mode (`activity.topCandidate.type`/`typeCode`, emitted by
`odlh_export.py`) plus each segment's distance and duration. Reports km/time/count per
mode, the longest journeys, a per-trip breakdown, and an **independent speed cross-check**
that flags any move whose speed is implausible for its labelled mode (surfaces both
Google misclassifications and GPS-jitter artifacts — it's how the `code 7 = vehicle, not
cycling` label was pinned). Mode labels: `2/5/29` are speed-verified (walking/flying/
vehicle); the rest are best-effort over Google's raw enum, and `typeCode` is always kept.

## Login (one-time, manual — do NOT automate)

Sign in to **your own** Google account inside your container, interactively, once:

```bash
adb shell am start -a android.settings.ADD_ACCOUNT_SETTINGS --es account_types com.google
```

then complete the sign-in on the device screen (via `scrcpy localhost:5555` or the
redroid display), and make sure Google Maps is installed.

Login is deliberately **not scripted**: it goes through a WebView with 2FA/passkey and
Google's abuse detection, so automating keystrokes is fragile and can get your account
flagged. It's a once-per-container manual step.

Where credentials live: the account and its OAuth tokens are stored by Google Play
services **inside the container's `/data` volume** (accounts DB + GMS token stores) —
**never in this repo**. Treat that volume like a secret: don't commit it, don't publish
the image or a snapshot of it.

## Refreshing the data (headless, no LLM) — `reimport.sh`

The new Google Timeline does **not** sync between devices; the only way to pull your
phone's ongoing cloud backup into this device is the manual **Import** flow. That flow
is a fixed sequence of taps, so it scripts deterministically:

```bash
./reimport.sh            # drive Import, verify via GMS log
./reimport.sh --export   # ... then run fetch_and_export.sh
```

`reimport.sh` launches the Timeline deep link and taps the fixed targets
(cloud icon → device ⋮ → Import → confirm), then **verifies success by watching
GMS's own log** (`LocationHistory: [BackupRunner] … restored` /
`[BackupPreprocessor] inserting N segments`). It reports how many new segments came
in and exits non-zero if it can't confirm — so a missed tap fails loudly and retries
rather than silently doing nothing. Cron it daily (phones back up ~daily):

```cron
# refresh timeline + export every night at 03:30 (log is git-ignored)
30 3 * * *  cd $HOME/timeline-export && ./reimport.sh --export >> out/reimport.log 2>&1
```

The script is **device-name agnostic**: it taps the ⋮ by *position* on the single
backup row, so it works whatever your phone is called (Pixel, Galaxy, whatever) —
nothing matches a device name. `uiautomator` is non-functional in redroid (empty dumps
even on static screens), so element-by-text tapping isn't available; fixed coordinates
are the fallback.

## Drift & robustness (read before distributing)

The fragile part is the four tap coordinates. Mitigations, in order of importance:

1. **Self-verification** (built in): the GMS `LocationHistory` log is the source of
   truth. If Google moves the UI, no `BackupRunner` line appears, `reimport.sh`
   retries then exits 1 — drift is detected, never silent.
2. **Pin the resolution.** Coordinates assume **720×1280 @ 320 dpi** (redroid's
   default; verify with `adb shell wm size`). Launch the container with those
   `redroid_width/height/dpi` and the coords are identical for every checkout.
   Different resolution ⇒ re-measure (`screencap`).
3. **Single backup device.** The ⋮ target assumes exactly one device under
   "Your backups". With several, adjust `TAP_OVERFLOW` (they stack vertically).
4. Coordinates live in labelled variables at the top of `reimport.sh` — the only
   thing to touch after a Maps redesign.

Deeper (not implemented): the Import button ultimately invokes GMS's
`OnDemandBackupRestoreOperation` (restore=true) via the `semanticlocationhistory`
bound service (`com.google.android.gms/.chimera.GmsApiService`). A custom client that
binds and sends that request would be fully UI-independent, but it means reproducing a
GMS protobuf API + auth — heavier and fragile in its own way. The tap flow is the
pragmatic, working choice.

## Reproduce from scratch (what `setup.sh` automates)

`./setup.sh` does all of the below for you; this is the manual equivalent / reference.
Nothing here bundles an image or personal data — you start from the **public** redroid
image, a **fresh empty** `/data`, and log into your own account.

1. **Container** — pinned resolution, and `/data` in a **named docker volume** (kept
   out of the repo tree, so account tokens can't be committed):
   ```bash
   docker volume create rd_data
   docker run -d --privileged --name rd --cgroupns=host \
     -v rd_data:/data -p 5555:5555 \
     redroid/redroid:14.0.0_mindthegapps_magisk \
     androidboot.redroid_gpu_mode=guest \
     androidboot.redroid_width=720 androidboot.redroid_height=1280 androidboot.redroid_dpi=320
   adb connect localhost:5555
   ```
   On a KVM-less host, redroid needs the lmkd/display stability fixes or GMS-heavy
   screens wedge adbd — see the companion `redroid-stability` unit.
2. **Sign in** — the manual one-time step (see **Login** above).
3. **Import the backup once** (bootstraps the on-device store): `./reimport.sh`
   — or by hand: Maps → *Your Timeline* → cloud icon → *Your backups* → your device →
   ⋮ → **Import**.
4. **Export**: `./fetch_and_export.sh` → `out/Timeline-latest.json`.
5. **Automate**: cron `./reimport.sh --export` (see above).

⚠️ The on-device store lives only in GMS app data (the `/data` volume) — clearing Play
services or deleting the volume destroys it; re-import to rebuild.

## Files

- `setup.sh` — one-shot bring-up of the whole pipeline on a clean machine.
- `login.sh` — best-effort automated Google sign-in for a fresh container (optional).
- `export_all.sh` — one command: decode → resolve names → comprehensive records.
- `build_records.py` — deterministic builder: enriched visits + rich trip records.
- `redroid/redroid-stability.sh` — host supervisor (lmkd watchdog + display keep-awake).
- `redroid/99-redroid-stability.sh` — in-container Magisk boot script (persistent half).
- `odlh_export.py` — the decoder (stdlib-only protobuf reader → JSON).
- `fetch_and_export.sh` — pull DB from container + decode. Export entry point.
- `reimport.sh` — headless re-import of the cloud backup (fixed taps + GMS-log verify).
- `resolve_names.js` — resolve placeIds → names via headless Chromium (no API key).
- `get_consent_cookie.sh` — fetch the consent cookie the browser resolver needs.
- `place_names.py` — resolve placeIds → names/categories via the Places API (keyed).
- `travel_mode.py` — deterministic travel-mode / modal-split analyzer.
- `package.json` — node dep (`puppeteer-core`) for `resolve_names.js`.
- `sample-output.json` — synthetic example of the output schema.
- `.gitignore` — keeps generated files and `node_modules/` out of the repo.
- `LICENSE` — MIT.
- `.github/workflows/ci.yml` — CI: shell / python / node syntax checks.
- `out/` — generated outputs (**git-ignored**).
