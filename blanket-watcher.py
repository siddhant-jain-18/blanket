#!/usr/bin/env python3
"""
blanket-watcher — wake the display on any input while it has been blanked
by the `blanket` CLI.

Signals:

    SIGUSR1   arm    — grab the input devices so the wake keypress is
                       swallowed instead of reaching the focused app.
    SIGUSR2   disarm — release the grabs.

Any input event that arrives while the watcher is armed puts the display
back on (PowerSaveMode 0) and disarms. The event that triggered the wake
is not delivered to any application.

Runs as a systemd user service. The user must be in the `input` group to
read /dev/input/event*.
"""

from __future__ import annotations

import atexit
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

try:
    import evdev
    from evdev import ecodes
except ImportError:
    sys.stderr.write(
        "blanket-watcher: python3-evdev is required. "
        "Install it with: sudo apt install python3-evdev\n"
    )
    sys.exit(1)


# --- constants -------------------------------------------------------------

BUS = "org.gnome.Mutter.DisplayConfig"
OBJ = "/org/gnome/Mutter/DisplayConfig"
IFACE = "org.gnome.Mutter.DisplayConfig"

POWER_ON = 0
POWER_OFF = 3

RUNTIME_DIR = Path(
    os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
)
PIDFILE = RUNTIME_DIR / "blanket.pid"

REFRESH_INTERVAL = 5.0       # seconds between /dev/input rescans
POLL_TIMEOUT_MS = 100        # responsiveness of the signal-driven path
STATE_CACHE_TTL = 0.4        # seconds a busctl reading stays valid


# --- D-Bus helpers ---------------------------------------------------------

def _busctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["busctl", "--user", *args],
        capture_output=True,
        text=True,
        timeout=3,
    )


def display_power_state() -> int | None:
    try:
        cp = _busctl("get-property", BUS, OBJ, IFACE, "PowerSaveMode")
    except subprocess.TimeoutExpired:
        return None
    if cp.returncode != 0:
        return None
    try:
        return int(cp.stdout.strip().split()[-1])
    except (ValueError, IndexError):
        return None


def set_display_on() -> None:
    try:
        _busctl(
            "set-property", BUS, OBJ, IFACE,
            "PowerSaveMode", "i", str(POWER_ON),
        )
    except subprocess.TimeoutExpired:
        pass


# --- device classification -------------------------------------------------

def _is_wake_device(dev: "evdev.InputDevice") -> bool:
    """Keyboards, touchpads and mice; excludes accelerometers etc."""
    caps = dev.capabilities(absinfo=False)
    keys = set(caps.get(ecodes.EV_KEY, ()))

    typing = bool(keys & {
        ecodes.KEY_A, ecodes.KEY_Z, ecodes.KEY_ENTER, ecodes.KEY_SPACE,
    })
    pointer = bool(keys & {
        ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_TOUCH, ecodes.BTN_MOUSE,
    })
    return typing or pointer


# --- watcher ---------------------------------------------------------------

