#!/bin/sh
# Explicit first deployment only; never run from the service entrypoint.
set -eu
umask 077

fail() {
    printf 'stage-reader: %s\n' "$1" >&2
    exit 2
}

[ "$#" -eq 1 ] || fail 'usage: sh /app/deploy/stage-reader.sh <approved-public-directory-or-manifest-URL>'
root=/publication
[ -d "$root" ] && [ ! -L "$root" ] || fail '/publication must be an existing real bind directory'
exec 9>"$root/.reader-stage.lock"
flock -n 9 || fail 'another initial staging operation is running'
for destination in "$root/source" "$root/site"; do
    [ ! -e "$destination" ] && [ ! -L "$destination" ] || fail 'source/site already exists; staging never replaces or deletes an existing deployment'
done

workspace=$(mktemp -d "$root/.reader-initial-XXXXXXXX")
trap 'rm -rf -- "$workspace"' 0
trap 'exit 1' HUP INT TERM
mkdir "$workspace/scripts" "$workspace/public"
cp /app/reader/scripts/stage-data.mjs "$workspace/scripts/stage-data.mjs"
cp /app/reader/scripts/stage-public-facts.mjs "$workspace/scripts/stage-public-facts.mjs"
cp /app/reader/scripts/catalog-projections.mjs "$workspace/scripts/catalog-projections.mjs"
# The existing importer copies only current manifest references, validates asset
# hashes and rejects symlinks. Unreferenced old versions in the input stay put.
node "$workspace/scripts/stage-data.mjs" "$1"
node /app/reader/scripts/build-search.mjs "$workspace/public/data"
cp -R /opt/nanna-reader-site "$workspace/site"
mkdir "$workspace/site/data"
# Source, site and publisher staging must stay on this single bind filesystem.
cp -al "$workspace/public/data/." "$workspace/site/data/"
node /app/reader/scripts/build-share.mjs \
    --data-root "$workspace/site/data" --output-root "$workspace/site"

# Refuse overwrites even if another operator created a destination meanwhile.
# Start services only after this explicit command succeeds. No private DB is
# initialized here, and a failure never removes an already installed destination.
for destination in "$root/source" "$root/site"; do
    [ ! -e "$destination" ] && [ ! -L "$destination" ] || fail 'destination appeared during preparation; nothing installed'
done
mv -T "$workspace/public/data" "$root/source"
mv -T "$workspace/site" "$root/site"
printf '%s\n' 'Public source and static site staged. Explicitly init-admin (first use) or migrate (upgrade) before starting the console.'
