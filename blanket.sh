#!/usr/bin/env bash
#
# blanket — blank the screen on GNOME/Wayland without locking or suspending.
# Any keyboard or touchpad input wakes the display back up.
#
#   blanket off      Blank the screen.
#   blanket on       Unblank the screen.
#   blanket toggle   Toggle between on and off.
#   blanket status   Print the current display power state.
#
set -euo pipefail

BUS="org.gnome.Mutter.DisplayConfig"
OBJ="/org/gnome/Mutter/DisplayConfig"
IFACE="org.gnome.Mutter.DisplayConfig"

POWER_ON=0
POWER_OFF=3

RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
PIDFILE="$RUNTIME_DIR/blanket.pid"

# --- helpers ---------------------------------------------------------------

_watcher_pid() {
    [[ -r "$PIDFILE" ]] || return 1
    local pid
    pid="$(<"$PIDFILE")"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    printf '%s' "$pid"
}

_notify_watcher() {
    # $1 = USR1 (arm) or USR2 (disarm)
    local pid
    pid="$(_watcher_pid)" || return 0
    kill -"$1" "$pid" 2>/dev/null || true
}

_get_mode() {
    busctl --user get-property "$BUS" "$OBJ" "$IFACE" PowerSaveMode 2>/dev/null \
        | awk '{print $NF}'
}

_set_mode() {
    busctl --user set-property "$BUS" "$OBJ" "$IFACE" PowerSaveMode i "$1"
}

usage() {
    cat <<'EOF'
blanket — blank the screen on GNOME/Wayland without locking or suspending.

Usage:
  blanket off      Blank the screen.
  blanket on       Unblank the screen.
  blanket toggle   Toggle between off and on.
  blanket status   Print the current display power state.

Any keyboard or touchpad input wakes the display. Requires the
blanket-watcher user service (run ./install.sh).
EOF
}

# --- commands --------------------------------------------------------------

cmd_off() {
    # Turn the panel off first, then ask the watcher to arm. The watcher
    # only grabs when the display is genuinely off, so a keypress can
    # never be swallowed on a visible desktop.
    _set_mode "$POWER_OFF"
    _notify_watcher USR1
}

cmd_on() {
    # Release grabs before turning the panel back on.
    _notify_watcher USR2
    _set_mode "$POWER_ON"
}

cmd_toggle() {
    if [[ "$(_get_mode)" == "$POWER_OFF" ]]; then
        cmd_on
    else
        cmd_off
    fi
}

cmd_status() {
    case "$(_get_mode)" in
        0) echo "on" ;;
        1) echo "standby" ;;
        2) echo "suspend" ;;
        3) echo "off" ;;
        *) echo "unknown" ;;
    esac
}

# --- entry -----------------------------------------------------------------

case "${1:-}" in
    off)    cmd_off ;;
    on)     cmd_on ;;
    toggle) cmd_toggle ;;
    status) cmd_status ;;
    -h|--help|help) usage ;;
    "") usage >&2; exit 1 ;;
    *)  echo "blanket: unknown command: $1" >&2; usage >&2; exit 1 ;;
esac
