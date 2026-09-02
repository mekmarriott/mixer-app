#!/usr/bin/env bash
# Runs every suite. Exit non-zero if any fails.
#
#   ./run_tests.sh          all three suites
#   ./run_tests.sh --fast   skip the browser suite (no server boot)
#
# Coverage map (which suite proves which testing-document item):
#   docs/automation-test-manifest.md
set -e
cd "$(dirname "$0")"

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "No .venv found. Create it first:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
PY="$(pwd)/$PY"

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
