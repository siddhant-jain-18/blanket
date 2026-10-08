import os, fcntl, evdev
from evdev import ecodes
FIFO = os.environ.get("FAKE_EVDEV_FIFO")
class FakeDev:
    def __init__(self, path):
        if path != "/fake/event0": raise OSError("nope")
        self.path = path; self.name = "Fake Keyboard"
        self.fd = os.open(FIFO, os.O_RDWR | os.O_NONBLOCK)
        self.grabbed = False; self._buf = b""
    def capabilities(self, absinfo=False): return {ecodes.EV_KEY: [ecodes.KEY_A, ecodes.KEY_Z, ecodes.KEY_ENTER, ecodes.KEY_SPACE]}
    def grab(self):
        with open(os.environ["FAKE_DIR"] + "/grab", "w") as f: f.write("1")
        self.grabbed = True
    def ungrab(self):
        with open(os.environ["FAKE_DIR"] + "/grab", "w") as f: f.write("0")
        self.grabbed = False
    def _lines(self):
        try: self._buf += os.read(self.fd, 4096)
        except BlockingIOError: pass
        while b"\n" in self._buf:
            line, _, self._buf = self._buf.partition(b"\n"); yield line.decode().split()
    def read(self):
        for t, c, v in self._lines():
            yield evdev.InputEvent(0, 0, int(t), int(c), int(v))
            yield evdev.InputEvent(0, 0, ecodes.EV_SYN, 0, 0)
    def active_keys(self, verbose=False):
        try:
            return [int(x) for x in open(os.environ["FAKE_DIR"] + "/held").read().split()]
        except OSError:
            return []
    def read_one(self):
        return None
    def close(self):
        try: os.close(self.fd)
        except OSError: pass
evdev.InputDevice = FakeDev
evdev.list_devices = lambda: ["/fake/event0"]
