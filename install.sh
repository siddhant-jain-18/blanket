#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
SERVICE="blanket-watcher.service"

BASH_COMP_DIR="$HOME/.local/share/bash-completion/completions"
ZSH_COMP_DIR="$HOME/.local/share/zsh/site-functions"
FISH_COMP_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/fish/completions"

command -v busctl >/dev/null 2>&1 || {
    echo "error: busctl not found (is systemd installed?)" >&2
    exit 1
}

if ! python3 -c 'import evdev' 2>/dev/null; then
    echo "error: python3-evdev is not installed." >&2
    echo "       Install it with:  sudo apt install python3-evdev" >&2
    exit 1
fi

install -d "$BIN_DIR" "$UNIT_DIR"

install -m 0755 "$HERE/blanket.sh"         "$BIN_DIR/blanket"
install -m 0755 "$HERE/blanket-watcher.py" "$BIN_DIR/blanket-watcher"
install -m 0644 "$HERE/$SERVICE"           "$UNIT_DIR/$SERVICE"

# Shell completions (best effort — only installed when the source exists).
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

me="$(id -un)"
if ! id -nG "$me" | tr ' ' '\n' | grep -qx input; then
    cat <<MSG

Your user ($me) is not in the 'input' group, so the watcher cannot read
/dev/input/event*. Adding you now (sudo required):

MSG
    sudo usermod -aG input "$me"
    cat <<MSG

>> Log out and back in for the group change to take effect.
>> Then re-run:  systemctl --user restart $SERVICE

MSG
fi

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE"

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
