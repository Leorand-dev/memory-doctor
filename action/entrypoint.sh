#!/usr/bin/env bash
# entrypoint.sh — runs the memory-doctor Python script and maps its
# exit code to a job-status decision.
#
# Args (all from the action's inputs):
#   $1  workspace  (path to scan; "." for repo root)
#   $2  fail-on    (minimum severity that fails the job)
#   $3  redact     ("true" / "false")
#   $4  exclude    (comma-separated relative dirs)
#   $5  args       (extra args verbatim, e.g. --ignore-file ...)
#
# Sets GITHUB_OUTPUT entries: worst, findings, suppressed, exit-code.

set -u

WORKSPACE="${1:-.}"
FAIL_ON="${2:-medium}"
REDACT="${3:-true}"
EXCLUDE="${4:-}"
EXTRA_ARGS="${5:-}"

# Resolve the doctor script path. $GITHUB_ACTION_PATH is set by the
# runner to the directory containing this action.yml.
SCRIPT_DIR="${GITHUB_ACTION_PATH:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
DOCTOR="$SCRIPT_DIR/../scripts/memory-doctor.py"

if [ ! -f "$DOCTOR" ]; then
  echo "::error::memory-doctor.py not found at $DOCTOR"
  exit 3
fi

# Build the command line.
CMD=(python3 "$DOCTOR" --scan --json)
if [ "$REDACT" = "true" ]; then
  CMD+=(--redact)
else
  CMD+=(--no-redact)
fi

# Multi-value --exclude. argparse handles `--exclude X --exclude Y` fine.
if [ -n "$EXCLUDE" ]; then
  IFS=',' read -ra DIRS <<< "$EXCLUDE"
  for d in "${DIRS[@]}"; do
    d_trimmed="$(echo "$d" | xargs)"  # trim whitespace
    if [ -n "$d_trimmed" ]; then
      CMD+=(--exclude "$d_trimmed")
    fi
  done
fi

# Append any user-supplied extra args verbatim (advanced).
if [ -n "$EXTRA_ARGS" ]; then
  # shellcheck disable=SC2206
  EXTRA=( $EXTRA_ARGS )
  CMD+=("${EXTRA[@]}")
fi

# Run the doctor. Capture stdout (JSON) and the exit code.
cd "$GITHUB_WORKSPACE" 2>/dev/null || cd "$WORKSPACE"
JSON="$( "${CMD[@]}" 2>/dev/null )"
EXIT_CODE=$?

# Always print a one-line summary for the job log.
echo "memory-doctor exit=$EXIT_CODE"
echo "::notice::memory-doctor exit code = $EXIT_CODE"

# Parse the JSON. Prefer jq; fall back to a Python one-liner.
parse_field() {
  local field="$1"
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$JSON" | jq -r ".${field} // \"\"" 2>/dev/null
  else
    printf '%s' "$JSON" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    v = d.get('${field}', '')
    if isinstance(v, (dict, list)):
        print(json.dumps(v))
    else:
        print(v)
except Exception:
    print('')
" 2>/dev/null
  fi
}

WORST="$(parse_field 'summary.worst')"
TOTAL="$(parse_field 'summary.unsuppressed')"
SUPPRESSED="$(parse_field 'summary.suppressed')"
WORST="${WORST:-none}"
TOTAL="${TOTAL:-0}"
SUPPRESSED="${SUPPRESSED:-0}"

# Emit to GITHUB_OUTPUT (multi-line safe).
{
  echo "worst=$WORST"
  echo "findings=$TOTAL"
  echo "suppressed=$SUPPRESSED"
  echo "exit-code=$EXIT_CODE"
} >> "$GITHUB_OUTPUT"

# Map exit code to a job-level fail decision.
# Per design:
#   0  clean                              → pass
#   1  unsuppressed findings              → depends on fail-on
#   2  unsuppressed secret leaked         → always fail
#   3  internal error                     → always fail
#   4  clean but suppressions used        → pass (with --quiet summary)

# Severity rank for fail-on comparison.
rank() {
  case "$1" in
    none) echo 0 ;;
    info) echo 1 ;;
    low) echo 2 ;;
    medium) echo 3 ;;
    high) echo 4 ;;
    critical) echo 5 ;;
    *) echo 0 ;;
  esac
}

FAIL_RANK="$(rank "$FAIL_ON")"
WORST_RANK="$(rank "$WORST")"

should_fail() {
  if [ "$EXIT_CODE" = "2" ] || [ "$EXIT_CODE" = "3" ]; then
    return 0  # always fail on secret or internal error
  fi
  if [ "$EXIT_CODE" = "1" ] && [ "$WORST_RANK" -ge "$FAIL_RANK" ]; then
    return 0
  fi
  return 1
}

if should_fail; then
  echo "::error::memory-doctor found issues (exit=$EXIT_CODE, worst=$WORST, fail-on=$FAIL_ON)"
  exit 1
fi
exit 0
