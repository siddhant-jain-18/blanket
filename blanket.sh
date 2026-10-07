#!/usr/bin/env bash
#
# blanket — blank the screen on GNOME/Wayland without locking or suspending.
# Any keyboard or touchpad input wakes the display back up.
#
#   blanket off [--force]   Blank the screen.
#   blanket on              Unblank the screen.
#   blanket toggle          Toggle between on and off.
#   blanket status [-v]     Print the current display power state.
#   blanket idle [N|off]    Blank after N seconds of inactivity.
#   blanket list            List the input devices the watcher has detected.
#   blanket doctor          Diagnose common setup problems.
#
set -euo pipefail

BUS="org.gnome.Mutter.DisplayConfig"
OBJ="/org/gnome/Mutter/DisplayConfig"
IFACE="org.gnome.Mutter.DisplayConfig"

POWER_ON=0
POWER_OFF=3

RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
SOCKET_PATH="$RUNTIME_DIR/blanket.sock"

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/blanket"
CONFIG_FILE="$CONFIG_DIR/config"

# --- watcher control -------------------------------------------------------

# Send a command to the watcher over its control socket. The response is a
# series of `key=value` lines whose first line is `ok=true` or `ok=false`.
#
# NOTE: the watcher replies with *multiple* lines and then closes the
# connection.  We must read until EOF — reading only up to the first "\n"
# truncates every response to just "ok=true", which silently broke
# `status -v`, `list` and `idle status`.
_ctl() {
    python3 - "$SOCKET_PATH" "$@" 2>/dev/null <<'PY'
import socket
import sys

path, *args = sys.argv[1:]
message = (" ".join(args) + "\n").encode()
try:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    sock.connect(path)
    sock.sendall(message)
    buf = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    sys.stdout.write(buf.decode("utf-8", "replace"))
    sys.stdout.flush()
except Exception as exc:
    sys.stdout.write("ok=false\nerror=%s\n" % exc)
    sys.stdout.flush()
PY
}

_watcher_alive() {
    local out
    out="$(_ctl ping || true)"
    [[ "$out" == ok=true* ]]
}

# Read a single `key=` value out of a key=value response body on stdin.
# Uses awk (not sed|head) so a duplicate key cannot trigger SIGPIPE and
# kill the script under `set -o pipefail`.
_field() {
    local key="$1"
    awk -v k="$key" '
        index($0, k "=") == 1 {
            sub("^" k "=", "")
            print
            exit
        }
    '
}

# --- display state ---------------------------------------------------------

_get_mode() {
    busctl --user get-property "$BUS" "$OBJ" "$IFACE" PowerSaveMode 2>/dev/null \
        | awk '{print $NF}' || true
}

_set_mode() {
    busctl --user set-property "$BUS" "$OBJ" "$IFACE" PowerSaveMode i "$1"
}

_mode_label() {
    case "${1:-}" in
        0) echo "on" ;;
        1) echo "standby" ;;
        2) echo "suspend" ;;
        3) echo "off" ;;
        *) echo "unknown" ;;
    esac
}

# --- config ----------------------------------------------------------------

_get_config() {
    local key="$1" default="$2" val
    [[ -r "$CONFIG_FILE" ]] || { printf '%s\n' "$default"; return; }
    val="$(sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" "$CONFIG_FILE" | tail -n1)"
    printf '%s\n' "${val:-$default}"
}

_set_config() {
    local key="$1" val="$2" tmp
    mkdir -p "$CONFIG_DIR"
    tmp="$(mktemp "${CONFIG_DIR}/config.XXXXXX")"
    if [[ -r "$CONFIG_FILE" ]]; then
        grep -v -E "^[[:space:]]*${key}[[:space:]]*=" "$CONFIG_FILE" > "$tmp" || true
    fi
    printf '%s=%s\n' "$key" "$val" >> "$tmp"
    mv "$tmp" "$CONFIG_FILE"
}

# --- helpers ---------------------------------------------------------------

_human_age() {
    local then="${1%%.*}" now ago
    now="$(date +%s)"
    [[ "$then" =~ ^[0-9]+$ ]] || { echo "unknown"; return; }
    ago=$(( now - then ))
    (( ago < 0 )) && ago=0
    if (( ago < 60 )); then
        echo "${ago}s ago"
    elif (( ago < 3600 )); then
        echo "$(( ago / 60 ))m ago"
    else
        echo "$(( ago / 3600 ))h ago"
    fi
}

