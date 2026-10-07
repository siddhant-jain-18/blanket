#!/usr/bin/env bash
set -euo pipefail

SERVICE="blanket-watcher.service"

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

echo "Reloading systemd user daemon..."
systemctl --user daemon-reload

echo "Uninstalled successfully."
echo "(Your preferences in ${XDG_CONFIG_HOME:-$HOME/.config}/blanket/config were kept.)"
