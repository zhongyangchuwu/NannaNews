#!/bin/sh
set -eu

OPERATION=deploy
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$ROOT/deploy/lifecycle.sh"

: "${NANNA_IMAGE:?Set NANNA_IMAGE to the release image tag}"
validate_image NANNA_IMAGE "$NANNA_IMAGE"
RELEASE_IMAGE=$NANNA_IMAGE
prepare_lifecycle

# Fetch both persisted current and candidate releases before maintenance.
run_at "$CURRENT_IMAGE" "$CURRENT_DATA_DIR" pull web
run_at "$RELEASE_IMAGE" "$CURRENT_DATA_DIR" pull web
resolve_current_root

# Test the target-host bind mount as the app user before stopping writers.
# A unique backup name must never replace earlier recovery evidence.
run_at "$CURRENT_IMAGE" "$CURRENT_DATA_DIR" run \
    --rm --no-deps --entrypoint sh web -ec \
    'test -d /backups && test -w /backups && test ! -e "$1" && test ! -L "$1"' \
    sh "/backups/$NANNA_BACKUP_NAME"

compose stop web collection
run_at "$CURRENT_IMAGE" "$CURRENT_DATA_DIR" run \
    --rm --no-deps web backup "/backups/$NANNA_BACKUP_NAME"
run_at "$CURRENT_IMAGE" "$CURRENT_DATA_DIR" run \
    --rm --no-deps --entrypoint sh web -ec 'test -s "$1"' \
    sh "/backups/$NANNA_BACKUP_NAME"

# Forward migration uses the resolved active root, including after rollback.
# Failures leave writers stopped and the persisted active state unchanged.
run_at "$RELEASE_IMAGE" "$CURRENT_DATA_DIR" run --rm --no-deps web migrate
activate "$RELEASE_IMAGE" "$CURRENT_DATA_DIR"

printf 'Deployed %s at %s. Pre-migration backup: %s\n' \
    "$RELEASE_IMAGE" "$CURRENT_DATA_DIR" "$NANNA_BACKUP_NAME"
