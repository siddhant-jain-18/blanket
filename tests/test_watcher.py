"""Unit tests for blanket-watcher that do not need a GNOME session.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "blanket_watcher", ROOT / "blanket-watcher.py"
)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
sys.modules["blanket_watcher"] = mod
SPEC.loader.exec_module(mod)


def make_watcher(**config) -> "mod.Watcher":
    cfg = {"idle": 0, "respect_inhibitors": True}
    cfg.update(config)
    return mod.Watcher(cfg)


class ConfigTests(unittest.TestCase):
    def test_defaults_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(mod, "CONFIG_PATH", Path(tmp) / "nope"):
                cfg = mod.load_config()
        self.assertEqual(cfg, {"idle": 0, "respect_inhibitors": True})

    def test_parses_values_comments_and_junk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config"
            path.write_text(
                "# a comment\n"
                "idle = 300\n"
                "respect_inhibitors = no\n"
                "bogus line without equals\n"
                "idle=42\n"  # last value wins
                "idle=not-a-number\n"  # ignored, keeps previous
            )
            with mock.patch.object(mod, "CONFIG_PATH", path):
                cfg = mod.load_config()
        self.assertEqual(cfg["idle"], 42)
        self.assertFalse(cfg["respect_inhibitors"])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.watcher = make_watcher()
        patcher = mock.patch.object(
            mod, "display_power_state", return_value=mod.POWER_OFF
        )
        self.state = patcher.start()
        self.addCleanup(patcher.stop)
        power = mock.patch.object(mod, "set_display_power", return_value=True)
        self.power = power.start()
        self.addCleanup(power.stop)

    def test_ping_and_unknown(self):
        self.assertEqual(self.watcher._handle_command("ping"), {"ok": True})
        resp = self.watcher._handle_command("frobnicate")
        self.assertFalse(resp["ok"])
        self.assertIn("unknown command", resp["error"])

    def test_arm_disarm_are_idempotent(self):
        first = self.watcher._handle_command("arm")
        self.assertTrue(first["armed"])
        second = self.watcher._handle_command("arm")
        self.assertTrue(second["armed"])
        self.assertEqual(
            self.watcher._handle_command("disarm"), {"ok": True, "armed": False}
        )
        self.assertEqual(
            self.watcher._handle_command("disarm"), {"ok": True, "armed": False}
        )

    def test_idle_set_and_disable(self):
        self.assertEqual(self.watcher._handle_command("idle 300")["idle"], 300)
        self.assertEqual(self.watcher.idle_seconds, 300)
        self.assertEqual(self.watcher._handle_command("idle off")["idle"], 0)
        self.assertEqual(self.watcher.idle_seconds, 0)
        bad = self.watcher._handle_command("idle soon")
        self.assertFalse(bad["ok"])

    def test_idle_status_reports_current_value(self):
        self.watcher._handle_command("idle 120")
        status = self.watcher._handle_command("idle status")
        self.assertEqual(status["idle"], 120)
        self.assertIn("idle_for", status)

    def test_blank_rolls_back_when_set_fails(self):
        # The power-set request itself failed, so the panel never went off:
        # blank must release the grabs and report armed=false rather than
        # stranding the user on a black screen.
        self.power.return_value = False
        result = self.watcher._handle_command("blank")
        self.assertFalse(result["armed"])
        self.assertFalse(self.watcher.armed)
        modes = [call.args[0] for call in self.power.call_args_list]
        self.assertEqual(modes, [mod.POWER_OFF])

    def test_blank_waits_for_async_apply(self):
        # Mutter applies PowerSaveMode asynchronously: the first read-back
        # may still show the old value. blank() must wait briefly instead
        # of treating one stale read as failure (flash off/back on).
        self.state.side_effect = [mod.POWER_ON, mod.POWER_OFF]
        with mock.patch.object(mod.time, "sleep"):
            result = self.watcher._handle_command("blank")
        self.assertTrue(result["armed"])
        self.assertTrue(self.watcher.armed)

    def test_blank_rolls_back_when_panel_never_goes_off(self):
        # The set was accepted but the panel never reports off: roll back
        # (and restore power) rather than stranding the user.
        self.state.return_value = mod.POWER_ON
        with mock.patch.object(mod.time, "sleep"):
            result = self.watcher._handle_command("blank")
        self.assertFalse(result["armed"])
        self.assertFalse(self.watcher.armed)
        modes = [call.args[0] for call in self.power.call_args_list]
        self.assertEqual(modes[0], mod.POWER_OFF)
        self.assertEqual(modes[-1], mod.POWER_ON)

    def test_blank_arms_when_display_is_off(self):
        result = self.watcher._handle_command("blank")
        self.assertTrue(result["armed"])
        self.assertTrue(self.watcher.armed)

    def test_blank_refuses_when_no_device_grabbed(self):
        watcher = make_watcher()
        fake = types.SimpleNamespace(
            path="/dev/input/event0", name="Busy kbd",
            grab=mock.MagicMock(side_effect=OSError(16, "busy")),
            ungrab=mock.MagicMock(), close=mock.MagicMock(),
            read_one=mock.MagicMock(return_value=None),
        )
        watcher.devices["/dev/input/event0"] = fake
        with mock.patch.object(
            mod, "display_power_state", return_value=mod.POWER_OFF
        ), mock.patch.object(
            mod, "set_display_power", return_value=True
        ) as power:
            result = watcher._handle_command("blank")
        self.assertFalse(result["armed"])
        self.assertFalse(watcher.armed)
        power.assert_not_called()  # never blanked: nothing could wake us

    def test_blank_is_idempotent_when_already_armed(self):
        self.watcher._handle_command("blank")
        self.assertTrue(self.watcher.armed)
        self.power.reset_mock()
        with mock.patch.object(
            self.watcher, "_grab_all", wraps=self.watcher._grab_all
        ) as grab_all:
            result = self.watcher._handle_command("blank")
        self.assertTrue(result["armed"])
        grab_all.assert_not_called()  # no re-grab spam (EBUSY)
        self.power.assert_not_called()  # no redundant bus round-trip

    def test_blank_grabs_before_power_off(self):
        watcher = make_watcher()
        order = []
        watcher._grab_all = mock.MagicMock(
            side_effect=lambda: order.append("grab") or 0
        )
        watcher._drain_all = mock.MagicMock(
            side_effect=lambda: order.append("drain")
        )
        with mock.patch.object(
            mod, "set_display_power",
            side_effect=lambda mode: order.append(f"power{mode}") or True,
        ):
            self.assertTrue(watcher.blank())
        self.assertLess(order.index("grab"), order.index("power3"), order)

    def test_list_reports_devices(self):
        self.watcher.devices["/dev/input/event0"] = types.SimpleNamespace(
            name="Test Keyboard"
        )
        resp = self.watcher._handle_command("list")
        self.assertEqual(resp["count"], 1)
        self.assertEqual(resp["device"], ["/dev/input/event0|Test Keyboard"])
        text = mod.Watcher._serialize(resp)
        self.assertIn("ok=true", text)
        self.assertIn("device=/dev/input/event0|Test Keyboard", text)

    def test_serialize_booleans_and_none(self):
        text = mod.Watcher._serialize(
            {"ok": True, "armed": False, "last_wake": None}
        )
        self.assertEqual(text, "ok=true\narmed=false\nlast_wake=\n")


class IdleLogicTests(unittest.TestCase):
    def setUp(self):
        self.watcher = make_watcher(idle=1)
        patcher = mock.patch.object(
            mod, "display_power_state", return_value=mod.POWER_OFF
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        power = mock.patch.object(mod, "set_display_power", return_value=True)
        power.start()
        self.addCleanup(power.stop)

    def _inhibitor(self, value):
        patcher = mock.patch.object(
            self.watcher, "_inhibited", return_value=value
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_idle_blank_waits_for_the_full_timeout(self):
        with redirect_stdout(io.StringIO()):
            self.watcher._maybe_idle_blank()
        self.assertFalse(self.watcher.armed)

    def test_idle_blank_fires_once_timeout_elapsed(self):
        self.watcher._last_activity -= 5
        self._inhibitor(False)
        with redirect_stdout(io.StringIO()):
            self.watcher._maybe_idle_blank()
        self.assertTrue(self.watcher.armed)

    def test_idle_blank_respects_inhibitors(self):
        self.watcher._last_activity -= 5
        self._inhibitor(True)
        with redirect_stdout(io.StringIO()):
            self.watcher._maybe_idle_blank()
        self.assertFalse(self.watcher.armed)


class HotplugTests(unittest.TestCase):
    def test_device_added_while_armed_is_grabbed(self):
        watcher = make_watcher()
        watcher.armed = True
        r, w = os.pipe()
        self.addCleanup(os.close, w)
        fake = types.SimpleNamespace(fd=r, path="/dev/input/eventX")
        fake.grab = mock.MagicMock()
        fake.close = mock.MagicMock()
        with mock.patch.object(mod.evdev, "InputDevice", return_value=fake), \
             mock.patch.object(mod, "_is_wake_device", return_value=True):
            watcher._add_device("/dev/input/eventX")
        self.assertIn("/dev/input/eventX", watcher.devices)
        fake.grab.assert_called_once()

    def test_device_removed_while_armed_is_ungrabbed(self):
        watcher = make_watcher()
        watcher.armed = True
        r, w = os.pipe()
        self.addCleanup(os.close, w)
        fake = types.SimpleNamespace(
            fd=r, path="/dev/input/eventX",
            grab=mock.MagicMock(), ungrab=mock.MagicMock(),
            close=mock.MagicMock(),
        )
        watcher.devices["/dev/input/eventX"] = fake
        watcher.fd_to_path[r] = "/dev/input/eventX"
        watcher.poller.register(r, mod.select.POLLIN)
        watcher._remove_device("/dev/input/eventX")
        self.assertNotIn("/dev/input/eventX", watcher.devices)
        fake.close.assert_called_once()


class WakeFilterTests(unittest.TestCase):
    """Releases must never wake; genuine presses/motion must always wake."""

    def setUp(self):
        self.watcher = make_watcher()
        self.watcher.armed = True
        power = mock.patch.object(mod, "set_display_power")
        self.power = power.start()
        self.addCleanup(power.stop)
        state = mock.patch.object(
            mod, "display_power_state", return_value=mod.POWER_ON
        )
        state.start()
        self.addCleanup(state.stop)
        poke = mock.patch.object(mod, "poke_session_activity")
        self.poke = poke.start()
        self.addCleanup(poke.stop)

    def _feed(self, *events):
        dev = mock.MagicMock()
        dev.read.return_value = list(events)
        self.watcher.devices["/dev/input/event0"] = dev
        with redirect_stdout(io.StringIO()):
            self.watcher._handle_input("/dev/input/event0")

    def _ev(self, type_, value=0, code=0):
        return types.SimpleNamespace(type=type_, code=code, value=value)

    def test_key_release_never_wakes(self):
        from evdev import ecodes
        self._feed(self._ev(ecodes.EV_KEY, 0, ecodes.KEY_B))
        self.assertTrue(self.watcher.armed)
        self.power.assert_not_called()

    def test_shortcut_release_sequence_never_wakes(self):
        # Ctrl+Alt+B key-ups arriving after the blank, as separate frames.
        from evdev import ecodes
        for code in (ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTALT, ecodes.KEY_B):
            self._feed(self._ev(ecodes.EV_KEY, 0, code))
        self.assertTrue(self.watcher.armed)
        self.power.assert_not_called()

    def test_key_press_wakes_immediately(self):
        from evdev import ecodes
        self._feed(self._ev(ecodes.EV_KEY, 1, ecodes.KEY_A))
        self.assertFalse(self.watcher.armed)
        self.power.assert_called_once_with(mod.POWER_ON)

    def test_key_autorepeat_wakes(self):
        from evdev import ecodes
        self._feed(self._ev(ecodes.EV_KEY, 2, ecodes.KEY_A))
        self.assertFalse(self.watcher.armed)

    def test_msc_scan_alone_never_wakes(self):
        from evdev import ecodes
        self._feed(self._ev(ecodes.EV_MSC, 0, ecodes.MSC_SCAN))
        self.assertTrue(self.watcher.armed)
        self.power.assert_not_called()

    def test_real_key_frame_with_msc_scan_wakes(self):
        # A physical keypress arrives as MSC_SCAN + KEY press + SYN.
        from evdev import ecodes
        self._feed(
            self._ev(ecodes.EV_MSC, 458756, ecodes.MSC_SCAN),
            self._ev(ecodes.EV_KEY, 1, ecodes.KEY_ENTER),
            self._ev(ecodes.EV_SYN, 0, 0),
        )
        self.assertFalse(self.watcher.armed)

    def test_pointer_motion_wakes(self):
        from evdev import ecodes
        for type_ in (ecodes.EV_REL, ecodes.EV_ABS, ecodes.EV_SW):
            with self.subTest(type=type_):
                w = make_watcher()
                w.armed = True
                dev = mock.MagicMock()
                dev.read.return_value = [self._ev(type_, 1, 0)]
                w.devices["d"] = dev
                with mock.patch.object(mod, "set_display_power"), \
                        redirect_stdout(io.StringIO()):
                    w._handle_input("d")
                self.assertFalse(w.armed)

    def test_syn_only_frame_ignored(self):
        from evdev import ecodes
        before = self.watcher._last_activity
        self._feed(self._ev(ecodes.EV_SYN, 0, 0))
        self.assertTrue(self.watcher.armed)
        self.assertEqual(self.watcher._last_activity, before)

    def test_release_still_resets_idle_clock(self):
        from evdev import ecodes
        before = self.watcher._last_activity
        self.watcher._last_activity = before - 10
        self._feed(self._ev(ecodes.EV_KEY, 0, ecodes.KEY_B))
        self.assertGreater(self.watcher._last_activity, before - 10)
        self.assertTrue(self.watcher.armed)  # …but does not wake

    def test_mixed_release_plus_press_frame_wakes(self):
        from evdev import ecodes
        self._feed(
            self._ev(ecodes.EV_KEY, 0, ecodes.KEY_B),
            self._ev(ecodes.EV_KEY, 1, ecodes.KEY_A),
        )
        self.assertFalse(self.watcher.armed)

    def test_input_while_disarmed_only_resets_idle(self):
        from evdev import ecodes
        self.watcher.armed = False
        self._feed(self._ev(ecodes.EV_KEY, 1, ecodes.KEY_A))
        self.power.assert_not_called()


class ImmediateWakeTests(unittest.TestCase):
    """No settle delay may swallow a genuine new press (regression test)."""

    def test_press_right_after_blank_wakes(self):
        watcher = make_watcher()
        with mock.patch.object(
            mod, "set_display_power", return_value=True
        ), mock.patch.object(
            mod, "display_power_state", return_value=mod.POWER_OFF
        ), redirect_stdout(io.StringIO()):
            self.assertTrue(watcher.blank())
        from evdev import ecodes
        dev = mock.MagicMock()
        dev.read.return_value = [
            types.SimpleNamespace(
                type=ecodes.EV_KEY, code=ecodes.KEY_SPACE, value=1
            )
        ]
        watcher.devices["/dev/input/event0"] = dev
        with mock.patch.object(
            mod, "set_display_power"
        ) as power, mock.patch.object(
            mod, "display_power_state", return_value=mod.POWER_ON
        ), mock.patch.object(
            mod, "poke_session_activity"
        ), redirect_stdout(io.StringIO()):
            watcher._handle_input("/dev/input/event0")
        self.assertFalse(watcher.armed)
        power.assert_called_once_with(mod.POWER_ON)


class GrabTrackingTests(unittest.TestCase):
    def test_grab_records_success_and_drains(self):
        watcher = make_watcher()
        dev = mock.MagicMock()
        dev.path = "/dev/input/event0"
        dev.read_one.return_value = None
        self.assertTrue(watcher._grab(dev))
        self.assertIn("/dev/input/event0", watcher.grabbed)
        dev.read_one.assert_called()

    def test_failed_grab_returns_false_and_untracked(self):
        watcher = make_watcher()
        dev = mock.MagicMock()
        dev.path = "/dev/input/event0"
        dev.grab.side_effect = OSError(16, "busy")
        with redirect_stdout(io.StringIO()), \
                mock.patch.object(mod.sys, "stderr", io.StringIO()):
            self.assertFalse(watcher._grab(dev))
        self.assertNotIn("/dev/input/event0", watcher.grabbed)

    def test_arm_refuses_when_nothing_grabbed(self):
        watcher = make_watcher()
        dev = mock.MagicMock()
        dev.path = "/dev/input/event0"
        dev.grab.side_effect = OSError(16, "busy")
        dev.read_one.return_value = None
        watcher.devices["/dev/input/event0"] = dev
        with mock.patch.object(
            watcher, "_state", return_value=mod.POWER_OFF
        ), redirect_stdout(io.StringIO()), \
                mock.patch.object(mod.sys, "stderr", io.StringIO()):
            self.assertFalse(watcher.arm())
        self.assertFalse(watcher.armed)

    def test_status_reports_real_grab_count(self):
        watcher = make_watcher()
        watcher.armed = True
        watcher.grabbed = {"/dev/input/event0"}
        watcher.devices["/dev/input/event0"] = mock.MagicMock()
        watcher.devices["/dev/input/event1"] = mock.MagicMock()
        with mock.patch.object(
            watcher, "_state", return_value=mod.POWER_OFF
        ), mock.patch.object(watcher, "_inhibited", return_value=False):
            st = watcher.status()
        self.assertEqual(st["grabbed"], 1)  # not len(devices) == 2


class WakePathTests(unittest.TestCase):
    """The wake path must not flicker: power first, then ungrab."""

    def test_wake_sets_power_before_releasing_grabs(self):
        watcher = make_watcher()
        watcher.armed = True
        order = []
        dev = mock.MagicMock()
        dev.path = "/dev/input/event0"
        watcher.devices["/dev/input/event0"] = dev
        watcher.grabbed.add("/dev/input/event0")
        orig_ungrab = watcher._ungrab
        watcher._ungrab = mock.MagicMock(
            side_effect=lambda d: (order.append("ungrab"), orig_ungrab(d))
        )
        with mock.patch.object(
            mod, "set_display_power",
            side_effect=lambda m: order.append(f"power{m}") or True,
        ), mock.patch.object(
            mod, "display_power_state", return_value=mod.POWER_ON
        ), mock.patch.object(
            mod, "poke_session_activity"
        ), redirect_stdout(io.StringIO()):
            watcher._wake()
        self.assertLess(order.index("power0"), order.index("ungrab"), order)
        self.assertFalse(watcher.armed)

    def test_wake_issues_single_power_set(self):
        # Every extra modeset is another visible flash: exactly one set.
        watcher = make_watcher()
        watcher.armed = True
        with mock.patch.object(
            mod, "set_display_power", return_value=True
        ) as power, mock.patch.object(
            mod, "display_power_state", return_value=mod.POWER_ON
        ), mock.patch.object(
            mod, "poke_session_activity"
        ), redirect_stdout(io.StringIO()):
            watcher._wake()
        power.assert_called_once_with(mod.POWER_ON)

    def test_wake_pokes_session_activity(self):
        watcher = make_watcher()
        watcher.armed = True
        with mock.patch.object(
            mod, "set_display_power", return_value=True
        ), mock.patch.object(
            mod, "display_power_state", return_value=mod.POWER_ON
        ), mock.patch.object(
            mod, "poke_session_activity"
        ) as poke, redirect_stdout(io.StringIO()):
            watcher._wake()
        poke.assert_called_once_with()

    def test_wake_releases_grabs_even_if_panel_misbehaves(self):
        # A grabbed keyboard with a lit screen strands the user; the
        # panel being stuck must never block the ungrab.
        watcher = make_watcher()
        watcher.armed = True
        dev = mock.MagicMock()
        dev.path = "/dev/input/event0"
        watcher.devices["/dev/input/event0"] = dev
        watcher.grabbed.add("/dev/input/event0")
        with mock.patch.object(
            mod, "set_display_power", return_value=True
        ), mock.patch.object(
            mod, "display_power_state", return_value=mod.POWER_OFF
        ), mock.patch.object(
            mod, "poke_session_activity"
        ), mock.patch.object(
            mod.time, "sleep"
        ), redirect_stdout(io.StringIO()):
            watcher._wake()
        self.assertFalse(watcher.armed)
        self.assertEqual(watcher.grabbed, set())
        dev.ungrab.assert_called_once()

    def test_rel_zero_delta_does_not_wake(self):
        from evdev import ecodes
        frame = [types.SimpleNamespace(type=ecodes.EV_REL, code=0, value=0)]
        self.assertFalse(mod.Watcher._is_wake_event(frame))
        frame = [types.SimpleNamespace(type=ecodes.EV_REL, code=0, value=3)]
        self.assertTrue(mod.Watcher._is_wake_event(frame))


class AwaitStateTests(unittest.TestCase):
    def test_returns_true_immediately_when_already_there(self):
        with mock.patch.object(
            mod, "display_power_state", return_value=mod.POWER_OFF
        ), mock.patch.object(mod.time, "sleep") as sleep:
            self.assertTrue(mod.await_power_state(mod.POWER_OFF))
        sleep.assert_not_called()

    def test_waits_through_transient_stale_reads(self):
        with mock.patch.object(
            mod, "display_power_state",
            side_effect=[mod.POWER_ON, mod.POWER_ON, mod.POWER_OFF],
        ), mock.patch.object(mod.time, "sleep"):
            self.assertTrue(mod.await_power_state(mod.POWER_OFF))

    def test_gives_up_after_attempts(self):
        with mock.patch.object(
            mod, "display_power_state", return_value=mod.POWER_ON
        ), mock.patch.object(mod.time, "sleep") as sleep:
            self.assertFalse(
                mod.await_power_state(mod.POWER_OFF, attempts=3)
            )
        self.assertEqual(sleep.call_count, 3)


class CrashRecoveryTests(unittest.TestCase):
    def _run_main(self, state):
        """Run main() with the display and control loop stubbed out."""
        with mock.patch.object(mod, "display_power_state", return_value=state), \
             mock.patch.object(mod, "set_display_power") as power, \
             mock.patch.object(mod, "load_config", return_value={"idle": 0}), \
             mock.patch.object(mod, "_acquire_pidfile"), \
             mock.patch.object(mod.Watcher, "start"), \
             mock.patch.object(mod.Watcher, "run"), \
             mock.patch.object(mod.signal, "signal"):
            rc = mod.main()
        return rc, power

    def test_startup_unblanks_when_screen_was_left_off(self):
        rc, power = self._run_main(mod.POWER_OFF)
        self.assertEqual(rc, 0)
        power.assert_called_once_with(mod.POWER_ON)

    def test_startup_leaves_a_visible_screen_alone(self):
        rc, power = self._run_main(mod.POWER_ON)
        self.assertEqual(rc, 0)
        power.assert_not_called()


if __name__ == "__main__":
    unittest.main()
