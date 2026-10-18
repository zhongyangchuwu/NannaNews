#!/bin/sh
set -eu

OPERATION=rollback
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$ROOT/deploy/lifecycle.sh"

: "${NANNA_PREVIOUS_IMAGE:?Set NANNA_PREVIOUS_IMAGE to the rollback-compatible release}"
: "${NANNA_RESTORE_DIR:?Choose a new directory name under the persistent /data parent}"
validate_image NANNA_PREVIOUS_IMAGE "$NANNA_PREVIOUS_IMAGE"
ROLLBACK_IMAGE=$NANNA_PREVIOUS_IMAGE
case "$NANNA_RESTORE_DIR" in
    ''|.|..|.*|*/*|*[!A-Za-z0-9_.-]*) fail 'NANNA_RESTORE_DIR must be a new plain directory name' ;;
esac
RESTORED_DATA_DIR="/data/$NANNA_RESTORE_DIR"
prepare_lifecycle

# Pull both exact releases and validate the backup/target before downtime.
run_at "$CURRENT_IMAGE" "$CURRENT_DATA_DIR" pull web
run_at "$ROLLBACK_IMAGE" "$RESTORED_DATA_DIR" pull web
resolve_current_root
run_at "$CURRENT_IMAGE" "$CURRENT_DATA_DIR" run \
    --rm --no-deps --entrypoint sh web -ec \
    'test -d /backups && test -r "$1" && test -s "$1" && test -w /data && test ! -e "$2" && test ! -L "$2"' \
    sh "/backups/$NANNA_BACKUP_NAME" "$RESTORED_DATA_DIR"

# Restore sees the current real root, so native source/daily prevention,
# historical attempts and original receipts remain monotonic across rollback.
compose stop web collection
run_at "$CURRENT_IMAGE" "$CURRENT_DATA_DIR" run --rm --no-deps web restore \
    "/backups/$NANNA_BACKUP_NAME" --target "$RESTORED_DATA_DIR"
run_at "$CURRENT_IMAGE" "$CURRENT_DATA_DIR" run \
    --rm --no-deps --entrypoint sh web -ec 'test -s "$1/database.db"' \
    sh "$RESTORED_DATA_DIR"

# Never downgrade in place. Only a healthy compatible reader becomes active;
# later collection and upgrades use this persisted sibling root and image.
activate "$ROLLBACK_IMAGE" "$RESTORED_DATA_DIR"

printf 'Rolled back to %s at %s. Backup: %s\n' \
    "$ROLLBACK_IMAGE" "$RESTORED_DATA_DIR" "$NANNA_BACKUP_NAME"
