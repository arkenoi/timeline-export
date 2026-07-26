#!/usr/bin/env python3
"""
web_key.py — EXPERIMENTAL: obtain the security-domain key with a browser only (no Android).

The lightweight alternative to `extract_key.py`, which needs a device already enrolled in
the `on_device_location_history` security domain. This drives the same Google-hosted page
that Google Play services itself uses to receive domain keys:

    https://accounts.google.com/encryption/unlock/android?kdi=<base64url KeyDeliveryInfo>

GMS loads that page in a WebView with a JS bridge bound as `mm`, and the page delivers key
material by calling `mm.setVaultSharedKeys(accountId, keysJson)`. This does the same thing
with headless Chromium and a `window.mm` shim, so no Android is involved at all.

Shapes come from microG GmsCore (Apache-2.0, PR #3331):
  KeyDeliveryInfo{ operationType=1, keyRetrieval=2, sessionId=6 }
  StartKeyRetrievalRequest{ domain=1, reset=2 }
  KeyDeliveryOperationType: START_KEY_RETRIEVAL=1, INITIAL_ENROLLMENT=3, LSKF_CONSENT=4
  auth: an OAuth token for service "weblogin:continue=<urlencoded target>", which returns
        a self-signing-in URL.

STATUS: VERIFIED. Produces the identical 32-byte key that `extract_key.py` reads from an
enrolled device (same sha256, same epoch), and that key decrypts a live Geller fetch.
Google interposes a password re-auth challenge before delivering the key, so run with
--headful and type your password in the window; it is NOT a lock-screen knowledge factor,
so no enrolled device is required. This makes the whole pipeline Android-free.

Usage:
  python3 web_key.py --email you@example.com --android-id <16 hex> \\
      --master-token-file out/master.txt -o out/key.b64
  # --enroll  attempts INITIAL_ENROLLMENT instead of key retrieval
  # --headful renders a real window so you can complete any challenge yourself
"""
import argparse, base64, json, os, subprocess, sys, urllib.parse, uuid

WEB_BASE = "https://accounts.google.com/encryption/unlock/android"
DOMAIN = "on_device_location_history"
GMS_PKG = "com.google.android.gms"
CLIENT_SIG = "38918a453d07199354f8b19af05ec6562ced5788"

def _v(n):
    out = b""
    while True:
        b = n & 0x7F; n >>= 7
        out += bytes([b | 0x80]) if n else bytes([b])
        if not n: return out

def tag(f, w): return _v((f << 3) | w)
def fvarint(f, n): return tag(f, 0) + _v(n)
def fbytes(f, b): return tag(f, 2) + _v(len(b)) + b
def fstr(f, s): return fbytes(f, s.encode())

def build_kdi(session_id, domain=DOMAIN, enroll=False, reset=False):
    """KeyDeliveryInfo{operationType, oneof data, sessionId}"""
    if enroll:
        op, data = 3, fbytes(4, b"")                      # INITIAL_ENROLLMENT{}
    else:
        inner = fstr(1, domain) + (fvarint(2, 1) if reset else b"")
        op, data = 1, fbytes(2, inner)                    # START_KEY_RETRIEVAL
    return fvarint(1, op) + data + fstr(6, session_id)

def b64u(b): return base64.urlsafe_b64encode(b).decode().rstrip("=")