usage() {
    cat <<'EOF'
blanket — blank the screen on GNOME/Wayland without locking or suspending.

Usage:
  blanket off [--force]   Blank the screen.
  blanket on              Unblank the screen.
  blanket toggle          Toggle between on and off.
  blanket status [-v]     Show display state (--verbose for watcher details).
  blanket idle [N|off]    Blank after N seconds of inactivity (default: show).
  blanket list            List the input devices the watcher has detected.
  blanket doctor          Diagnose common setup problems.

Any keyboard or touchpad input wakes the display. Requires the
blanket-watcher user service (run ./install.sh).
EOF
}

# --- commands --------------------------------------------------------------

cmd_off() {
    local force=0 arg
    for arg in "$@"; do
        [[ "$arg" == "--force" || "$arg" == "-f" ]] && force=1
    done

    if ! _watcher_alive; then
        if [[ $force -eq 1 ]]; then
            _set_mode "$POWER_OFF"
            return 0
        fi
        cat >&2 <<'EOF'
blanket: the watcher is not running, so nothing would wake the screen.
         Start it with:        systemctl --user start blanket-watcher
         Diagnose with:        blanket doctor
         Blank anyway with:    blanket off --force
EOF
        return 1
    fi

    local out
    out="$(_ctl blank || true)"
    if [[ "$out" != ok=true* ]]; then
        local err
        err="$(printf '%s' "$out" | _field error)"
        if [[ -n "$err" ]]; then
            echo "blanket: failed to blank ($err)" >&2
        else
            echo "blanket: failed to blank" >&2
        fi
        return 1
    fi
    if [[ "$out" == *armed=false* ]]; then
        echo "blanket: could not grab the input devices; screen left on." >&2
        return 1
    fi
}

cmd_on() {
    # Unblanking is always safe. Try the watcher first so it disarms
    # cleanly, but fall back to a direct modeset when the watcher is
    # unreachable or refused — otherwise a watcher that died mid-command
    # would leave the panel off forever.
    local out
    out="$(_ctl unblank || true)"
    if [[ "$out" != ok=true* ]]; then
        _set_mode "$POWER_ON"
    fi
}

cmd_toggle() {
    if [[ "$(_get_mode)" == "$POWER_OFF" ]]; then
        cmd_on
    else
        cmd_off "$@"
    fi
}

cmd_status() {
    local verbose=0 arg
    for arg in "$@"; do
        [[ "$arg" == "-v" || "$arg" == "--verbose" ]] && verbose=1
    done

    local mode
    mode="$(_get_mode)"

    if [[ $verbose -eq 0 ]]; then
        _mode_label "$mode"
        return 0
    fi

    printf 'display: %s\n' "$(_mode_label "$mode")"
    if ! _watcher_alive; then
        printf 'watcher: not running\n'
        return 1
    fi

    local out armed grabbed devices idle idle_for inhibited last_wake uptime
    out="$(_ctl status || true)"
    armed="$(_field armed <<<"$out")"
    grabbed="$(_field grabbed <<<"$out")"
    devices="$(_field devices <<<"$out")"
    idle="$(_field idle <<<"$out")"
    idle_for="$(_field idle_for <<<"$out")"
    inhibited="$(_field inhibited <<<"$out")"
    last_wake="$(_field last_wake <<<"$out")"
    uptime="$(_field uptime <<<"$out")"

    printf 'watcher: running\n'
    printf 'armed: %s\n' "${armed:-unknown}"
    printf 'devices: %s (%s grabbed)\n' "${devices:-0}" "${grabbed:-0}"
    if [[ "${idle:-0}" == 0 ]]; then
        printf 'idle auto-blank: off\n'
    else
        printf 'idle auto-blank: after %ss (idle for %ss)\n' "$idle" "${idle_for:-?}"
    fi
    if [[ "${inhibited:-false}" == true ]]; then
        printf 'idle inhibited: yes (something is holding the session awake)\n'
    fi
    if [[ -n "${last_wake:-}" ]]; then
        printf 'last wake: %s\n' "$(_human_age "$last_wake")"
    else
        printf 'last wake: never\n'
    fi
    printf 'watcher uptime: %ss\n' "${uptime:-?}"
}

cmd_list() {
    if ! _watcher_alive; then
        echo "blanket: watcher is not running (systemctl --user start blanket-watcher)" >&2
        return 1
    fi
    local out found=0 line entry
    out="$(_ctl list || true)"
    while IFS= read -r line; do
        case "$line" in
            device=*)
                found=1
                entry="${line#device=}"
                printf '  %-26s %s\n' "${entry%%|*}" "${entry#*|}"
                ;;
        esac
    done <<<"$out"
    if [[ $found -eq 0 ]]; then
        echo "  (no wake devices detected)"
    fi
}

