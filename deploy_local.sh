#!/bin/sh
# Deploy to Vercel from this machine, without going through GitHub.
#
#   ./deploy_local.sh          preview deployment
#   ./deploy_local.sh prod     production deployment
#
# Deploys a CLEAN EXPORT of HEAD plus your uncommitted edits to tracked files,
# rather than the working directory itself. The Python builder bundles whatever
# directory it is pointed at, and this one carries 1.6 GB of agent worktrees
# under .claude/ and a 954 MB data/ — 2185 MB against a 500 MB function limit.
# Exporting sidesteps that. .vercelignore alone does not: the builder does not
# consult it, and listing a path there while the file is still on disk makes the
# build machine fail with ENOENT on a file it was told about but never received.
set -e
ROOT=$(cd "$(dirname "$0")" && pwd)
STAGE=${TMPDIR:-/tmp}/mixer-deploy.$$
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE"
git -C "$ROOT" archive HEAD | tar -x -C "$STAGE"

# Ship the working-tree version of anything tracked and modified, so a deploy
# reflects what you are looking at rather than the last commit.
git -C "$ROOT" diff --name-only HEAD | while read -r f; do
  if [ -f "$ROOT/$f" ]; then
    mkdir -p "$STAGE/$(dirname "$f")"
    cp "$ROOT/$f" "$STAGE/$f"
  fi
done

# Drop what .vercelignore excludes, then drop .vercelignore itself. Under
# --prebuilt the CLI puts ignored files in the manifest the build machine reads
# but does not upload them, so every remaining exclusion becomes an ENOENT
# there. With the tree already pruned there is nothing left for it to exclude.
rm -rf "$STAGE/tests" "$STAGE/docs" "$STAGE/data" "$STAGE/blobs" \
       "$STAGE/run_tests.sh" "$STAGE/requirements-ingest.txt"
find "$STAGE" -name '*.md' ! -name 'README.md' -delete
rm -f "$STAGE/.vercelignore"

mkdir -p "$STAGE/.vercel"
cp "$ROOT/.vercel/project.json" "$STAGE/.vercel/project.json"

cd "$STAGE"
if [ "$1" = "prod" ] || [ "$1" = "production" ]; then
  npx --yes vercel@latest build --prod --yes
  npx --yes vercel@latest deploy --prebuilt --prod --yes
else
  npx --yes vercel@latest build --yes
  npx --yes vercel@latest deploy --prebuilt --yes
fi
