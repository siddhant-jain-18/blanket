#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
SERVICE="blanket-watcher.service"

BASH_COMP_DIR="$HOME/.local/share/bash-completion/completions"
ZSH_COMP_DIR="$HOME/.local/share/zsh/site-functions"
FISH_COMP_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/fish/completions"

say()  { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

# --- dependency checks -----------------------------------------------------

command -v busctl >/dev/null 2>&1 \
    || fail "busctl not found (is systemd installed?)"

command -v python3 >/dev/null 2>&1 \
    || fail "python3 not found"

python3 -c 'import evdev' 2>/dev/null \
    || fail "python3-evdev is not installed.
       Install it with:  sudo apt install python3-evdev"

[[ -f "$HERE/blanket.sh" ]]         || fail "blanket.sh missing next to install.sh"
[[ -f "$HERE/blanket-watcher.py" ]] || fail "blanket-watcher.py missing next to install.sh"
[[ -f "$HERE/$SERVICE" ]]           || fail "$SERVICE missing next to install.sh"

# --- optional self-test ----------------------------------------------------

if [[ -f "$HERE/tests/test_watcher.py" ]]; then
    say "Running unit tests..."
    if python3 -m unittest discover -s "$HERE/tests" -p 'test_watcher.py' -q >/dev/null 2>&1; then
        say "  [ok]   unit tests pass"
    else
        warn "  [warn] unit tests failed — rerun with:"
        warn "         python3 -m unittest discover -s tests -v"
    fi
    say
fi

# --- install files ---------------------------------------------------------

install -d "$BIN_DIR" "$UNIT_DIR"

install -m 0755 "$HERE/blanket.sh"         "$BIN_DIR/blanket"
install -m 0755 "$HERE/blanket-watcher.py" "$BIN_DIR/blanket-watcher"
install -m 0644 "$HERE/$SERVICE"           "$UNIT_DIR/$SERVICE"

# Shell completions (best effort — only when the source file exists).
if [[ -f "$HERE/completions/blanket.bash" ]]; then
    install -d "$BASH_COMP_DIR"
    install -m 0644 "$HERE/completions/blanket.bash" "$BASH_COMP_DIR/blanket"
fi
if [[ -f "$HERE/completions/_blanket" ]]; then
    install -d "$ZSH_COMP_DIR"
    install -m 0644 "$HERE/completions/_blanket" "$ZSH_COMP_DIR/_blanket"
fi
if [[ -f "$HERE/completions/blanket.fish" ]]; then
    install -d "$FISH_COMP_DIR"
    install -m 0644 "$HERE/completions/blanket.fish" "$FISH_COMP_DIR/blanket.fish"
fi

# --- input group -----------------------------------------------------------

me="$(id -un)"
needs_relogin=0
if ! id -nG "$me" | tr ' ' '\n' | grep -qx input; then
    cat <<MSG

Your user ($me) is not in the 'input' group, so the watcher cannot read
/dev/input/event*. Adding you now (sudo required):

MSG
    sudo usermod -aG input "$me"
    needs_relogin=1
fi

# --- enable the service ----------------------------------------------------

# Make sure the screen is on before we replace the watcher: an old watcher
# that is killed while the panel is blank would strand the display.
busctl --user set-property org.gnome.Mutter.DisplayConfig \
    /org/gnome/Mutter/DisplayConfig org.gnome.Mutter.DisplayConfig \
    PowerSaveMode i 0 >/dev/null 2>&1 || true

systemctl --user daemon-reload
# Clear any prior failed state so a stale "restart counter" from earlier
# experiments does not trigger immediate rate-limiting on the fresh start.
systemctl --user reset-failed "$SERVICE" 2>/dev/null || true
systemctl --user enable "$SERVICE"
# `enable --now` would leave an already-running OLD watcher in place after
# an upgrade (the new CLI then cannot talk to it).  Always restart.
systemctl --user restart "$SERVICE"

# Give it a moment to come up, and make sure it *stayed* up — a crash on
# start would otherwise be reported as a success (the 0.5s check used to
# race against the very first restart).
sleep 2
if systemctl --user is-active --quiet "$SERVICE"; then
    restarts="$(systemctl --user show -p NRestarts --value "$SERVICE" 2>/dev/null || echo 0)"
    if [[ "${restarts:-0}" == "0" ]]; then
        say "blanket-watcher service is active."
    else
        warn "blanket-watcher is active but has restarted ${restarts} time(s)."
        warn "Recent log:"
        warn
        journalctl --user -u "$SERVICE" -n 20 --no-pager >&2 || true
    fi
else
    warn "blanket-watcher did not start cleanly. Recent log:"
    warn
    journalctl --user -u "$SERVICE" -n 20 --no-pager >&2 || true
fi

# --- summary ---------------------------------------------------------------

cat <<'MSG'

Installed.

Try:
    blanket off
    blanket on
    blanket toggle
    blanket status --verbose
    blanket idle 300     # blank after 5 minutes of inactivity
    blanket doctor       # diagnose any problems

Bind a shortcut (Settings -> Keyboard -> View and Customize Shortcuts ->
Custom Shortcuts) to:  blanket toggle

Tab-completion for bash, zsh and fish has been installed too.
MSG

if (( needs_relogin )); then
    cat <<'MSG'

>> Log out and back in for the 'input' group change to take effect.
>> Then run:  systemctl --user restart blanket-watcher
MSG
fi