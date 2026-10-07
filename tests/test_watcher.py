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

    def test_blank_rolls_back_when_grab_is_impossible(self):
        # Display won't read back as off, so arm() refuses -> blank must
        # restore the panel rather than stranding the user.
        self.state.return_value = mod.POWER_ON
        result = self.watcher._handle_command("blank")
        self.assertFalse(result["armed"])
        modes = [call.args[0] for call in self.power.call_args_list]
        self.assertEqual(modes[0], mod.POWER_OFF)
        self.assertEqual(modes[-1], mod.POWER_ON)

    def test_blank_arms_when_display_is_off(self):
        result = self.watcher._handle_command("blank")
        self.assertTrue(result["armed"])
        self.assertTrue(self.watcher.armed)

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
