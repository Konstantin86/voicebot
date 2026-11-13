"""Jabra Speak2 40 buttons.

Call / hang-up arrive as raw HID telephony reports on /dev/hidraw*.
Volume keys arrive as normal input events on /dev/input/event*.
"""

import glob
import logging
import os
import select
import struct
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

HOOK_REPORT_ID = 0x02
OFF_HOOK = 0x01
SETTLE_SECONDS = 0.25

INPUT_EVENT = struct.Struct("llHHi")
EV_KEY = 1
KEYS = {114: "volume_down", 115: "volume_up", 164: "play_pause"}


class HookParser:
    """Tracks the hook-switch bit of the telephony report (report 02, byte 1, bit 0)."""

    def __init__(self) -> None:
        self.hook = False

    def feed(self, report: bytes) -> bool | None:
        """Returns the new hook state when it changed, else None."""
        if len(report) < 2 or report[0] != HOOK_REPORT_ID:
            return None
        hook = bool(report[1] & OFF_HOOK)
        if hook == self.hook:
            return None
        self.hook = hook
        return hook


def find_hidraw(name: str) -> str | None:
    for uevent in sorted(glob.glob("/sys/class/hidraw/hidraw*/device/uevent")):
        with open(uevent, encoding="utf-8", errors="replace") as f:
            if any(line.startswith("HID_NAME=") and name in line for line in f):
                return "/dev/" + uevent.split("/")[4]
    return None


def find_input_event(name: str) -> str | None:
    for name_file in sorted(glob.glob("/sys/class/input/event*/device/name")):
        with open(name_file, encoding="utf-8", errors="replace") as f:
            if name in f.read():
                return "/dev/input/" + name_file.split("/")[4]
    return None


class _DeviceReader(threading.Thread):
    def __init__(self, label: str, find: Callable[[], str | None], on_event: Callable[[str], None]):
        super().__init__(name=label, daemon=True)
        self.label = label
        self.find = find
        self.on_event = on_event
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        missing_logged = False
        while not self._stop.is_set():
            path = self.find()
            if not path:
                if not missing_logged:
                    log.warning("%s: Jabra device not found, waiting", self.label)
                    missing_logged = True
                self._stop.wait(5)
                continue
            missing_logged = False
            try:
                fd = self._open(path)
                log.info("%s: reading %s", self.label, path)
                try:
                    self.read_loop(fd)
                finally:
                    os.close(fd)
            except OSError as e:
                log.warning("%s: %s: %s", self.label, path, e)
            self._stop.wait(2)

    def _open(self, path: str) -> int:
        return os.open(path, os.O_RDONLY)

    def read_loop(self, fd: int) -> None:
        raise NotImplementedError


class HookButtons(_DeviceReader):
    """Emits "hook_on" (call button) and "hook_off" (hang-up button).

    The Jabra expects the computer to confirm a call by turning on its off-hook light,
    otherwise it drops the call by itself. Each press also makes the hook bit flicker
    for a few milliseconds, so events are emitted only after the state settles.
    """

    def __init__(self, name: str, on_event: Callable[[str], None]):
        super().__init__("hid", lambda: find_hidraw(name), on_event)
        self._fd: int | None = None
        self._fd_lock = threading.Lock()

    def set_off_hook_light(self, on: bool) -> None:
        with self._fd_lock:
            if self._fd is None:
                return
            try:
                os.write(self._fd, bytes([HOOK_REPORT_ID, OFF_HOOK if on else 0, 0]))
            except OSError as e:
                log.warning("hid: cannot set off-hook light: %s", e)

    def _open(self, path: str) -> int:
        return os.open(path, os.O_RDWR)

    def read_loop(self, fd: int) -> None:
        with self._fd_lock:
            self._fd = fd
        try:
            self._read(fd)
        finally:
            with self._fd_lock:
                self._fd = None

    def _read(self, fd: int) -> None:
        parser = HookParser()
        reported = False
        settling = False
        while not self._stop.is_set():
            ready, _, _ = select.select([fd], [], [], SETTLE_SECONDS if settling else 1.0)
            if not ready:
                if settling:
                    settling = False
                    if parser.hook != reported:
                        reported = parser.hook
                        self.on_event("hook_on" if reported else "hook_off")
                continue
            report = os.read(fd, 64)
            if not report:
                return
            log.debug("hid report %s", report.hex(" "))
            changed = parser.feed(report)
            if changed is True:
                self.set_off_hook_light(True)
            if changed is not None:
                settling = True


class MediaKeys(_DeviceReader):
    def __init__(self, name: str, on_event: Callable[[str], None]):
        super().__init__("keys", lambda: find_input_event(name), on_event)

    def read_loop(self, fd: int) -> None:
        while not self._stop.is_set():
            data = os.read(fd, INPUT_EVENT.size)
            if len(data) < INPUT_EVENT.size:
                return
            _, _, ev_type, code, value = INPUT_EVENT.unpack(data)
            if ev_type == EV_KEY and code in KEYS and (value == 1 or (value == 2 and KEYS[code] != "play_pause")):
                self.on_event(KEYS[code])
