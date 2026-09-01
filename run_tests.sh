#!/usr/bin/env bash
# Runs both test suites. Exit non-zero if either fails.
set -e
cd "$(dirname "$0")"

echo "== Backend suite (unittest) =="
(cd tests/backend && python3 -m unittest discover -s . -v)

echo
echo "== Frontend suite (node:test) =="
node --test tests/frontend/*.test.mjs

echo
echo "All suites passed."
