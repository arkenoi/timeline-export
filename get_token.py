#!/usr/bin/env python3
"""
get_token.py — obtain the OAuth bearer that geller_fetch.py needs, for your own account.

One documented flow, no guessing:
    email + Google APP PASSWORD  --(gpsoauth master login)-->  master token
    master token                 --(gpsoauth perform_oauth)-->  scoped bearer

The scope is the one microG uses for the Geller/Timeline corpus:
    oauth2:https://www.googleapis.com/auth/webhistory

You must supply the credentials — this script neither reads them from the device nor
stores them. The master token is kept only in memory unless you pass --save-master.
Create an app password at https://myaccount.google.com/apppasswords (needs 2-Step on).

Usage:
    pip install gpsoauth
    python3 get_token.py --email you@example.com --android-id <16 hex>   # prompts for app pw
    python3 get_token.py ... --out tok.txt                             # write instead of print

--android-id: the GSF/Android ID the token is bound to. Read it from a device you own,
e.g.  adb shell settings get secure android_id  (or, for a container,
docker exec <name> settings get secure android_id).

Then:
    python3 geller_fetch.py --token-file tok.txt --key-file key.b64 -o out/odlh-storage.db
"""
import argparse, getpass, sys

SCOPE = "oauth2:https://www.googleapis.com/auth/webhistory"
# microG/GMS client identity the scoped token is issued to
APP = "com.google.android.gms"
CLIENT_SIG = "38918a453d07199354f8b19af05ec6562ced5788"   # GMS release signature


def main():
    ap = argparse.ArgumentParser(description="Fetch a webhistory-scoped bearer token.")
    ap.add_argument("--email", required=True)
    ap.add_argument("--android-id", required=True, help="16-hex device/GSF id")
    ap.add_argument("--app-password", help="omit to be prompted (preferred)")
    ap.add_argument("--master-token-file", help="reuse a saved master token instead of logging in")
    ap.add_argument("--save-master", metavar="PATH", help="persist the master token (treat as a password)")
    ap.add_argument("--out", help="write the bearer here instead of stdout")
    a = ap.parse_args()

    try:
        import gpsoauth
    except ImportError:
        sys.exit("error: pip install gpsoauth")

    # 1. master token — either reuse, or exchange the app password for one
    if a.master_token_file:
        with open(a.master_token_file) as f:
            master = f.read().strip()
    else:
        pw = a.app_password or getpass.getpass("Google app password: ")
        r = gpsoauth.perform_master_login(a.email, pw, a.android_id)
        master = r.get("Token")
        if not master:
            sys.exit(f"master login failed: {r.get('Error') or r.get('ErrorDetail') or r}")
        if a.save_master:
            with open(a.save_master, "w") as f:
                f.write(master)
            import os; os.chmod(a.save_master, 0o600)
            print(f"master token saved to {a.save_master} (mode 600 — treat as a password)", file=sys.stderr)

    # 2. exchange it for a bearer scoped to the Geller/Timeline corpus
    r = gpsoauth.perform_oauth(a.email, master, a.android_id, service=SCOPE,
                               app=APP, client_sig=CLIENT_SIG)
    tok = r.get("Auth")
    if not tok:
        sys.exit(f"scoped token request failed: {r.get('Error') or r}")

    if a.out:
        with open(a.out, "w") as f:
            f.write(tok)
        import os; os.chmod(a.out, 0o600)
        print(f"bearer written to {a.out} (mode 600, expires in ~1h)", file=sys.stderr)
    else:
        print(tok)


if __name__ == "__main__":
    main()
