#!/usr/bin/env bash
set -euo pipefail

SERVICE="blanket-watcher.service"

echo "Stopping and disabling $SERVICE..."
systemctl --user disable --now "$SERVICE" 2>/dev/null || true

echo "Removing files..."
rm -f "$HOME/.local/bin/blanket"
rm -f "$HOME/.local/bin/blanket-watcher"
rm -f "$HOME/.config/systemd/user/$SERVICE"

echo "Reloading systemd user daemon..."
systemctl --user daemon-reload

echo "Uninstalled successfully."
