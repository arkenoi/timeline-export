# Replacing the redroid stack with microG — findings & plan

**Status: researched, not built. Blocked pending decisions/credentials — see "What's parked".**

The pipeline's only heavy part is the Android container used solely to make Google Play
services fetch and decrypt your Timeline into `odlh-storage.db`. Today that needs
Android 14 + MindTheGapps + Magisk + ReZygisk + PlayIntegrityFork. This note records what
we learned about doing it with **microG** instead, and what actually blocks it.

## The key discovery

microG GmsCore merged **[PR #3331 "Add support for Google Maps timeline functionality"](https://github.com/microg/GmsCore/pull/3331)**
(2026-07-06, +6464/−227 across 48 files, shipped in v0.3.16.252432). It is an open-source
(Apache-2.0) clean-room implementation of the whole on-device Location History path: it
speaks Google's "Geller" sync protocol, fetches the `ENCRYPTED_ONDEVICE_LOCATION_HISTORY`
corpus, and decrypts it.

Relevant files, if you want to read the source:

| File | Role |
|---|---|
| `play-services-core-proto/src/main/proto/segment.proto` | the segment message (**already used** — see below) |
| `.../proto/batchsync.proto` | Geller sync request/response |
| `.../proto/externaldbsync.proto` | the DB-snapshot envelope |
| `.../proto/folsomkeystore.proto`, `securitydomainmembers.proto` | key/security-domain structures |
| `play-services-core/src/main/kotlin/.../semanticlocationhistory/api/GellerSyncClient.kt` | the sync client |
| `.../semanticlocationhistory/db/OdlhStorageManager.kt` | the on-device store |
| `.../db/backup/OdlhSyncProcessor.kt`, `BackupRestoreHandler.kt` | the restore pipeline |

### Already banked from this

`segment.proto` independently confirms the field numbering this repo's decoder derived
from raw wire bytes, and gave us `is_deleted` / `display_mode` / `finalization_status` /
`source` — all now handled in `odlh_export.py`. See **Schema provenance** in the README.
That value is realised regardless of whether the rest of this plan ever happens.

## Runtime options, and the blocker

**Option A — microG inside redroid.** ❌ Blocked as-is. microG needs *signature spoofing*
(so it can answer as `com.google.android.gms`). Checked on our container:

```
pm list permissions | grep FAKE_PACKAGE_SIGNATURE   →   (empty)
```

Vanilla redroid has no `FAKE_PACKAGE_SIGNATURE`, so it would need a patched framework —
which trades one pile of hacks for another.

**Option B — microG on Waydroid.** ✅ Most promising. Waydroid's vanilla (non-GApps)
image ships signature spoofing **already enabled**, and microG installs directly with no
patching. Waydroid is LXC-based rather than Docker, and needs the same host-kernel
`binder`/`ashmem` as redroid, so it should run wherever redroid does.

**Option C — standalone port (no Android at all).** Attractive but *not* recommended: it
means reimplementing an undocumented internal Google API and its key-retrieval flow, then
maintaining that against an interface Google can change without notice. Also, the key step
in microG runs through a Google-hosted web page with a JS bridge, which is the least
portable piece. High effort, high breakage risk.

**Option D — status quo.** The current stack works and is fully automated.

## Open question worth testing first

microG's signature spoofing exists so *third-party apps* trust it as GMS. Our use is
narrower: we don't need Google Maps installed at all — we only need microG's own
`SemanticLocationHistoryService` to sync ODLH into its store, which we then read. It is
plausible that sync works **without** spoofing.

**Cheap decisive experiment** (~1 hour, no commitment): stand up a second container on a
different name/port (do *not* touch the working one), install the microG GmsCore APK from
its official GitHub release, and see whether it runs and can reach its own sign-in. If yes,
Option B or even a plain-redroid variant becomes viable.

## What's parked

1. **Sign-in.** microG authenticates through its own normal login flow — this needs your
   credentials entered interactively, once. Nothing here extracts or reuses tokens from the
   existing container; that approach was explicitly abandoned.
2. **Verifying output compatibility.** `OdlhStorageManager.kt` suggests microG writes an
   `odlh-storage.db`-shaped store, which would mean this repo's decoder works unchanged —
   but that is *unverified*. If the shape differs, a small shim would be needed.
3. **Whether microG's ODLH implementation is field-proven.** It is recent (mid-2026) and we
   have not seen independent reports of it working in the wild.

## Honest summary

The protocol and crypto are no longer secret — they are public Apache-2.0 code. The
remaining questions are packaging (which Android runtime hosts microG with least fuss) and
verification (does its output match our decoder). Neither is solved here. Nothing in this
note should be read as "microG will definitely work" — it's the most promising route,
not a proven one.
