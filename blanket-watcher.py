#!/usr/bin/env python3
"""
blanket-watcher — wake the display on any input while it has been blanked
by the `blanket` CLI, and optionally blank it automatically after a period
of inactivity.

Signals (kept for compatibility; the CLI now prefers the control socket):

    SIGUSR1   arm    — grab the input devices so the wake keypress is
                       swallowed instead of reaching the focused app.
    SIGUSR2   disarm — release the grabs.

Control socket:

    $XDG_RUNTIME_DIR/blanket.sock

The CLI talks to the watcher here. Each request is a single line and each
response is a series of `key=value` lines whose first line is `ok=true` or
`ok=false`. Commands: ping, status, list, blank, unblank, arm, disarm,
idle [N|off|status], reload.

Runs as a systemd user service. The user must be in the `input` group to
read /dev/input/event*.

Safety properties:

  * never blanks unless at least one input device is grabbed (otherwise
    nothing could wake the panel);
  * waits for the keys that invoked `blank` to be physically released
    before grabbing, so the compositor never sees stuck keys;
  * swallows the whole wake gesture (press *and* release) before handing
    the devices back, bounded by RELEASE_HOLD;
  * restores the panel on shutdown and on startup if it was left blank.
"""

from __future__ import annotations

import atexit
import os
import select
import signal
import socket
import struct
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

SESSION_BUS = "org.gnome.SessionManager"
SESSION_OBJ = "/org/gnome/SessionManager"
SESSION_IFACE = "org.gnome.SessionManager"
INHIBIT_IDLE = 8               # gsm inhibitor flag: "session is idle"

POWER_ON = 0
POWER_OFF = 3

RUNTIME_DIR = Path(
    os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
)
PIDFILE = RUNTIME_DIR / "blanket.pid"
SOCKET_PATH = RUNTIME_DIR / "blanket.sock"

CONFIG_HOME = Path(
    os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
)
CONFIG_PATH = CONFIG_HOME / "blanket" / "config"

REFRESH_INTERVAL = 5.0       # seconds between /dev/input rescans
POLL_TIMEOUT_MS = 100        # responsiveness of the signal-driven path
STATE_CACHE_TTL = 0.4        # seconds a busctl reading stays valid
INHIBIT_CACHE_TTL = 2.0      # seconds an inhibitor reading stays valid
IDLE_TICK = 1.0              # seconds between idle-timer evaluations
DRAIN_LIMIT = 64             # bounded discard of stale events per device
RELEASE_WAIT = 1.5           # max seconds to wait for held keys before blanking
RELEASE_HOLD = 1.5           # max seconds to keep swallowing a wake gesture
CLIENT_TIMEOUT = 5.0         # drop control clients that never finish a request


# --- configuration ---------------------------------------------------------

def load_config() -> dict:
    """Read the simple `key=value` config file, with sane defaults."""
    cfg = {"idle": 0, "respect_inhibitors": True}
    try:
        text = CONFIG_PATH.read_text()
    except OSError:
        return cfg

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key == "idle":
            try:
                cfg["idle"] = max(0, int(value))
            except ValueError:
                pass
        elif key == "respect_inhibitors":
            cfg["respect_inhibitors"] = value.lower() in (
                "1", "true", "yes", "on",
            )
    return cfg


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


def set_display_power(mode: int) -> bool:
    try:
        cp = _busctl(
            "set-property", BUS, OBJ, IFACE,
            "PowerSaveMode", "i", str(mode),
        )
    except subprocess.TimeoutExpired:
        return False
    return cp.returncode == 0


def idle_inhibited() -> bool:
    """True when an app (video player, call, …) is holding the session awake.

    Fails open: if the session manager is unreachable we assume nothing is
    inhibiting idle, otherwise the idle timer would silently never fire.
    """
    try:
        cp = _busctl(
            "call", SESSION_BUS, SESSION_OBJ, SESSION_IFACE,
            "IsInhibited", "u", str(INHIBIT_IDLE),
        )
    except subprocess.TimeoutExpired:
        return False
    if cp.returncode != 0:
        return False
    out = cp.stdout.strip().split()
    return bool(out) and out[-1].lower() == "true"