class Watcher:
    def __init__(self) -> None:
        self.poller = select.poll()

        self.devices: dict[str, "evdev.InputDevice"] = {}
        self.fd_to_path: dict[int, str] = {}

        self.armed = False
        self._state_cache: int | None = None
        self._state_at = 0.0
        self._last_refresh = 0.0
        self._running = True
        self._pending: list[str] = []

    # ---- device bookkeeping ----

    def _add_device(self, path: str) -> None:
        if path in self.devices:
            return
        try:
            dev = evdev.InputDevice(path)
        except (PermissionError, OSError):
            return
        if not _is_wake_device(dev):
            dev.close()
            return

        self.devices[path] = dev
        self.fd_to_path[dev.fd] = path
        self.poller.register(dev.fd, select.POLLIN)
        if self.armed:
            self._grab(dev)

    def _remove_device(self, path: str) -> None:
        dev = self.devices.pop(path, None)
        if dev is None:
            return
        self.fd_to_path.pop(dev.fd, None)
        try:
            self.poller.unregister(dev.fd)
        except (KeyError, OSError):
            pass
        if self.armed:
            self._ungrab(dev)
        try:
            dev.close()
        except OSError:
            pass

    def _refresh_devices(self) -> None:
        try:
            present = set(evdev.list_devices())
        except OSError:
            return
        for path in present - set(self.devices):
            self._add_device(path)
        for path in set(self.devices) - present:
            self._remove_device(path)

    # ---- grabs ----

    def _grab(self, dev: "evdev.InputDevice") -> None:
        try:
            dev.grab()
        except OSError as exc:
            print(
                f"blanket-watcher: grab {dev.path} failed: {exc}",
                file=sys.stderr, flush=True,
            )

    def _ungrab(self, dev: "evdev.InputDevice") -> None:
        try:
            dev.ungrab()
        except OSError:
            pass

    def arm(self) -> None:
        if self.armed:
            return
        # Only grab when the panel is actually off; otherwise we would
        # silently swallow the user's keystrokes on a visible desktop.
        if self._state(force=True) != POWER_OFF:
            return
        for dev in self.devices.values():
            self._grab(dev)
        self.armed = True
        print("blanket-watcher: armed", flush=True)

    def disarm(self) -> None:
        if not self.armed:
            return
        for dev in self.devices.values():
            self._ungrab(dev)
        self.armed = False
        print("blanket-watcher: disarmed", flush=True)

    # ---- display state cache ----

    def _state(self, *, force: bool = False) -> int | None:
        now = time.monotonic()
        if (
            not force
            and self._state_cache is not None
            and now - self._state_at < STATE_CACHE_TTL
        ):
            return self._state_cache
        self._state_cache = display_power_state()
        self._state_at = now
        return self._state_cache

    # ---- input handling ----

    def _handle_input(self, path: str) -> None:
        dev = self.devices.get(path)
        if dev is None:
            return
        try:
            events = list(dev.read())
        except OSError:
            self._remove_device(path)
            return

        # Ignore frames that contain only synchronisation events.
        if not any(e.type != ecodes.EV_SYN for e in events):
            return

        if self.armed:
            # Swallow the wake key, then wake the display.
            self.disarm()
            set_display_on()
            self._state_cache = POWER_ON
            self._state_at = time.monotonic()
            print("blanket-watcher: woke display", flush=True)

    # ---- signal handling ----

    def _on_arm(self, *_args) -> None:
        self._pending.append("arm")

    def _on_disarm(self, *_args) -> None:
        self._pending.append("disarm")

    def _drain_pending(self) -> None:
        while self._pending:
            cmd = self._pending.pop(0)
            if cmd == "arm":
                self.arm()
            elif cmd == "disarm":
                self.disarm()

    # ---- main loop ----

    def run(self) -> None:
        self._refresh_devices()
        self._last_refresh = time.monotonic()
        print(
            f"blanket-watcher: watching {len(self.devices)} device(s)",
            flush=True,
        )

        while self._running:
            try:
                events = self.poller.poll(POLL_TIMEOUT_MS)
            except InterruptedError:
                events = []

            for fd, mask in events:
                path = self.fd_to_path.get(fd)
                if path is None:
                    continue
                if mask & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
                    self._remove_device(path)
                    continue
                if mask & select.POLLIN:
                    self._handle_input(path)

            self._drain_pending()

            now = time.monotonic()
            if now - self._last_refresh >= REFRESH_INTERVAL:
                self._refresh_devices()
                self._last_refresh = now

        self.shutdown()

    def shutdown(self) -> None:
        was_armed = self.armed
        self.disarm()
        if was_armed:
            # Don't leave the user with a blank panel and no watcher.
            set_display_on()
        for path in list(self.devices):
            self._remove_device(path)

    def stop(self, *_args) -> None:
        self._running = False


# --- entry point -----------------------------------------------------------

def _acquire_pidfile() -> None:
    if PIDFILE.exists():
        try:
            pid = int(PIDFILE.read_text().strip())
            os.kill(pid, 0)
        except (ValueError, OSError):
            # Stale or unreadable; clean it up.
            try:
                PIDFILE.unlink()
            except OSError:
                pass
        else:
            print("blanket-watcher: already running", file=sys.stderr)
            sys.exit(0)

    PIDFILE.write_text(str(os.getpid()))
    atexit.register(lambda: PIDFILE.unlink(missing_ok=True))


def main() -> int:
    _acquire_pidfile()

    watcher = Watcher()
    signal.signal(signal.SIGUSR1, watcher._on_arm)
    signal.signal(signal.SIGUSR2, watcher._on_disarm)
    signal.signal(signal.SIGTERM, watcher.stop)
    signal.signal(signal.SIGINT, watcher.stop)

    try:
        watcher.run()
    except Exception as exc:
        watcher.shutdown()
        print(f"blanket-watcher: fatal: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