cmd_idle() {
    local arg="${1:-}"
    if [[ -z "$arg" || "$arg" == status || "$arg" == show ]]; then
        _idle_show
        return 0
    fi

    local secs
    case "$arg" in
        off|disable|none) secs=0 ;;
        *[!0-9]*) echo "blanket: idle expects a number of seconds or 'off'" >&2; return 1 ;;
        *) secs="$arg" ;;
    esac

    _set_config idle "$secs"
    if _watcher_alive; then
        _ctl idle "$secs" >/dev/null 2>&1 || true
    else
        echo "blanket: saved; it will apply once the watcher is running." >&2
    fi

    if [[ "$secs" == 0 ]]; then
        echo "blanket: idle auto-blank disabled"
    else
        echo "blanket: screen will blank after ${secs}s of inactivity"
    fi
}

_idle_show() {
    local idle idle_for=""
    if _watcher_alive; then
        local out
        out="$(_ctl idle status || true)"
        idle="$(_field idle <<<"$out")"
        idle_for="$(_field idle_for <<<"$out")"
    else
        idle="$(_get_config idle 0)"
    fi
    [[ -n "$idle" ]] || idle=0
    if [[ "$idle" == 0 ]]; then
        echo "idle auto-blank: off"
    elif [[ -n "$idle_for" ]]; then
        printf 'idle auto-blank: after %ss (currently idle for %ss)\n' "$idle" "$idle_for"
    else
        printf 'idle auto-blank: after %ss\n' "$idle"
    fi
}

cmd_doctor() {
    local problems=0

    echo "blanket doctor"
    echo

    if command -v busctl >/dev/null 2>&1; then
        echo "  [ok]   busctl is available"
    else
        echo "  [FAIL] busctl not found"
        echo "         -> install systemd:  sudo apt install systemd"
        problems=$(( problems + 1 ))
    fi

    if python3 -c 'import evdev' 2>/dev/null; then
        echo "  [ok]   python3-evdev is available"
    else
        echo "  [FAIL] python3-evdev is not installed"
        echo "         -> sudo apt install python3-evdev"
        problems=$(( problems + 1 ))
    fi

    if id -nG 2>/dev/null | tr ' ' '\n' | grep -qx input; then
        echo "  [ok]   user is in the 'input' group"
    else
        echo "  [FAIL] user is not in the 'input' group (cannot read /dev/input/*)"
        echo "         -> sudo usermod -aG input \$USER   (then log out and back in)"
        problems=$(( problems + 1 ))
    fi

    if [[ -n "$(_get_mode)" ]]; then
        echo "  [ok]   Mutter DisplayConfig is reachable over D-Bus"
    else
        echo "  [FAIL] cannot read the display power state over D-Bus"
        echo "         -> are you in a GNOME/Wayland session?"
        problems=$(( problems + 1 ))
    fi

    if _watcher_alive; then
        echo "  [ok]   blanket-watcher is running"
        local out count
        out="$(_ctl list || true)"
        count="$(_field count <<<"$out")"
        if [[ "${count:-0}" -gt 0 ]]; then
            echo "  [ok]   watcher sees ${count} input device(s)"
        else
            echo "  [FAIL] watcher sees no input devices"
            echo "         -> check 'blanket list' and 'input' group membership"
            problems=$(( problems + 1 ))
        fi
    else
        echo "  [FAIL] blanket-watcher is not running"
        echo "         -> systemctl --user restart blanket-watcher"
        echo "         -> journalctl --user -u blanket-watcher -e"
        problems=$(( problems + 1 ))
    fi

    echo
    if [[ $problems -eq 0 ]]; then
        echo "All checks passed."
        return 0
    fi
    echo "${problems} problem(s) found."
    return 1
}

# --- entry -----------------------------------------------------------------

case "${1:-}" in
    off)    shift; cmd_off "$@" ;;
    on)     shift; cmd_on "$@" ;;
    toggle) shift; cmd_toggle "$@" ;;
    status) shift; cmd_status "$@" ;;
    idle)   shift; cmd_idle "$@" ;;
    list)   cmd_list ;;
    doctor) cmd_doctor ;;
    -h|--help|help) usage ;;
    "") usage >&2; exit 1 ;;
    *)  echo "blanket: unknown command: $1" >&2; usage >&2; exit 1 ;;
esac