def main():
    ap = argparse.ArgumentParser(description="Browser-only security-domain key retrieval.")
    ap.add_argument("--email", required=True)
    ap.add_argument("--android-id", required=True)
    ap.add_argument("--master-token-file", required=True)
    ap.add_argument("--domain", default=DOMAIN)
    ap.add_argument("--enroll", action="store_true", help="INITIAL_ENROLLMENT instead of retrieval")
    ap.add_argument("--headful", action="store_true", help="show the browser so you can answer challenges")
    ap.add_argument("--chrome", default=os.environ.get("CHROME_PATH", "/usr/bin/chromium-browser"))
    ap.add_argument("--node-path", default=os.environ.get("NODE_PATH", ""))
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("-o", "--out", default="out/key.b64")
    a = ap.parse_args()

    try:
        import gpsoauth
    except ImportError:
        sys.exit("error: pip install gpsoauth")

    master = open(a.master_token_file).read().strip()
    session_id = str(uuid.uuid4())
    kdi = build_kdi(session_id, a.domain, enroll=a.enroll)
    target = f"{WEB_BASE}?kdi={b64u(kdi)}&hl=en"
    service = "weblogin:continue=" + urllib.parse.quote(target, safe="")

    print(f"[*] session {session_id}", file=sys.stderr)
    print(f"[*] operation: {'INITIAL_ENROLLMENT' if a.enroll else 'START_KEY_RETRIEVAL'} on {a.domain}", file=sys.stderr)
    r = gpsoauth.perform_oauth(a.email, master, a.android_id, service=service,
                               app=GMS_PKG, client_sig=CLIENT_SIG)
    auth_url = r.get("Auth")
    if not auth_url:
        sys.exit(f"weblogin token request failed: {r.get('Error') or r}")
    if "WILL_NOT_SIGN_IN" in auth_url:
        sys.exit("Google returned WILL_NOT_SIGN_IN for this weblogin request")
    print(f"[*] got a self-signing-in URL ({len(auth_url)} chars)", file=sys.stderr)

    # drive the page in Chromium with a window.mm shim, exactly like GMS's WebView bridge
    driver = r"""
const P = require('puppeteer-core');
const fs = require('fs');
(async () => {
  const [chrome, url, timeout, headful] = process.argv.slice(2);
  const browser = await P.launch({ executablePath: chrome, headless: headful === '1' ? false : 'new',
    args: ['--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--lang=en-US'] });
  const pg = await browser.newPage();
  const got = [];
  await pg.exposeFunction('__mmCall', (name, args) => { got.push({ name, args }); });
  // bind the same bridge object name GMS uses (addJavascriptInterface(jsBridge, "mm"))
  await pg.evaluateOnNewDocument(() => {
    const names = ['setVaultSharedKeys','setConsent','closeView','getAccountId',
                   'getDomainState','setDomainState','onKeyRetrievalResult','log'];
    window.mm = {};
    for (const n of names) window.mm[n] = (...a) => {
      try { window.__mmCall(n, a.map(x => typeof x === 'string' ? x : JSON.stringify(x))); } catch(e){}
      if (n === 'getAccountId') return '';
      return null;
    };
  });
  try { await pg.goto(url, { waitUntil: 'networkidle2', timeout: 60000 }); } catch (e) {}
  const deadline = Date.now() + parseInt(timeout) * 1000;
  while (Date.now() < deadline && !got.some(g => g.name === 'setVaultSharedKeys')) {
    await new Promise(r => setTimeout(r, 1000));
  }
  const title = await pg.title().catch(() => '');
  const finalUrl = pg.url();
  const bodyText = await pg.evaluate(() => document.body ? document.body.innerText.slice(0, 400) : '').catch(() => '');
  console.log(JSON.stringify({ calls: got, title, finalUrl, bodyText }));
  await browser.close();
})().catch(e => { console.log(JSON.stringify({ error: e.message })); process.exit(1); });
"""
    drv = "/tmp/.web_key_driver.js"
    with open(drv, "w") as f: f.write(driver)
    env = dict(os.environ)
    if a.node_path: env["NODE_PATH"] = a.node_path
    p = subprocess.run(["node", drv, a.chrome, auth_url, str(a.timeout), "1" if a.headful else "0"],
                       capture_output=True, text=True, env=env)
    os.unlink(drv)
    if not p.stdout.strip():
        sys.exit(f"driver produced no output: {p.stderr[-400:]}")
    res = json.loads(p.stdout.strip().splitlines()[-1])
    if res.get("error"): sys.exit(f"driver error: {res['error']}")

    print(f"[*] page title : {res.get('title')!r}", file=sys.stderr)
    print(f"[*] final url  : {res.get('finalUrl','')[:110]}", file=sys.stderr)
    print(f"[*] mm calls   : {[c['name'] for c in res.get('calls',[])] or 'none'}", file=sys.stderr)

    shared = next((c for c in res.get("calls", []) if c["name"] == "setVaultSharedKeys"), None)
    if not shared:
        txt = (res.get("bodyText") or "").strip().replace("\n", " ")
        print(f"[*] page said  : {txt[:300]}", file=sys.stderr)
        sys.exit("no setVaultSharedKeys — the page did not hand over key material.\n"
                 "  If it asked for a lock-screen PIN/consent, a bare browser cannot satisfy it;\n"
                 "  use extract_key.py against an enrolled device. Try --headful to see the page.")

    # keysJson = { "<domain>": [ { "epoch": N, "key": { "keyMaterial": "<base64>" } } ] }
    payload = shared["args"][-1]
    try: keys = json.loads(payload)
    except Exception: sys.exit(f"could not parse keysJson: {payload[:200]}")
    raw = None; epoch = None
    def as_bytes(o):
        """the page sends key material either as base64 or as a {"0":b,"1":b,...} byte map"""
        if isinstance(o, str):
            try: return base64.b64decode(o + "=" * (-len(o) % 4))
            except Exception: return None
        if isinstance(o, dict) and o and all(k.isdigit() for k in o):
            try: return bytes(o[str(i)] for i in range(len(o)))
            except Exception: return None
        return None
    def walk(o):
        nonlocal raw, epoch
        if isinstance(o, dict):
            if isinstance(o.get("epoch"), int): epoch = o["epoch"]
            for k in ("keyMaterial", "key"):
                if k in o and raw is None:
                    b = as_bytes(o[k])
                    if b: raw = b
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    walk(keys)
    if not raw: sys.exit(f"no key material in the delivered payload: {payload[:200]}")
    if len(raw) != 32: print(f"  warn: key is {len(raw)} bytes (expected 32)", file=sys.stderr)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    old = os.umask(0o077)
    try:
        with open(a.out, "w") as f: f.write(base64.b64encode(raw).decode())
    finally: os.umask(old)
    os.chmod(a.out, 0o600)
    print(f"key written to {a.out} (epoch {epoch}, {len(raw)} bytes, mode 600, not displayed)", file=sys.stderr)

if __name__ == "__main__":
    main()
