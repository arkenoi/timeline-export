#!/usr/bin/env python3
"""
get_token.py — obtain the OAuth bearer that geller_fetch.py needs, for your own account.

Two documented flows:
  A (recommended) browser sign-in --> oauth_token --(exchange_token)--> master token
  B (legacy)      email + app password --(perform_master_login)--> master token
     Google has largely retired B; it usually returns BadAuthentication now.
  then, either way:  master token --(perform_oauth)--> scoped bearer

FLOW A — get the oauth_token (a normal Google sign-in, 2FA and all):
  1. open this in a browser (a private window is fine):
       https://accounts.google.com/embedded/setup/android?source=com.google.android.gms&xoauth_display_name=Android%20Device
  2. sign in normally. The page will end up blank / "signed in" — that is expected.
  3. read the `oauth_token` cookie for accounts.google.com (DevTools > Application >
     Cookies). Its value starts with "oauth2_4/".
  4. python3 get_token.py --email you@example.com --android-id <16 hex> \
         --oauth-token-file oauth.txt --out tok.txt
  That token is single-use — redo step 1-3 if you need another master token.

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
    ap.add_argument("--oauth-token", help="oauth2_4/... from the browser sign-in (flow A)")
    ap.add_argument("--oauth-token-file", help="read the oauth_token from a file (flow A)")
    ap.add_argument("--app-password", help="legacy flow B; usually BadAuthentication now")
    ap.add_argument("--app-password-file", help="read the app password from a file (mode 600)")
    ap.add_argument("--app-password-stdin", action="store_true", help="read it from stdin (no TTY needed)")
    ap.add_argument("--master-token-file", help="reuse a saved master token instead of logging in")
    ap.add_argument("--save-master", metavar="PATH", help="persist the master token (treat as a password)")
    ap.add_argument("--out", help="write the bearer here instead of stdout")
    a = ap.parse_args()

    try:
        import gpsoauth
    except ImportError:
        sys.exit("error: pip install gpsoauth")

    # 1. master token — reuse, exchange a browser oauth_token (A), or app password (B)
    if a.master_token_file:
        with open(a.master_token_file) as f:
            master = f.read().strip()
    elif a.oauth_token or a.oauth_token_file:
        if a.oauth_token_file:
            with open(a.oauth_token_file) as f: ot = f.read().strip()
        else:
            ot = a.oauth_token.strip()
        if not ot.startswith("oauth2_4/"):
            print("warning: oauth_token usually starts with 'oauth2_4/'", file=sys.stderr)
        r = gpsoauth.exchange_token(a.email, ot, a.android_id)
        master = r.get("Token")
        if not master:
            sys.exit(f"token exchange failed: {r.get('Error') or r.get('ErrorDetail') or r}\n"
                     "the oauth_token is single-use and short-lived — redo the browser sign-in")
        if a.save_master:
            with open(a.save_master, "w") as f: f.write(master)
            import os; os.chmod(a.save_master, 0o600)
            print(f"master token saved to {a.save_master} (mode 600 — treat as a password)", file=sys.stderr)
    else:
        if a.app_password_file:
            with open(a.app_password_file) as f: pw = f.read().strip()
        elif a.app_password_stdin:
            pw = sys.stdin.readline().strip()
        elif a.app_password:
            pw = a.app_password
        elif sys.stdin.isatty():
            pw = getpass.getpass("Google app password: ")
        else:
            sys.exit("no TTY for a hidden prompt — run this in a real terminal, or use "
                     "--app-password-file PATH, or pipe it with --app-password-stdin")
        if not pw: sys.exit("empty app password")
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
