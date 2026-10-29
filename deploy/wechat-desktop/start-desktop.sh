#!/bin/bash
set -euo pipefail
umask 077

if [[ $(id -u) == 0 || ! -w $HOME ]]; then
    echo 'Dedicated desktop requires a writable private home and a non-root user.' >&2
    exit 1
fi
install -d -m 0700 "$XDG_RUNTIME_DIR" "$HOME/.vnc" \
    "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME"

# Standard VNC authentication uses only eight characters. Generate 48 random
# bits once, never echo them, and retain them only in this dedicated volume.
password_file="$HOME/.vnc/password"
if [[ ! -e $password_file ]]; then
    openssl rand -base64 6 > "$password_file"
fi
password=$(< "$password_file")
if [[ ! $password =~ ^[A-Za-z0-9+/]{8}$ ]]; then
    echo 'Dedicated VNC password file is invalid; refusing unauthenticated access.' >&2
    exit 1
fi
unset password
chmod 0600 "$password_file"

pids=()
cleanup() {
    # Let the client flush its dedicated profile while X and D-Bus still exist.
    if [[ -r "$XDG_RUNTIME_DIR/wechat.pid" ]]; then
        local client_pid attempt
        client_pid=$(< "$XDG_RUNTIME_DIR/wechat.pid")
        kill "$client_pid" 2>/dev/null || true
        for ((attempt = 0; attempt < 20; attempt++)); do
            if ! kill -0 "$client_pid" 2>/dev/null; then
                break
            fi
            sleep 0.5
        done
    fi
    if ((${#pids[@]})); then
        kill "${pids[@]}" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

start() {
    local name=$1
    shift
    # Client output can contain private session data: keep it out of Docker logs.
    "$@" > "$XDG_RUNTIME_DIR/$name.log" 2>&1 &
    pids+=("$!")
    printf '%s\n' "$!" > "$XDG_RUNTIME_DIR/$name.pid"
}

: > "$XAUTHORITY"
xauth -f "$XAUTHORITY" add "$DISPLAY" . "$(openssl rand -hex 16)"
start xvfb Xvfb "$DISPLAY" -screen 0 1440x1000x24 -auth "$XAUTHORITY" -nolisten tcp -noreset
for ((attempt = 0; attempt < 30; attempt++)); do
    if xdpyinfo -display "$DISPLAY" > /dev/null 2>&1; then
        break
    fi
    if ! kill -0 "${pids[0]}" 2>/dev/null; then
        echo 'Dedicated X server exited; inspect its private runtime log.' >&2
        exit 1
    fi
    sleep 0.5
done
if ! xdpyinfo -display "$DISPLAY" > /dev/null 2>&1; then
    echo 'Dedicated X server did not become available within 15 seconds.' >&2
    exit 1
fi

start dbus dbus-daemon --session --nofork --nopidfile --address="$DBUS_SESSION_BUS_ADDRESS"
for ((attempt = 0; attempt < 20; attempt++)); do
    if [[ -S "$XDG_RUNTIME_DIR/bus" ]]; then
        break
    fi
    sleep 0.1
done
if ! timeout 3s gdbus call --session --dest org.freedesktop.DBus \
    --object-path /org/freedesktop/DBus --method org.freedesktop.DBus.ListNames > /dev/null 2>&1; then
    echo 'Dedicated session bus did not become available within five seconds.' >&2
    exit 1
fi
# Activate the distro accessibility bus when available. This does not claim
# that the proprietary client exports an accessible tree or stable selectors.
if ! timeout 5s gdbus call --session --dest org.a11y.Bus \
    --object-path /org/a11y/bus --method org.freedesktop.DBus.Properties.Set \
    org.a11y.Status IsEnabled '<true>' > "$XDG_RUNTIME_DIR/accessibility.log" 2>&1; then
    echo 'Accessibility activation unavailable; inspect its private runtime log.' >&2
fi
start openbox openbox --sm-disable
start vnc x11vnc -norc -display "$DISPLAY" -auth "$XAUTHORITY" \
    -localhost -no6 -rfbport 5900 -passwdfile "$password_file" \
    -forever -shared -noxdamage -quiet
start novnc websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900
start wechat /usr/bin/wechat

# Any required child exiting ends this isolated session. Health is checked
# separately against a visible window owned by the actual vendor executable.
if wait -n "${pids[@]}"; then
    echo 'A dedicated desktop component exited; inspect its private runtime log.' >&2
else
    echo 'A dedicated desktop component failed; inspect its private runtime log.' >&2
fi
exit 1
