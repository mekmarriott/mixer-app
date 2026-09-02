#!/usr/bin/env bash
# Ingest a catalog in batches, publishing each batch to the blob store and the
# remote database before starting the next.
#
#   ./ingest_and_upload.sh
#   BATCH=50 TRACKS=config/tracks.electronic.json ./ingest_and_upload.sh
#
# WHY BATCHES
#
# `backend.publish` fetches every master before it renders anything, so a
# 1200-track import downloads for hours with nothing publishable until the very
# end. That is a bad shape for a long run: an interruption at hour ten leaves
# the store exactly as empty as it was at hour one, and nothing is verifiable
# until everything is done. Running it `--limit BATCH` at a time turns one
# all-or-nothing job into a sequence of complete ones — after every round the
# catalog and the store are consistent with each other and the app can serve
# what has landed so far.
#
# Nothing is wasted by stopping between rounds, and nothing is re-fetched by
# starting again: the publisher skips tracks whose master and metadata sidecar
# are already in the store, so a resumed run costs no API quota for anything it
# already has.
#
# THE THREE STEPS OF A ROUND
#
#   1. publish --limit N   download, analyse and render N tracks locally
#   2. db.sync             upsert the new rows into the remote catalog
#   3. reconcile --apply   upload the objects those rows name
#
# The order matters. Rows before objects would briefly advertise audio that is
# not there yet, and the app answers a track request with a redirect straight
# to the object — so a client could be sent to a URL that 404s. Doing it the
# other way (objects first) is merely invisible: an object nothing references
# yet is inert. Step 3 is what closes the gap, and it is idempotent, so a round
# that dies midway is fixed by running the script again.
set -euo pipefail
cd "$(dirname "$0")"

BATCH=${BATCH:-100}
TRACKS=${TRACKS:-config/tracks.jamendo-bulk.json}
IO_WORKERS=${IO_WORKERS:-8}
UPLOAD_WORKERS=${UPLOAD_WORKERS:-8}
PY=${PYTHON:-.venv/bin/python}

# The local catalog is the source of truth for a push: it is where ingestion
# accumulates. Deliberately NOT the local PostgreSQL, which is the development
# and test database and collects synthetic fixture tracks from the suites.
SOURCE_DB=${SOURCE_DB:-sqlite:///data/catalog.sqlite3}

# Credentials and the remote endpoints.
set -a
# shellcheck disable=SC1091
. ./.env
set +a

# `vercel blob` refuses to start when BLOB_STORE_ID is set without an OIDC
# token, and the Vercel integration writes BLOB_STORE_ID into .env on its own.
unset BLOB_STORE_ID

VERCEL_CLI=${VERCEL_CLI:-"npx --yes vercel@latest"}
REMOTE_DB=${REMOTE_DB:-$MIX_DB_POSTGRES_URL}
: "${BLOB_BASE_URL:=https://9fj05rbnkmudvmgn.public.blob.vercel-storage.com}"
export BLOB_BASE_URL

pending_count() {
  DJMIXER_DATABASE_URL= DJMIXER_TRACKS="$TRACKS" "$PY" -m backend.publish --dry-run \
    | sed -n 's/.*pending=\([0-9]*\).*/\1/p'
}

round=0
while :; do
  pending=$(pending_count)
  pending=${pending:-0}
  echo
  echo "=================================================================="
  echo "== $pending track(s) still pending"
  if [ "$pending" -eq 0 ]; then
    echo "== nothing left to ingest"
    break
  fi
  round=$((round + 1))
  echo "== round $round: ingesting up to $BATCH"
  echo "=================================================================="

  DJMIXER_DATABASE_URL= DJMIXER_TRACKS="$TRACKS" \
    "$PY" -u -m backend.publish --limit "$BATCH" --io-workers "$IO_WORKERS"

  echo "-- pushing catalog rows to the remote database"
  "$PY" -u -m backend.db.sync --from "$SOURCE_DB" --to "$REMOTE_DB"

  echo "-- uploading objects to the blob store"
  DJMIXER_DATABASE_URL="$REMOTE_DB" BLOB_BACKEND=vercel \
    "$PY" -u -m backend.reconcile --apply \
      --workers "$UPLOAD_WORKERS" --cli "$VERCEL_CLI"
done

echo
echo "== all rounds complete =="
