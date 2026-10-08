# Changelog

## 2.1.0

### Fixed
- **Screen flicker.** Waking no longer calls `org.gnome.ScreenSaver.SetActive(false)`,
  which made GNOME Shell redraw its lock curtain on every wake.
- **Flicker / dead shortcuts from held keys.** `blank` now waits for the keys
  that triggered it to be released before grabbing the keyboard, and the wake
  gesture is swallowed until released. Previously the compositor never saw
  the key-up events (stuck modifiers, key-repeat re-firing the shortcut).
- **`on` / `toggle` not working.** `blanket on` verifies the panel is really
  on and forces it if not; `unblank` is a no-op when nothing needs undoing
  instead of issuing a needless modeset.
- **Upgrade left an old watcher running.** `install.sh` used `enable --now`,
  which does not restart a running service; it now always restarts, and
  `blanket doctor` detects a stale watcher.
- Event dispatch could fall through from a control-socket fd to the device
  table.
- Test suite: missing `import time`; tests for the removed ScreenSaver poke.

### Added
- Wake events are logged with the device and event that caused them.
- `blanket logs`, `blanket version`.
- Control socket hardening: 0600 permissions, `SO_PEERCRED` check, reaping of
  stalled clients.
- End-to-end test suite (real CLI + real watcher, fake `busctl`/evdev).
- ShellCheck in CI; README / safety documentation repaired.
