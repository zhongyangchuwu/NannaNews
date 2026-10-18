#!/bin/sh
set -eu

fail() {
    printf 'release: %s\n' "$1" >&2
    exit 2
}

: "${NANNA_IMAGE:?Set NANNA_IMAGE to your registry/image:version tag}"
case "$NANNA_IMAGE" in
    *@*) fail 'use a version tag, not a digest-only reference' ;;
    *:*) image_tag=${NANNA_IMAGE##*:} ;;
    *) fail 'NANNA_IMAGE must include an explicit version tag' ;;
esac
case "$image_tag" in
    ''|latest|*[!A-Za-z0-9_.-]*) fail 'NANNA_IMAGE must use a non-latest version tag' ;;
esac

command -v docker >/dev/null 2>&1 || fail 'Docker CLI is required'
docker buildx inspect >/dev/null 2>&1 || fail 'Docker Buildx is required and must be configured'
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec docker buildx build \
    --file "$ROOT/deploy/Dockerfile" \
    --tag "$NANNA_IMAGE" \
    --push \
    "$ROOT"
