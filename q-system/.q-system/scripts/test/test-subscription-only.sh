#!/usr/bin/env bash
# Subscription only, never the billed API (founder, 2026-09-28: "I don't want to
# use the API"). Any scheduled script that shells `claude -p` must make it
# impossible for that call to inherit ANTHROPIC_API_KEY: `claude` prefers the
# key over the subscription login when both exist, so one exported key in a
# launchd env turns every unattended run into metered API spend.
#
# The rule this pins: a script that invokes `claude -p` either
#   - unsets the key near its top (`unset ANTHROPIC_API_KEY`), or
#   - calls claude only through `env -u ANTHROPIC_API_KEY` / the claude_p helper.
# Comment lines are ignored, so a script that only MENTIONS claude -p passes.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SCRIPTS="$ROOT/q-system/.q-system/scripts"
PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  ok   $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL $1"; }

# Scheduled entry points that run claude -p (found by the 2026-09-28 sweep).
TARGETS="kipi-dispatch.sh linear-worker.sh pr-review-agent.sh open-loops-heartbeat.sh hosted-review-gate.sh"

check() {  # check <path>
  local f="$1" name; name="$(basename "$f")"
  [ -f "$f" ] || { bad "$name is missing"; return; }
  if grep -qE '^[[:space:]]*unset([[:space:]]+[A-Z_]+)*[[:space:]]+ANTHROPIC_API_KEY\b' "$f"; then
    ok "$name unsets ANTHROPIC_API_KEY"; return
  fi
  # Every non-comment claude -p call must strip the key itself.
  local raw
  raw="$(grep -nE '(^|[^_[:alnum:]])claude"?[[:space:]]+-p\b' "$f" | grep -vE '^[0-9]+:[[:space:]]*#' | grep -vE 'env -u ANTHROPIC_API_KEY|claude_p ' || true)"
  if [ -n "$raw" ]; then
    bad "$name can run claude -p on the billed API key: $(echo "$raw" | head -1)"
  else
    ok "$name has no claude -p that can inherit the key"
  fi
}

echo "subscription-only guard for scheduled claude -p callers"
for t in $TARGETS; do
  case "$t" in
    kipi-dispatch.sh) check "$ROOT/$t" ;;
    *) check "$SCRIPTS/$t" ;;
  esac
done

# Negative control: a script with a bare call must be caught.
CTRL="$(mktemp)"; trap 'rm -f "$CTRL"' EXIT
printf '#!/bin/bash\nclaude -p "hi"\n' > "$CTRL"
out="$(FAIL=0; check "$CTRL" 2>&1)"
case "$out" in *FAIL*) ok "negative control: a bare claude -p is caught" ;; *) bad "negative control passed a bare claude -p" ;; esac

echo
echo "passed $PASS, failed $FAIL"
[ "$PASS" -gt 0 ] && [ "$FAIL" = 0 ]
