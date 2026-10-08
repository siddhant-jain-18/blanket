# blanket

Blank your screen on GNOME/Wayland without locking or suspending — and wake it with any keyboard or touchpad input.

## Features

- **True screen blanking**: Uses Mutter's `PowerSaveMode` to turn off the display without locking or suspending.
- **Wake on any input**: A lightweight watcher grabs input devices while the screen is off, so the first keypress or touchpad tap wakes the display.
- **Swallows the wake key**: The event that wakes the screen is not delivered to any application.
- **Idle auto-blank**: Blank the screen automatically after N seconds of inactivity — a behavior instead of a command.
- **Respects video playback**: Idle blanking is skipped while an application inhibits idle (video calls, fullscreen video, …).
- **Self-healing**: Automatically rescans for input devices, handles hotplugging, and cleans up stale PID files. If the watcher restarts while the screen is blank, it restores the display on startup.
- **Safe by design**: `blanket off` refuses to blank the screen when the watcher is not running, so you can never get stranded on a black screen. `blanket on` always works.
- **No hardcoded device paths**: Devices are discovered dynamically via `evdev`.
- **Extremely lightweight**: Runs at ~0.0% CPU and uses ~20MB of RAM.

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
4. Install bash, zsh and fish completions
5. Add you to the `input` group (if needed — you'll need to log out and back in)

## Usage

```bash
blanket off            # Blank the screen
blanket on             # Unblank the screen
blanket toggle         # Toggle between on and off
blanket status         # Print the current display power state
blanket status -v      # ...with watcher, device and idle details
blanket idle 300       # Blank after 5 minutes of inactivity
blanket idle off       # Disable idle auto-blanking
blanket idle           # Show the current idle setting
blanket list           # List the input devices the watcher has detected
blanket doctor         # Diagnose the most common setup problems
blanket logs [N]       # Last N lines of the watcher log (default 40)
blanket version        # Print the version
```

Bind a keyboard shortcut (e.g., `Ctrl+Alt+B`) to `blanket toggle` in **Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts**.

### Idle auto-blank

`blanket idle 300` turns blanket from a command you remember to run into a
behavior that just works. The watcher tracks input activity on the devices it
monitors and blanks the screen after the given number of seconds. The setting
is stored in `~/.config/blanket/config` and survives reboots:

```ini
idle=300
respect_inhibitors=1
```

Set `respect_inhibitors=0` if you would rather blank on a schedule even while a
video is playing. Send `blanket idle <N>` again (or restart the service) to
apply a config-file change.

### Diagnosing problems

`blanket doctor` checks, in order:

1. `busctl` is available
2. `python3-evdev` is installed
3. Your user is in the `input` group
4. Mutter's `DisplayConfig` is reachable over D-Bus
5. `blanket-watcher` is running and sees at least one input device

Each failure prints an actionable fix, and the command exits non-zero if
anything is wrong — handy in scripts.

## Architecture

```text
blanket off  →  CLI asks the watcher over $XDG_RUNTIME_DIR/blanket.sock
             →  Watcher sets Mutter PowerSaveMode = 3 and grabs the input devices
             →  Waits for input
             →  On first input: releases grabs, sets PowerSaveMode = 0
blanket on   →  Watcher disarms and restores PowerSaveMode = 0
```

The CLI and the watcher talk over a small Unix-domain control socket
(`$XDG_RUNTIME_DIR/blanket.sock`) using newline-delimited `key=value` messages.
This carries values (unlike signals), cannot lose commands, and lets
`status --verbose`, `list` and `idle` query live watcher state.

### Safety

- `blanket off` will **not** blank the screen unless the watcher answers on the
  socket, unless you pass `--force`. Blanking with no watcher is the one state
  from which there is no way back.
- If the watcher cannot grab any input device — no wake-capable devices
  discovered, no `input` group membership, or every grab held by another
  process — it refuses to blank at all, and the display stays on.
- **Held keys are respected.** `blanket off` is usually fired by a keyboard
  shortcut, so its keys are still down. The watcher waits (up to 1.5 s) for
  them to be physically released *before* grabbing the keyboard. Grabbing
  mid-press would hide the key-up events from the compositor, leaving
  modifiers "stuck" (shortcuts stop working) and key-repeat re-firing the
  shortcut — which shows up as the screen flickering on and off.
- **The whole wake gesture is swallowed.** The key that wakes the screen is
  kept grabbed until it is released (max 1.5 s), so applications never see
  half a keystroke.
- `blanket on` is idempotent: if the screen is already on it does nothing,
  and it verifies the panel really came back (forcing it if not).
- If the watcher shuts down or starts up while the screen is blank, it
  turns the display back on.
- The control socket is private (mode 0600) and the watcher rejects peers
  that are not the same user.
- If the watcher starts up (e.g. after a crash or `systemctl --user restart`)
  while the screen is blank, it restores the display on startup.
- `arm`/`disarm`/`on`/`off` are all idempotent; running any of them twice is
  harmless.

## Troubleshooting

### Screen blanks but nothing wakes it

1. Check the watcher status:

   ```bash
   systemctl --user status blanket-watcher
   journalctl --user -u blanket-watcher -f
   ```

2. Run the built-in diagnosis:

   ```bash
   blanket doctor
   ```

3. Restart the watcher:

   ```bash
   systemctl --user restart blanket-watcher
   ```

### "Permission denied" opening /dev/input/event*

You're not in the `input` group, or the group change hasn't taken effect:

```bash
sudo usermod -aG input $USER
```

Then log out and back in.

### Blanking does nothing

If you are in a session that doesn't expose Mutter's `DisplayConfig`
(e.g. a nested compositor or a different desktop), `blanket doctor` will report
that the D-Bus interface is unreachable.

### After upgrading, `blanket off` says the watcher is not running

An older watcher is probably still running and does not speak the new socket
protocol. `./install.sh` now restarts the service for you; to do it by hand:

```bash
systemctl --user restart blanket-watcher
blanket doctor
```

### The screen flickers or wakes instantly

Run `blanket logs 20`. Every wake logs what caused it, e.g.
`woke display (Some Touchpad: EV_ABS code=53 value=...)`. A device that wakes
the screen on its own (a noisy touchpad, a mouse with a jittery sensor) will
show up there — please include that line when reporting a problem.

### Watcher fails to grab a device

Another process (like `evtest` or another grab tool) may be holding the device.
The watcher will log a warning and continue with the remaining devices.

### The screen doesn't blank during a video

That's intentional: while an application inhibits idle, the idle timer is
skipped. Run `blanket status -v` to see whether `idle inhibited: yes` is
reported and by what. Set `respect_inhibitors=0` in the config to override.

## Development

```bash
python3 -m unittest discover -s tests -v   # unit + end-to-end tests
shellcheck blanket.sh install.sh uninstall.sh
```

The end-to-end tests (`tests/test_integration.py`) run the real CLI against
the real watcher with a fake `busctl` and a fake input device, so they need no
GNOME session and no `/dev/input` access.

## Uninstall

```bash
./uninstall.sh
```

## License

MIT
