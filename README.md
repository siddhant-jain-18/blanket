# blanket

Blank your screen on GNOME/Wayland without locking or suspending — and wake it with any keyboard or touchpad input.

## Features

- **True screen blanking**: Uses Mutter's `PowerSaveMode` to turn off the display without locking or suspending.
- **Wake on any input**: A lightweight watcher grabs input devices while the screen is off, so the first keypress or touchpad tap wakes the display.
- **Swallows the wake key**: The event that wakes the screen is not delivered to any application.
- **Self-healing**: Automatically rescans for input devices, handles hotplugging, and cleans up stale PID files.
- **No hardcoded device paths**: Devices are discovered dynamically via `evdev`.
- **Extremely lightweight**: Runs at 0.0% CPU and uses ~20MB of RAM.

## Requirements

- Ubuntu 26.04 (or any GNOME/Wayland systemd distribution)
- `busctl` (part of systemd)
- `python3-evdev` (`sudo apt install python3-evdev`)
- Your user must be in the `input` group to read `/dev/input/event*`

## Installation

```bash
git clone https://github.com/siddhant-jain-18/blanket.git
cd blanket
chmod +x install.sh blanket.sh blanket-watcher.py uninstall.sh
./install.sh
```

The installer will:

1. Copy the CLI to `~/.local/bin/blanket`
2. Copy the watcher to `~/.local/bin/blanket-watcher`
3. Install and enable the systemd user service
4. Add you to the `input` group (if needed — you'll need to log out and back in)

## Usage

```bash
blanket off      # Blank the screen
blanket on       # Unblank the screen
blanket toggle   # Toggle between on and off
blanket status   # Print the current display power state
```

Bind a keyboard shortcut (e.g., `Ctrl+Alt+B`) to `blanket toggle` in **Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts**.

## Architecture

```text
blanket off  →  Sets Mutter PowerSaveMode = 3
             →  Sends SIGUSR1 to blanket-watcher
                →  Watcher verifies the screen is off
                →  Grabs all keyboard/touchpad devices
                →  Waits for input
                →  On first input: releases grabs, sets PowerSaveMode = 0
```

## Troubleshooting

### Screen blanks but nothing wakes it

1. Check the watcher status:

   ```bash
   systemctl --user status blanket-watcher
   journalctl --user -u blanket-watcher -f
   ```

2. Ensure you're in the `input` group:

   ```bash
   groups | grep input
   ```

   If not, add yourself and log out/back in:

   ```bash
   sudo usermod -aG input $USER
   ```

3. Restart the watcher:

   ```bash
   systemctl --user restart blanket-watcher
   ```

### "Permission denied" opening /dev/input/event*

You're not in the `input` group, or the group change hasn't taken effect. Log out and back in.

### Watcher fails to grab a device

Another process (like `evtest` or another grab tool) may be holding the device. The watcher will log a warning and continue with the remaining devices.

## Uninstall

```bash
./uninstall.sh
```

## License

MIT