def await_power_state(mode: int, attempts: int = 5,
                      delay: float = 0.1) -> bool:
    """Poll until the compositor reports `mode`, or give up.

    Mutter applies PowerSaveMode asynchronously: a single immediate
    read-back can still show the previous value. Polling briefly tells a
    slow-but-successful transition apart from a real failure — without
    this, blanking falsely rolls back (flash off/on) on slow hardware.
    """
    for _ in range(attempts):
        if display_power_state() == mode:
            return True
        time.sleep(delay)
    return False


# --- device classification -------------------------------------------------

def _pressed_keys(dev) -> list:
    """Keys/buttons currently held down on `dev` (best effort, never raises)."""
    try:
        keys = dev.active_keys()
    except (OSError, AttributeError, TypeError):
        return []
    return list(keys) if isinstance(keys, (list, tuple, set)) else []



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
    def __init__(self, config: dict | None = None) -> None:
        config = config or load_config()

        self.poller = select.poll()

        self.devices: dict[str, "evdev.InputDevice"] = {}
        self.fd_to_path: dict[int, str] = {}
        self.clients: dict[int, tuple[socket.socket, bytearray]] = {}
        self.sock: socket.socket | None = None

        self.armed = False
        self._releasing = False          # wake gesture still being swallowed
        self._release_deadline = 0.0
        self.client_born: dict[int, float] = {}
        self.grabbed: set[str] = set()
        self.idle_seconds = max(0, int(config.get("idle", 0)))
        self.respect_inhibitors = bool(config.get("respect_inhibitors", True))

        self._state_cache: int | None = None
        self._state_at = 0.0

        self._inhibit_cache: bool | None = None
        self._inhibit_at = 0.0

        self._last_refresh = 0.0
        self._last_idle_check = 0.0
        self._last_activity = time.monotonic()
        self._last_wake: float | None = None
        self._started = time.time()

        self._running = True
        self._pending: list[str] = []

    # ---- lifecycle ----

    def start(self) -> None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        try:
            SOCKET_PATH.unlink()
        except OSError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.setblocking(False)
        old_umask = os.umask(0o077)       # socket is user-private from birth
        try:
            self.sock.bind(str(SOCKET_PATH))
        finally:
            os.umask(old_umask)
        self.sock.listen(8)
        self.poller.register(self.sock.fileno(), select.POLLIN)

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
        if self.armed or self._releasing or path in self.grabbed:
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

    def _grab(self, dev: "evdev.InputDevice") -> bool:
        """Grab one device; return True on success.

        On success the kernel queue is drained, discarding anything queued
        before the grab (e.g. the key-up events from the Ctrl+Alt+B / Enter
        that invoked us) so a stale release is never mistaken for a fresh
        wake gesture.
        """
        path = getattr(dev, "path", None)
        try:
            dev.grab()
        except OSError as exc:
            print(
                f"blanket-watcher: grab {path or '?'} failed: {exc}",
                file=sys.stderr, flush=True,
            )
            if path is not None:
                self.grabbed.discard(path)
            return False
        self._drain(dev)
        if path is not None:
            self.grabbed.add(path)
        return True

    def _ungrab(self, dev: "evdev.InputDevice") -> None:
        try:
            dev.ungrab()
        except OSError:
            pass
        finally:
            path = getattr(dev, "path", None)
            if path is not None:
                self.grabbed.discard(path)

    def _drain(self, dev: "evdev.InputDevice", limit: int = DRAIN_LIMIT) -> None:
        """Discard queued events on one device (bounded).

        The cap matters: an actively-used device (mouse being wiggled
        during `blanket off`) keeps producing events, so an unbounded
        drain would spin forever and blank() would never return.
        """
        read_one = getattr(dev, "read_one", None)
        if read_one is None:
            return
        try:
            for _ in range(limit):
                if read_one() is None:
                    break
        except (OSError, AttributeError, TypeError):
            pass

    def _drain_all(self) -> None:
        for dev in self.devices.values():
            self._drain(dev)

    def _grab_all(self) -> int:
        """Grab every known device; return the number successfully grabbed."""
        ok = 0
        for dev in self.devices.values():
            if self._grab(dev):
                ok += 1
        return ok

    def _rollback_grabs(self) -> None:
        """Release any grabs we managed to acquire during a failed arm/blank."""
        for dev in self.devices.values():
            self._ungrab(dev)
        self.grabbed.clear()

    def arm(self) -> bool:
        if self.armed:
            return True
        # Only grab when the panel is actually off; otherwise we would
        # silently swallow the user's keystrokes on a visible desktop.
        if self._state(force=True) != POWER_OFF:
            return False
        grabbed = self._grab_all()
        self._drain_all()
        # An *empty* device list is as bad as a failed grab: with nothing
        # grabbed nothing can wake us, so arming would strand the user on
        # a black screen.  This is the case that the previous
        # `if self.devices and …` check silently let through.
        if grabbed == 0:
            self._rollback_grabs()
            print(
                "blanket-watcher: arm refused: could not grab any device",
                file=sys.stderr, flush=True,
            )
            return False
        self.armed = True
        print("blanket-watcher: armed", flush=True)
        return True

    def disarm(self) -> bool:
        if not self.armed and not self._releasing:
            return True
        was_armed = self.armed
        for dev in self.devices.values():
            self._ungrab(dev)
        self.grabbed.clear()
        self.armed = False
        self._releasing = False
        if was_armed:
            print("blanket-watcher: disarmed", flush=True)
        return True

    # ---- held-key handling ----

    def _any_pressed(self) -> bool:
        return any(_pressed_keys(dev) for dev in self.devices.values())

    def _wait_for_release(self, timeout: float = RELEASE_WAIT) -> bool:
        """Wait until no key/button is held on any watched device.

        `blanket off` is normally fired by a keyboard shortcut, so its keys
        are still down when we get here.  If we grab the keyboard now, the
        compositor never sees the key-up events: it believes Ctrl+Alt+B is
        still held (stuck modifiers, shortcuts that stop working, key
        repeat that re-fires the shortcut — i.e. flicker).  Waiting for the
        physical release first lets the compositor finish the gesture.
        Bounded: a genuinely stuck key never blocks blanking forever.
        """
        deadline = time.monotonic() + timeout
        while self._any_pressed():
            if time.monotonic() >= deadline:
                print(
                    "blanket-watcher: keys still held after "
                    f"{timeout:.1f}s; blanking anyway",
                    file=sys.stderr, flush=True,
                )
                return False
            time.sleep(0.02)
        return True

    def _tick_release(self) -> None:
        """Finish swallowing a wake gesture, then hand the devices back."""
        if not self._releasing:
            return
        if self._any_pressed() and time.monotonic() < self._release_deadline:
            return
        self.disarm()

    # ---- blanking ----

    def blank(self) -> bool:
        """Turn the panel off and arm. Rolls back if blanking fails.

        The grab happens *before* the panel is switched off so the
        key-release events from the shortcut that invoked us (Ctrl+Alt+B,
        Enter, …) can never reach the compositor in the window between
        "display off" and "inputs grabbed" — that window is what used to
        wake the screen straight back up.

        Success is decided by the result of the power-set request itself.
        (A synchronous read-back here would be racy: Mutter applies the
        mode change asynchronously, so an immediate re-read can still show
        the old value and cause a false rollback — the screen flashing off
        and straight back on.)
        """
        # Idempotent: a repeated `blanket off` while already blank must not
        # re-grab everything (which spams EBUSY) — just clear stale input
        # and restart the idle clock.
        if self.armed:
            self._drain_all()
            self._last_activity = time.monotonic()
            return True

        # 0. Let the shortcut that invoked us finish (see _wait_for_release),
        #    and settle any wake gesture that is still being swallowed.
        self.disarm()
        self._wait_for_release()

        # 1. Pre-grab: swallow the trigger keystrokes before they leak
        #    through to Mutter (which would auto-unblank on input).
        grabbed = self._grab_all()
        self._drain_all()
        # A successful grab — at least one device — is a hard precondition.
        # With *zero* grabbed devices (no wake devices discovered yet, none
        # in the `input` group, or every grab held by another process)
        # nothing can wake the panel, so blanking would leave the user on a
        # black screen with no way back.
        if grabbed == 0:
            self._rollback_grabs()
            print(
                "blanket-watcher: could not grab any input device; "
                "left display on",
                file=sys.stderr, flush=True,
            )
            return False

        # 2. Now it is safe to switch the panel off.
        if not set_display_power(POWER_OFF):
            self._rollback_grabs()
            print(
                "blanket-watcher: could not blank display; left display on",
                file=sys.stderr, flush=True,
            )
            return False

        # 3. The compositor applies the change asynchronously: wait briefly
        #    for it to show up instead of treating a stale read-back as a
        #    failure (that false rollback is a flash-off-and-back-on).
        if not await_power_state(POWER_OFF):
            self._rollback_grabs()
            set_display_power(POWER_ON)
            print(
                "blanket-watcher: panel never went off; left display on",
                file=sys.stderr, flush=True,
            )
            return False

        self.armed = True
        self._state_cache = POWER_OFF
        self._state_at = time.monotonic()

        # 4. Discard anything that arrived during the bus round-trips and
        #    restart the idle clock from a clean slate.
        self._drain_all()
        self._last_activity = time.monotonic()
        print("blanket-watcher: armed", flush=True)
        return True

    def _wake(self, reason: str = "request") -> None:
        """Restore the panel and hand inputs back (never leaves a grab).

        Power is restored *before* the grabs are released: the moment
        inputs flow again the compositor sees them and may drive the panel
        itself, so setting power afterwards races it — that race is the
        visible on/off flicker. The set is issued exactly once (no retry
        storm: every extra modeset is another flash); the read-back below
        only decides whether to log a warning.
        """
        set_display_power(POWER_ON)
        if not await_power_state(POWER_ON, attempts=3):
            print(
                "blanket-watcher: warning: panel did not report back on",
                file=sys.stderr, flush=True,
            )
        # Timestamp the cache *after* the bus round-trips so it starts its
        # TTL from now, not from before the await (which could already be
        # older than STATE_CACHE_TTL by the time we get here).
        self._state_cache = POWER_ON
        self._state_at = time.monotonic()
        self._last_wake = time.time()
        # Ungrab even if the panel misbehaved: a grabbed keyboard with a
        # lit screen is worse than any power-state mismatch.
        #
        # If the wake gesture is still in progress (key held, finger on
        # the touchpad) keep swallowing it until it ends, so the
        # compositor never sees a half-finished gesture or kernel
        # auto-repeat.  _tick_release() hands the devices back, bounded
        # by RELEASE_HOLD so we can never strand the user.
        if self.armed and self._any_pressed():
            self.armed = False
            self._releasing = True
            self._release_deadline = time.monotonic() + RELEASE_HOLD
        else:
            self.disarm()
        # Restart the idle clock: the user is by definition present now.
        # Without this, `blanket on` (or any wake that lands while the
        # idle timer is already expired) is followed on the very next tick
        # by an immediate re-blank.
        self._last_activity = time.monotonic()
        print(f"blanket-watcher: woke display ({reason})", flush=True)

    def unblank(self) -> None:
        # Nothing to undo when the panel is already on and we hold no grabs.
        # (An unconditional modeset here is a pointless visible flash.)
        # A failed read-back (None) falls through: fail awake.
        if (
            not self.armed
            and not self._releasing
            and self._state(force=True) == POWER_ON
        ):
            return
        self._wake("unblank")

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

    def _inhibited(self) -> bool:
        if not self.respect_inhibitors:
            return False
        now = time.monotonic()
        if (
            self._inhibit_cache is not None
            and now - self._inhibit_at < INHIBIT_CACHE_TTL
        ):
            return self._inhibit_cache
        self._inhibit_cache = idle_inhibited()
        self._inhibit_at = now
        return self._inhibit_cache

    def idle_time(self) -> float:
        return time.monotonic() - self._last_activity

    # ---- input handling ----

    @staticmethod
    def _is_wake_event(events) -> bool:
        """True if the frame holds a genuine wake gesture.

        Key/button *releases* (value 0) never wake: they are almost always
        the tail end of the shortcut that blanked the screen (Ctrl+Alt+B,
        Enter, …). Presses (1), autorepeats (2), pointer motion and switch
        events wake immediately — including right after blanking, so the
        very next real keypress always brings the screen back.
        """
        for e in events:
            if e.type == ecodes.EV_KEY:
                if e.value != 0:  # press (1) or autorepeat (2)
                    return True
            elif e.type == ecodes.EV_REL:
                if e.value != 0:  # real motion; zero-delta frames are noise
                    return True
            elif e.type in (ecodes.EV_ABS, ecodes.EV_SW):
                # Position/switch changes mean real contact. A zero-valued
                # EV_ABS frame can be noise, but waking is the safe
                # direction here (fail awake, never stranded).
                return True
            # EV_SYN / EV_MSC and anything else: not a wake gesture.
        return False

    @staticmethod
    def _describe_wake(dev, events) -> str:
        """Short, loggable description of what woke the display."""
        for e in events:
            if e.type == ecodes.EV_SYN:
                continue
            name = getattr(dev, "name", "?")
            kind = ecodes.EV.get(e.type, e.type)
            return f"{name}: {kind} code={e.code} value={e.value}"
        return "unknown"

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

        now = time.monotonic()
        self._last_activity = now

        if not self.armed:
            return

        # Swallow releases; only a real press/motion wakes the display.
        # Releases are filtered at *any* age (not just in a settle window),
        # so late-arriving key-ups from the blank shortcut can never wake
        # us, while a genuine new press wakes instantly with no delay.
        if not self._is_wake_event(events):
            return

        # The wake keypress was already swallowed by the grab; restore the
        # panel and hand the devices back.
        self._wake(self._describe_wake(dev, events))

    def _maybe_idle_blank(self) -> None:
        if self.armed or self.idle_seconds <= 0:
            return
        if self.idle_time() < self.idle_seconds:
            return
        if self._inhibited():
            return
        if self.blank():
            print(
                f"blanket-watcher: idle for {self.idle_seconds}s; blanked",
                flush=True,
            )
        else:
            # Do not hammer the bus while we cannot arm; retry in a moment.
            self._last_activity = time.monotonic() - self.idle_seconds + 5.0

    # ---- control socket ----

    def _accept_clients(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break
            if not self._peer_is_me(conn):
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            conn.setblocking(False)
            self.clients[conn.fileno()] = (conn, bytearray())
            self.client_born[conn.fileno()] = time.monotonic()
            self.poller.register(conn.fileno(), select.POLLIN)

    @staticmethod
    def _peer_is_me(conn: socket.socket) -> bool:
        """Only the owning user may drive the watcher (SO_PEERCRED)."""
        try:
            creds = conn.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _pid, uid, _gid = struct.unpack("3i", creds)
        except (OSError, AttributeError, struct.error):
            return True  # platform without SO_PEERCRED: the 0600 socket guards us
        return uid == os.getuid()

    def _reap_stale_clients(self) -> None:
        now = time.monotonic()
        for fd, born in list(self.client_born.items()):
            if fd in self.clients and now - born > CLIENT_TIMEOUT:
                self._close_client(fd)

    def _close_client(self, fd: int) -> None:
        self.client_born.pop(fd, None)
        entry = self.clients.pop(fd, None)
        if entry is None:
            return
        conn, _ = entry
        try:
            self.poller.unregister(fd)
        except (KeyError, OSError):
            pass
        try:
            conn.close()
        except OSError:
            pass

    def _service_client(self, fd: int) -> None:
        entry = self.clients.get(fd)
        if entry is None:
            return
        conn, buf = entry
        try:
            data = conn.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._close_client(fd)
            return

        if not data:
            self._close_client(fd)
            return

        buf.extend(data)
        if b"\n" not in buf:
            if len(buf) > 8192:
                self._close_client(fd)
            return

        line, _, _ = bytes(buf).partition(b"\n")
        try:
            payload = self._serialize(
                self._handle_command(line.decode("utf-8", "replace").strip())
            )
        except Exception as exc:  # keep the watcher alive on bad input
            payload = f"ok=false\nerror={exc}\n"
        try:
            conn.sendall(payload.encode())
        except OSError:
            pass
        self._close_client(fd)

    @staticmethod
    def _serialize(resp: dict) -> str:
        resp = dict(resp)
        ok = resp.pop("ok", True)
        out = [f"ok={'true' if ok else 'false'}"]
        for key, value in resp.items():
            if isinstance(value, (list, tuple)):
                for item in value:
                    out.append(f"{key}={item}")
            else:
                if isinstance(value, bool):
                    value = "true" if value else "false"
                elif value is None:
                    value = ""
                out.append(f"{key}={value}")
        return "\n".join(out) + "\n"

    def _handle_command(self, line: str) -> dict:
        parts = line.split()
        cmd = parts[0].lower() if parts else ""
        if cmd in ("ping", ""):
            return {"ok": True}
        if cmd == "status":
            return {"ok": True, **self.status()}
        if cmd == "list":
            devices = [
                f"{path}|{dev.name}"
                for path, dev in sorted(self.devices.items())
            ]
            return {"ok": True, "count": len(devices), "device": devices}
        if cmd == "arm":
            return {"ok": True, "armed": self.arm()}
        if cmd == "disarm":
            self.disarm()
            return {"ok": True, "armed": self.armed}
        if cmd == "blank":
            return {"ok": True, "armed": self.blank()}
        if cmd == "unblank":
            self.unblank()
            return {"ok": True, "armed": self.armed}
        if cmd == "idle":
            return self._cmd_idle(parts[1:])
        if cmd == "reload":
            cfg = load_config()
            self.idle_seconds = max(0, int(cfg.get("idle", 0)))
            self.respect_inhibitors = bool(cfg.get("respect_inhibitors", True))
            return {"ok": True, "idle": self.idle_seconds}
        return {"ok": False, "error": f"unknown command: {cmd or '(empty)'}"}

    def _cmd_idle(self, args: list[str]) -> dict:
        if not args or args[0].lower() in ("status", "show"):
            return {
                "ok": True,
                "idle": self.idle_seconds,
                "idle_for": round(self.idle_time(), 1),
            }
        value = args[0].lower()
        if value in ("off", "none", "disable"):
            self.idle_seconds = 0
        else:
            try:
                self.idle_seconds = max(0, int(value))
            except ValueError:
                return {
                    "ok": False,
                    "error": "idle expects a number of seconds or 'off'",
                }
        # Reset the timer so a freshly configured idle delay starts now.
        self._last_activity = time.monotonic()
        return {"ok": True, "idle": self.idle_seconds}

    def status(self) -> dict:
        return {
            "pid": os.getpid(),
            "armed": self.armed,
            "grabbed": len(self.grabbed),
            "devices": len(self.devices),
            "power": self._state(force=True),
            "idle": self.idle_seconds,
            "idle_for": (
                round(self.idle_time(), 1) if self.idle_seconds else None
            ),
            "respect_inhibitors": self.respect_inhibitors,
            "inhibited": self._inhibited(),
            "last_wake": self._last_wake,
            "uptime": round(time.time() - self._started, 1),
        }

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
        if self.idle_seconds:
            print(
                f"blanket-watcher: idle blanking after {self.idle_seconds}s",
                flush=True,
            )

        server_fd = self.sock.fileno() if self.sock else -1

        while self._running:
            try:
                events = self.poller.poll(POLL_TIMEOUT_MS)
            except InterruptedError:
                events = []

            for fd, mask in events:
                if fd == server_fd:
                    self._accept_clients()
                    continue
                if fd in self.clients:
                    if mask & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
                        self._close_client(fd)
                    elif mask & select.POLLIN:
                        self._service_client(fd)
                    continue

                path = self.fd_to_path.get(fd)
                if path is None:
                    continue
                if mask & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
                    self._remove_device(path)
                    continue
                if mask & select.POLLIN:
                    self._handle_input(path)

            self._drain_pending()
            self._tick_release()
            self._reap_stale_clients()

            now = time.monotonic()
            if now - self._last_refresh >= REFRESH_INTERVAL:
                self._refresh_devices()
                self._last_refresh = now

            if self.idle_seconds > 0 and now - self._last_idle_check >= IDLE_TICK:
                self._last_idle_check = now
                self._maybe_idle_blank()

        self.shutdown()

    def shutdown(self) -> None:
        was_armed = self.armed
        if was_armed:
            # Don't leave the user with a blank panel and no watcher.
            # Power first, then release the grabs (same anti-flicker order
            # as the wake path).
            set_display_power(POWER_ON)
        self.disarm()

        for fd in list(self.clients):
            self._close_client(fd)
        if self.sock is not None:
            try:
                self.poller.unregister(self.sock.fileno())
            except (KeyError, OSError):
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        try:
            SOCKET_PATH.unlink()
        except OSError:
            pass

        for path in list(self.devices):
            self._remove_device(path)

    def stop(self, *_args) -> None:
        self._running = False


# --- entry point -----------------------------------------------------------

def _pid_is_watcher(pid: int) -> bool:
    """True if `pid` names a live blanket-watcher process.

    `os.kill(pid, 0)` only proves *some* process holds the PID; PIDs are
    recycled constantly, so a stale pidfile could otherwise block the
    service from starting after any reboot.  Read the process's command
    line from /proc instead — that contains the script name in every way
    the watcher is realistically launched.
    """
    if pid <= 0:
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return b"blanket-watcher" in fh.read()
    except OSError:
        return False


def _acquire_pidfile() -> None:
    if PIDFILE.exists():
        try:
            pid = int(PIDFILE.read_text().strip())
        except (ValueError, OSError):
            pid = 0
        if pid and _pid_is_watcher(pid):
            print("blanket-watcher: already running", file=sys.stderr)
            sys.exit(0)
        # Stale, unreadable, or pointing at an unrelated (recycled) PID.
        try:
            PIDFILE.unlink()
        except OSError:
            pass

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(str(os.getpid()))
    atexit.register(lambda: PIDFILE.unlink(missing_ok=True))


def main() -> int:
    _acquire_pidfile()

    # Crash recovery: if we are (re)starting while the panel is already blank,
    # bring it back on so the user is never stranded on a black screen with no
    # watcher to wake it.
    if display_power_state() == POWER_OFF:
        print(
            "blanket-watcher: screen was blank at startup; restoring",
            file=sys.stderr, flush=True,
        )
        set_display_power(POWER_ON)

    watcher = Watcher()
    signal.signal(signal.SIGUSR1, watcher._on_arm)
    signal.signal(signal.SIGUSR2, watcher._on_disarm)
    signal.signal(signal.SIGTERM, watcher.stop)
    signal.signal(signal.SIGINT, watcher.stop)

    try:
        watcher.start()
        watcher.run()
    except Exception as exc:
        watcher.shutdown()
        print(f"blanket-watcher: fatal: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())