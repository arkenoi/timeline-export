#!/usr/bin/env bash
# precommit-scan.sh — local leak scan before pushing.
#
# Runs the same generic checks as CI, plus your own private terms from `.private-denylist`
# (one lowercase term per line). That file is git-ignored on purpose: a public repo must
# never contain a curated list of your real places, names or devices.
#
# Create it once, e.g.:
#     printf 'mytown\nmystreet\nmydevicename\n' > .private-denylist
#
# Usage: tools/precommit-scan.sh
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"
fail=0
ex=(':!.github/workflows/ci.yml' ':!tools/precommit-scan.sh')

check() { # name, pattern, extra excludes...
  local name="$1" pat="$2"; shift 2
  if git grep -nIE "$pat" -- . "${ex[@]}" "$@" ; then
    echo "  !! $name"; fail=1
  fi
}
check "personal email"      '[A-Za-z0-9._%+-]+@(gmail|yahoo|hotmail|outlook|proton(mail)?|icloud)\.[a-z]+'
check "real placeId"        'ChIJ[A-Za-z0-9_-]{22,}' ':!sample-output.json'
check "credential-shaped"   'oauth2_4/[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,}|ya29\.[A-Za-z0-9_-]{20,}|aas_et/'
check "featureId"           '0x[0-9a-f]{16}:0x[0-9a-f]{16}' ':!sample-output.json'

# private terms — case-insensitive (a case-sensitive check once let a capitalised city through)
if [ -f .private-denylist ]; then
  while IFS= read -r term; do
    [ -z "$term" ] && continue
    if git grep -niE -- "$term" . "${ex[@]}" ':!.private-denylist' ; then
      echo "  !! private term matched (see .private-denylist)"; fail=1
    fi
  done < .private-denylist
else
  echo "  note: no .private-denylist — create one with your own terms for a stronger check"
fi

# and the same scan across ALL history, not just the working tree
echo "-- history --"
if git grep -niE 'ChIJ[A-Za-z0-9_-]{22,}|oauth2_4/|AIza[0-9A-Za-z_-]{30,}|aas_et/' \
     $(git rev-list --all) -- . ':!sample-output.json' 2>/dev/null | head -5 ; then
  echo "  !! matches found in git history — a fix-forward commit is not enough; rewrite it"
  fail=1
fi

[ "$fail" = 0 ] && echo "clean" || echo "LEAKS FOUND"
exit $fail
