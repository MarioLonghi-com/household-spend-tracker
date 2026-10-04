#!/usr/bin/env bash
# The static half of "no bulk statements against audited tables".
#
# The runtime guard in app/audit/guard.py is the real enforcement -- this catches
# the pattern at review time, before anyone runs the code. Lines that genuinely
# target an excluded table mark themselves with "# audit-exempt:" and say why.
set -uo pipefail

# \w+\.execute, not session\.execute: the session is not always called
# "session" -- ratelimit.prune opens its own and called it "own", which the
# narrower pattern walked straight past.
hits=$(grep -rnE '\w+\.execute\(\s*(update|delete|insert)\(|__table__\.(delete|update|insert)\(' \
        app/ scripts/ --include='*.py' 2>/dev/null \
      | grep -vE '# audit-exempt:' \
      | grep -vE '^app/audit/' || true)

if [ -n "$hits" ]; then
  echo "Bulk statements bypass the audit hook. Load the rows and change them:"
  echo "$hits"
  exit 1
fi
echo "no bulk statements against audited tables"
