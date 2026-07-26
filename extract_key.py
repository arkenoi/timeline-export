#!/usr/bin/env python3
"""
extract_key.py — read your own Timeline decryption key from a device you control.

The `on_device_location_history` security-domain key is what `geller_fetch.py` needs to
decrypt what Google's Geller service returns. It is stored by Google Play services on any
device already *enrolled* in that security domain:

    /data/data/com.google.android.gms/files/folsom/shared/FolsomKeyStore.pb

Inside, per account and per security domain, the entry for `on_device_location_history`
carries a plaintext 32-byte AES-256 key (unlike e.g. `hw_protected`, whose key material is
wrapped). This reads that one value and writes it base64-encoded, mode 600.

THIS IS A ONE-TIME STEP. Afterwards `geller_fetch.py` runs anywhere with no Android at
all. You need one enrolled device to bootstrap — a rooted phone or the project's redroid
container — because enrollment is what puts the key on the device in the first place.

Usage:
    # from a running container (needs root inside it)
    python3 extract_key.py --container rd -o out/key.b64
    # or from a FolsomKeyStore.pb you already pulled
    python3 extract_key.py --file FolsomKeyStore.pb -o out/key.b64

The key is never printed. Treat out/key.b64 as a secret: it decrypts your location history.
If the key rotates (the store also records an epoch), re-run this.
"""
import argparse, base64, os, subprocess, sys

DOMAIN = "on_device_location_history"
STORE = "/data/data/com.google.android.gms/files/folsom/shared/FolsomKeyStore.pb"

def parse(buf):
    """protobuf wire reader: field -> [(wire, value)]"""
    out, i, n = {}, 0, len(buf)
    while i < n:
        t = s = 0
        while True:
            x = buf[i]; i += 1; t |= (x & 0x7f) << s
            if not (x & 0x80): break
            s += 7
        f, w = t >> 3, t & 7
        if w == 0:
            v = s = 0
            while True:
                x = buf[i]; i += 1; v |= (x & 0x7f) << s
                if not (x & 0x80): break
                s += 7
        elif w == 1: v = buf[i:i+8]; i += 8
        elif w == 5: v = buf[i:i+4]; i += 4
        elif w == 2:
            ln = s = 0
            while True:
                x = buf[i]; i += 1; ln |= (x & 0x7f) << s
                if not (x & 0x80): break
                s += 7
            v = buf[i:i+ln]; i += ln
        else:
            raise ValueError(f"bad wire type {w}")
        out.setdefault(f, []).append((w, v))
    return out

def find_key(blob):
    """Locate the DomainsEntry named on_device_location_history and return its 32-byte key.

    Real GMS field numbering differs from microG's folsomkeystore.proto, so this walks
    structurally: find the message that contains the domain name as a string, then take the
    single 32-byte value from its DomainData/Keys submessage.
    """
    hits = []

    def walk(b, depth=0):
        if depth > 8: return
        try: fields = parse(b)
        except Exception: return
        for _, vals in fields.items():
            for w, v in vals:
                if w != 2 or not isinstance(v, bytes): continue
                try:
                    if v.decode("utf8") == DOMAIN:
                        hits.append(fields)      # the entry holding {name, data}
                except Exception:
                    pass
                walk(v, depth + 1)

    walk(blob)
    for entry in hits:
        for _, vals in entry.items():
            for w, v in vals:
                if w != 2 or not isinstance(v, bytes): continue
                # DomainData -> Keys -> a bare 32-byte field
                for depth_blob in (v,):
                    try: dd = parse(depth_blob)
                    except Exception: continue
                    for _, dvals in dd.items():
                        for dw, dv in dvals:
                            if dw != 2 or not isinstance(dv, bytes): continue
                            try: ks = parse(dv)
                            except Exception: continue
                            for _, kvals in ks.items():
                                for kw, kv in kvals:
                                    if kw == 2 and isinstance(kv, bytes) and len(kv) == 32:
                                        return kv
    return None

def main():
    ap = argparse.ArgumentParser(description="Extract the on_device_location_history key.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--container", help="docker container name (e.g. rd)")
    src.add_argument("--file", help="a FolsomKeyStore.pb pulled from a device")
    ap.add_argument("-o", "--out", default="out/key.b64")
    a = ap.parse_args()

    if a.file:
        blob = open(a.file, "rb").read()
    else:
        p = subprocess.run(["docker", "exec", a.container, "sh", "-c", f"cat {STORE}"],
                           capture_output=True)
        blob = p.stdout
        if not blob:
            sys.exit(f"could not read {STORE} from container {a.container!r} "
                     "(is it running, and is the account signed in / enrolled?)")

    key = find_key(blob)
    if not key:
        sys.exit(f"no 32-byte key for {DOMAIN!r} found — is this device enrolled in that "
                 "security domain? (Timeline must have synced at least once)")

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    old = os.umask(0o077)
    try:
        with open(a.out, "w") as f:
            f.write(base64.b64encode(key).decode())
    finally:
        os.umask(old)
    os.chmod(a.out, 0o600)
    print(f"key written to {a.out} (32 bytes, mode 600, not displayed)", file=sys.stderr)
    print(f"next: python3 geller_fetch.py --token-file out/tok.txt --key-file {a.out} "
          "-o out/odlh-storage.db", file=sys.stderr)

if __name__ == "__main__":
    main()
