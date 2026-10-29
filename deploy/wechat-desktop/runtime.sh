#!/bin/bash
set -euo pipefail
umask 077

here=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repository=$(realpath "$here/../..")
volume=nannanews_wechat_desktop_private

compose() {
    # Never load the production deployment's .env, service names, or profiles.
    docker compose --env-file /dev/null --project-directory "$here" \
        --project-name nannanews-wechat-desktop --file "$here/compose.yaml" "$@"
}

ensure_volume() {
    if docker volume inspect "$volume" > /dev/null 2>&1; then
        if [[ $(docker volume inspect --format '{{index .Labels "org.nannanews.purpose"}}' "$volume") != wechat-desktop ]]; then
            echo 'Existing volume is not marked as this dedicated desktop; refusing to attach it.' >&2
            exit 1
        fi
    else
        docker volume create --driver local --label org.nannanews.purpose=wechat-desktop \
            --label org.nannanews.private=true "$volume" > /dev/null
    fi
}

private_directory() {
    private_dir=$(realpath -m "${XDG_STATE_HOME:-$HOME/.local/state}/nannanews/wechat-desktop")
    case "$private_dir" in
        "$repository"|"$repository"/*)
            echo 'Private desktop exports must live outside the repository.' >&2
            exit 1
            ;;
    esac
    mkdir -p -m 0700 "$private_dir"
    if [[ $(stat -c %u "$private_dir") != "$(id -u)" || $(stat -c %a "$private_dir") != 700 ]]; then
        echo 'Private desktop export directory must be owned by this user with mode 0700.' >&2
        exit 1
    fi
}

export_private() {
    local kind=$1 temporary destination
    private_directory
    temporary=$(mktemp "$private_dir/.export.XXXXXXXX")
    trap 'rm -f -- "$temporary"' EXIT
    case "$kind" in
        password)
            destination="$private_dir/vnc-password"
            compose exec -T --user 10001:10001 desktop cat /home/wechat/.vnc/password > "$temporary"
            ;;
        screenshot)
            destination="$private_dir/screenshot.png"
            compose exec -T --user 10001:10001 desktop /bin/sh -c '
                umask 077
                capture=$(mktemp "$XDG_RUNTIME_DIR/capture.XXXXXXXX.png")
                trap '\''rm -f -- "$capture"'\'' EXIT
                scrot --overwrite "$capture" && cat "$capture"
            ' > "$temporary"
            ;;
    esac
    chmod 0600 "$temporary"
    mv -T -- "$temporary" "$destination"
    trap - EXIT
    printf 'Private %s file: %s\n' "$kind" "$destination"
}

case "${1:-}" in
    build)
        compose build desktop
        ;;
    start)
        ensure_volume
        compose up --detach --no-build --pull never --wait --wait-timeout 120 desktop
        compose exec -T desktop x11vnc -norc -R wait:20 -Q wait,defer
        compose exec -T desktop x11vnc -norc -R defer:20 -Q wait,defer
        export_private password
        printf '%s\n' 'Manual login only: http://127.0.0.1:33440/vnc.html?autoconnect=1&resize=scale&quality=6&compression=0'
        ;;
    password-file)
        export_private password
        ;;
    screenshot)
        export_private screenshot
        ;;
    input)
        shift
        if (($# == 0)); then
            echo 'Supply xdotool keyboard/mouse arguments after input.' >&2
            exit 2
        fi
        compose exec -T --user 10001:10001 desktop xdotool "$@"
        ;;
    status)
        compose ps desktop
        ;;
    stop)
        # Keep the external profile volume and private exports intact.
        compose stop desktop
        ;;
    *)
        echo 'Usage: runtime.sh {build|start|password-file|screenshot|input XDO_ARGS...|status|stop}' >&2
        exit 2
        ;;
esac
