#!/usr/bin/env bash
# Runs every suite. Exit non-zero if any fails.
#
#   ./run_tests.sh          all three suites
#   ./run_tests.sh --fast   skip the browser suite (no server boot)
#
# Coverage map (which suite proves which testing-document item):
#   docs/automation-test-manifest.md
#
# DJMIXER_LIVE_TESTS=1 additionally runs the tests that hit the live Jamendo
# API (needs network + credentials in .env); they skip by default.
set -e
cd "$(dirname "$0")"

# Find an interpreter that actually has the deps. Checking for importability
# rather than just for .venv/ matters in two cases: a bare `python3` is often
# the system one (3.9 on macOS) with no site-packages, which fails at import
# and looks like a broken suite; and sibling worktrees have no .venv of their
# own but can use the main checkout's. Override with PYTHON=/path/to/python.
find_python() {
  local candidates=() common
  [ -n "$PYTHON" ] && candidates+=("$PYTHON")
  candidates+=("$PWD/.venv/bin/python")
  common="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  [ -n "$common" ] && candidates+=("$(dirname "$common")/.venv/bin/python")
  candidates+=("$(command -v python3 || true)")

  for c in "${candidates[@]}"; do
    if [ -n "$c" ] && [ -x "$c" ] && "$c" -c "import numpy, flask" >/dev/null 2>&1; then
      printf '%s\n' "$c"
      return 0
    fi
  done
  return 1
}

if ! PY="$(find_python)"; then
  cat >&2 <<'EOF'
No Python with numpy+flask found. Create one:
  python3 -m venv .venv && .venv/bin/pip install -r requirements-ingest.txt
(requirements.txt alone is the serving subset and omits scipy — the suite
needs the ingest extras.) Or set PYTHON=/path/to/python.
EOF
  exit 1
fi
echo "Using $PY ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"

echo
echo "== Backend suite (unittest) =="
(cd tests/backend && "$PY" -m unittest discover -s . -v)

echo
echo "== Frontend logic suite (node:test) =="
node --test tests/frontend/*.test.mjs

if [ "$1" = "--fast" ]; then
  echo
  echo "Skipped browser suite (--fast). All other suites passed."
  exit 0
fi

if [ ! -d node_modules ]; then
  echo
  echo "Skipping browser suite: run 'npm install && npx playwright install chromium' first." >&2
  echo "All other suites passed."
  exit 0
fi

echo
echo "== Browser suite (Playwright) =="
npx playwright test

echo
echo "All suites passed."
