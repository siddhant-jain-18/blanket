#!/usr/bin/env bash
set -euo pipefail

SERVICE="blanket-watcher.service"
PURGE=0

for arg in "$@"; do
    [[ "$arg" == "--purge" ]] && PURGE=1
done

# Never leave the panel blank when the thing that wakes it goes away.
busctl --user set-property org.gnome.Mutter.DisplayConfig \
    /org/gnome/Mutter/DisplayConfig org.gnome.Mutter.DisplayConfig \
    PowerSaveMode i 0 >/dev/null 2>&1 || true

echo "Stopping and disabling $SERVICE..."
systemctl --user disable --now "$SERVICE" 2>/dev/null || true

echo "Removing files..."
rm -f "$HOME/.local/bin/blanket"
rm -f "$HOME/.local/bin/blanket-watcher"
rm -f "$HOME/.config/systemd/user/$SERVICE"

echo "Removing shell completions..."
rm -f "$HOME/.local/share/bash-completion/completions/blanket"
rm -f "$HOME/.local/share/zsh/site-functions/_blanket"
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/fish/completions/blanket.fish"

if (( PURGE )); then
    echo "Removing configuration and state..."
    rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/blanket"
    rm -f "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/blanket.pid"
    rm -f "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/blanket.sock"
fi

echo "Reloading systemd user daemon..."
systemctl --user daemon-reload

echo "Uninstalled successfully."
if (( ! PURGE )); then
    echo "(Your preferences in ${XDG_CONFIG_HOME:-$HOME/.config}/blanket/config were kept.)"
    echo "(Run with --purge to remove them too.)"
fi