# Shared implementation for deploy.sh and rollback.sh; source, do not execute.

fail() {
    printf '%s: %s\n' "$OPERATION" "$1" >&2
    exit 2
}

validate_image() {
    label=$1
    reference=$2
    case "$reference" in
        *@*) fail "$label must be an explicit version tag, not a digest-only reference" ;;
        *:*) image_tag=${reference##*:} ;;
        *) fail "$label must include an explicit version tag" ;;
    esac
    case "$image_tag" in
        ''|latest|*[!A-Za-z0-9_.-]*) fail "$label must use a non-latest version tag" ;;
    esac
}

compose() {
    docker --context "$NANNA_DEPLOY_TARGET" compose \
        --project-name nannanews \
        --project-directory "$ROOT" \
        --file "$ROOT/deploy/compose.yaml" \
        --env-file "$ROOT/.env" \
        --env-file "$ROOT/.env.active" \
        --profile collection \
        "$@"
}

run_at() {
    run_image=$1
    run_root=$2
    shift 2
    NANNA_ACTIVE_IMAGE="$run_image" NANNA_ACTIVE_DATA_DIR="$run_root" compose "$@"
}

prepare_lifecycle() {
    : "${NANNA_DEPLOY_TARGET:?Set NANNA_DEPLOY_TARGET to an existing Docker context}"
    : "${NANNA_BACKUP_NAME:?Set NANNA_BACKUP_NAME to a backup .zip basename}"
    [ "${NANNA_EXTERNAL_WRITERS_STOPPED:-}" = YES ] || fail 'set NANNA_EXTERNAL_WRITERS_STOPPED=YES only after stopping every writer outside this Compose project'
    case "$NANNA_BACKUP_NAME" in
        *.zip) ;;
        *) fail 'NANNA_BACKUP_NAME must end in .zip' ;;
    esac
    case "$NANNA_BACKUP_NAME" in
        ''|.|..|.*|*/*|*[!A-Za-z0-9_.-]*) fail 'NANNA_BACKUP_NAME must be a plain, non-hidden filename' ;;
    esac
    [ -f "$ROOT/.env" ] || fail "create $ROOT/.env from .env.example and configure this deployment"
    [ -f "$ROOT/.env.active" ] && [ ! -L "$ROOT/.env.active" ] || fail 'create a regular .env.active with the active image and data root as documented'
    command -v docker >/dev/null 2>&1 || fail 'Docker CLI is required'
    docker context inspect "$NANNA_DEPLOY_TARGET" >/dev/null 2>&1 || fail 'the requested Docker context is unavailable'
    docker --context "$NANNA_DEPLOY_TARGET" info >/dev/null 2>&1 || fail 'the requested Docker target is unreachable or permission was denied'

    # The managed file, not a caller's inherited shell, selects current state.
    unset NANNA_ACTIVE_IMAGE NANNA_ACTIVE_DATA_DIR
    resolved=$(compose config --environment) || fail 'cannot resolve the persisted Compose configuration'
    CURRENT_IMAGE=
    CURRENT_DATA_DIR=
    BACKUP_DIR=
    while IFS= read -r setting; do
        case "$setting" in
            NANNA_ACTIVE_IMAGE=*) CURRENT_IMAGE=${setting#*=} ;;
            NANNA_ACTIVE_DATA_DIR=*) CURRENT_DATA_DIR=${setting#*=} ;;
            NANNA_BACKUP_DIR=*) BACKUP_DIR=${setting#*=} ;;
        esac
    done <<EOF
$resolved
EOF
    unset resolved
    validate_image NANNA_ACTIVE_IMAGE "$CURRENT_IMAGE"
    case "$CURRENT_DATA_DIR" in
        /data/*) data_name=${CURRENT_DATA_DIR#/data/} ;;
        *) fail 'active data root must be a direct child of /data, normally /data/live' ;;
    esac
    case "$data_name" in
        ''|.|..|.*|*/*|*[!A-Za-z0-9_.-]*) fail 'active data root must be /data followed by one plain directory name' ;;
    esac
    case "$BACKUP_DIR" in
        /*) ;;
        *) fail 'NANNA_BACKUP_DIR in .env must be an absolute path on the Docker host' ;;
    esac
    # Reserve the atomic state replacement before stopping any services. The
    # operator's .env and secrets are never rewritten or copied into this file.
    umask 077
    STATE_TEMP=$(mktemp "$ROOT/.env.active.XXXXXX") || fail 'cannot stage the active deployment configuration'
    trap 'rm -f "$STATE_TEMP"' 0
    trap 'exit 1' HUP INT TERM
}

resolve_current_root() {
    CURRENT_DATA_DIR=$(run_at "$CURRENT_IMAGE" "$CURRENT_DATA_DIR" run \
        --rm --no-deps --entrypoint python web -c '
import os
from pathlib import Path
root = Path(os.environ["NANNA_DATA_DIR"]).resolve(strict=True)
if root.parent != Path("/data") or not (root / "database.db").is_file():
    raise SystemExit("Active data must be an initialized direct child of /data")
print(root)
') || fail 'cannot resolve the initialized active data root'
}

activate() {
    next_image=$1
    next_root=$2
    printf 'NANNA_ACTIVE_IMAGE=%s\nNANNA_ACTIVE_DATA_DIR=%s\n' \
        "$next_image" "$next_root" > "$STATE_TEMP"
    if ! run_at "$next_image" "$next_root" up --detach --wait --wait-timeout 120 web; then
        run_at "$next_image" "$next_root" stop web collection || :
        fail 'Web did not become healthy; services stopped and persisted active state is unchanged'
    fi
    if ! mv -f "$STATE_TEMP" "$ROOT/.env.active"; then
        run_at "$next_image" "$next_root" stop web collection || :
        fail 'could not persist active state; services stopped; inspect before restarting'
    fi
}
