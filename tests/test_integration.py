"""End-to-end tests: the real CLI talking to the real watcher process.

GNOME and /dev/input are replaced by fakes (tests/integration/):

  * fakebin/busctl         stores PowerSaveMode in a file
  * pysite/sitecustomize   swaps python-evdev's device layer for a FIFO

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent / "integration"


def _have_evdev() -> bool:
    return subprocess.run(
        [sys.executable, "-c", "import evdev"], capture_output=True
    ).returncode == 0


@unittest.skipUnless(shutil.which("bash") and _have_evdev(), "needs bash + evdev")
class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="blanket-it-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fake = self.tmp / "fake"
        self.fake.mkdir()
        (self.tmp / "run").mkdir(mode=0o700)
        (self.tmp / "cfg").mkdir()
        (self.fake / "mode").write_text("0")
        (self.fake / "held").write_text("")
        self.fifo = self.fake / "fifo"
        os.mkfifo(self.fifo)

        self.env = dict(os.environ)
        self.env.update(
            FAKE_DIR=str(self.fake),
            FAKE_EVDEV_FIFO=str(self.fifo),
            XDG_RUNTIME_DIR=str(self.tmp / "run"),
            XDG_CONFIG_HOME=str(self.tmp / "cfg"),
            PATH=f"{HERE / 'fakebin'}:{os.environ['PATH']}",
            PYTHONPATH=str(HERE / "pysite"),
        )
        self.log = open(self.tmp / "watcher.log", "w")
        self.watcher = subprocess.Popen(
            [sys.executable, str(ROOT / "blanket-watcher.py")],
            env=self.env, stdout=self.log, stderr=subprocess.STDOUT,
        )
        self.addCleanup(self._stop)
        sock = self.tmp / "run" / "blanket.sock"
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.05)
        else:
            self.fail("watcher never opened its socket")

    def _stop(self):
        self.watcher.terminate()
        try:
            self.watcher.wait(5)
        except subprocess.TimeoutExpired:
            self.watcher.kill()
        self.log.close()

    # -- helpers --

    def cli(self, *args):
        return subprocess.run(
            ["bash", str(ROOT / "blanket.sh"), *args],
            env=self.env, capture_output=True, text=True, timeout=20,
        )

    @property
    def mode(self) -> str:
        return (self.fake / "mode").read_text().strip()

    @property
    def grab(self) -> str:
        p = self.fake / "grab"
        return p.read_text().strip() if p.exists() else "0"

    def press(self, code=30, value=1):
        with open(self.fifo, "w") as f:
            f.write(f"1 {code} {value}\n")

    def wait_for(self, predicate, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    # -- tests --

    def test_off_blanks_and_grabs(self):
        self.assertEqual(self.cli("off").returncode, 0)
        self.assertEqual((self.mode, self.grab), ("3", "1"))
        self.assertEqual(self.cli("status").stdout.strip(), "off")

    def test_on_restores_and_releases(self):
        self.cli("off")
        self.assertEqual(self.cli("on").returncode, 0)
        self.assertEqual((self.mode, self.grab), ("0", "0"))

    def test_toggle_round_trip(self):
        self.cli("toggle")
        self.assertEqual(self.mode, "3")
        self.cli("toggle")
        self.assertEqual(self.mode, "0")
        self.cli("toggle")
        self.assertEqual(self.mode, "3")

    def test_keypress_wakes_and_is_swallowed(self):
        self.cli("off")
        self.press(30, 1)
        self.assertTrue(self.wait_for(lambda: self.mode == "0"))
        self.assertTrue(self.wait_for(lambda: self.grab == "0"))

    def test_key_release_alone_never_wakes(self):
        self.cli("off")
        self.press(48, 0)
        time.sleep(0.4)
        self.assertEqual(self.mode, "3")

    def test_off_waits_for_the_shortcut_to_be_released(self):
        (self.fake / "held").write_text("48")
        proc = subprocess.Popen(
            ["bash", str(ROOT / "blanket.sh"), "off"],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        time.sleep(0.5)
        self.assertEqual(self.mode, "0")      # still waiting for key-up
        self.assertEqual(self.grab, "0")      # and nothing grabbed yet
        (self.fake / "held").write_text("")
        proc.wait(10)
        self.assertEqual(self.mode, "3")

    def test_wake_gesture_is_swallowed_until_released(self):
        self.cli("off")
        (self.fake / "held").write_text("30")
        self.press(30, 1)
        self.assertTrue(self.wait_for(lambda: self.mode == "0"))
        self.assertEqual(self.grab, "1")      # key still down: keep swallowing
        (self.fake / "held").write_text("")
        self.assertTrue(self.wait_for(lambda: self.grab == "0"))

    def test_wake_makes_no_screensaver_calls(self):
        self.cli("off")
        self.press(30, 1)
        self.wait_for(lambda: self.mode == "0")
        self.cli("on")
        self.assertFalse((self.fake / "screensaver.log").exists())

    def test_on_when_already_on_is_a_noop(self):
        before = (self.fake / "busctl.log").read_text() \
            if (self.fake / "busctl.log").exists() else ""
        self.cli("on")
        log = (self.fake / "busctl.log").read_text()
        self.assertNotIn("set-property", log[len(before):])

    def test_off_refuses_without_watcher(self):
        self.watcher.terminate()
        self.watcher.wait(5)
        res = self.cli("off")
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(self.mode, "0")      # screen left on

    def test_on_works_without_watcher(self):
        self.cli("off")
        self.watcher.kill()
        self.watcher.wait(5)
        (self.tmp / "run" / "blanket.sock").unlink(missing_ok=True)
        self.assertEqual(self.cli("on").returncode, 0)
        self.assertEqual(self.mode, "0")

    def test_status_verbose_and_list(self):
        out = self.cli("status", "-v").stdout
        self.assertIn("watcher: running", out)
        self.assertIn("devices: 1", out)
        self.assertIn("Fake Keyboard", self.cli("list").stdout)

    def test_idle_setting_round_trip(self):
        self.assertEqual(self.cli("idle", "300").returncode, 0)
        self.assertIn("after 300s", self.cli("idle").stdout)
        self.cli("idle", "off")
        self.assertIn("off", self.cli("idle").stdout)

    def test_idle_blanks_automatically(self):
        self.cli("idle", "2")
        self.assertTrue(self.wait_for(lambda: self.mode == "3", timeout=6))

    def test_version_and_help(self):
        self.assertIn("blanket", self.cli("version").stdout)
        self.assertIn("Usage", self.cli("help").stdout)

    def test_unknown_command_fails(self):
        self.assertNotEqual(self.cli("frobnicate").returncode, 0)


if __name__ == "__main__":
    unittest.main()
