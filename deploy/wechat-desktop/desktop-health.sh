#!/bin/sh
set -eu

for component in xvfb dbus openbox vnc novnc wechat; do
    test -r "$XDG_RUNTIME_DIR/$component.pid"
    pid=$(cat "$XDG_RUNTIME_DIR/$component.pid")
    kill -0 "$pid"
done

client_pid=$(cat "$XDG_RUNTIME_DIR/wechat.pid")
test "$(readlink -f "/proc/$client_pid/exe")" = /opt/wechat/wechat
# Do not use window titles (which can contain private chats), a generic desktop
# window, a PID alone, or an open TCP port as a successful ready signal.
xdotool search --onlyvisible --pid "$client_pid" > /dev/null
curl --fail --silent --max-time 2 http://127.0.0.1:6080/vnc.html > /dev/null
