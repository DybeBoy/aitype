"""Keystroke injection. Pure stdlib.

Linux (X11 *and* Wayland): a virtual keyboard through /dev/uinput. This sits below the
display server, so compositors treat it like a real keyboard - no Wayland protocol needed.
Windows: SendInput with unicode events (layout independent).
"""
from __future__ import annotations

import os
import struct
import sys
import time

from . import layouts


class BackendError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- Linux

EV_SYN, EV_KEY = 0x00, 0x01
SYN_REPORT = 0
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_DEV_SETUP = 0x405C5503
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
_EVENT = "llHHi"  # struct input_event (native): timeval + type + code + value


class UInputBackend:
    name = "uinput"

    def __init__(self, layout: str):
        self.map = layouts.build_charmap(layout)
        self.fd: int | None = None

    def can_type(self, ch: str) -> bool:
        return ch in self.map

    def open(self) -> None:
        import fcntl

        try:
            self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        except FileNotFoundError:
            raise BackendError("/dev/uinput not found - is the uinput kernel module loaded?") from None
        except PermissionError:
            raise BackendError(
                "no permission for /dev/uinput. On NixOS set `hardware.uinput.enable = true;` and add your "
                'user to the "uinput" group (users.users.<you>.extraGroups), then log out and in again.'
            ) from None
        codes = {c for c, _, _ in self.map.values()}
        codes |= {m for _, mods, _ in self.map.values() for m in mods}
        codes |= {layouts.KEY_BACKSPACE, layouts.KEY_SPACE}
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        for c in codes:
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, c)
        fcntl.ioctl(self.fd, UI_DEV_SETUP, struct.pack("<HHHH80sI", 0x03, 0x1234, 0x5678, 1, b"aitype virtual keyboard", 0))
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        time.sleep(1.0)  # give the compositor time to pick the new device up

    def close(self) -> None:
        if self.fd is not None:
            import fcntl

            time.sleep(0.2)
            try:
                fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            finally:
                os.close(self.fd)
                self.fd = None

    def _write(self, code: int, value: int) -> None:
        data = struct.pack(_EVENT, 0, 0, EV_KEY, code, value) + struct.pack(_EVENT, 0, 0, EV_SYN, SYN_REPORT, 0)
        os.write(self.fd, data)

    def _tap(self, code: int, mods: tuple[int, ...], hold: float) -> None:
        try:
            for m in mods:
                self._write(m, 1)
            if mods:
                time.sleep(0.004)
            self._write(code, 1)
            time.sleep(hold)
        finally:
            self._write(code, 0)
            for m in reversed(mods):
                self._write(m, 0)

    def type_char(self, ch: str, hold: float) -> None:
        code, mods, dead = self.map[ch]
        self._tap(code, mods, hold)
        if dead:  # dead key + space yields the bare character
            time.sleep(0.02)
            self._tap(layouts.KEY_SPACE, (), hold)

    def backspace(self, hold: float) -> None:
        self._tap(layouts.KEY_BACKSPACE, (), hold)


# --------------------------------------------------------------------------- Windows

def _win_structs():
    import ctypes
    from ctypes import wintypes

    ulong_ptr = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr)]

    class MOUSEINPUT(ctypes.Structure):  # only here so the union has the right size
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    return INPUT


class WindowsBackend:
    name = "sendinput"
    VK_BACK, VK_TAB, VK_RETURN = 0x08, 0x09, 0x0D
    UNICODE, KEYUP = 0x0004, 0x0002

    def __init__(self, layout: str):
        self.user32 = None

    def can_type(self, ch: str) -> bool:
        return ch in "\n\t" or ch.isprintable()

    def open(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.INPUT = _win_structs()
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(self.INPUT), ctypes.c_int)
        self.user32.SendInput.restype = wintypes.UINT

    def close(self) -> None:
        pass

    def _send(self, vk: int, scan: int, flags: int) -> None:
        inp = self.INPUT(type=1)  # INPUT_KEYBOARD
        inp.ki.wVk, inp.ki.wScan, inp.ki.dwFlags = vk, scan, flags
        self.user32.SendInput(1, self.ctypes.byref(inp), self.ctypes.sizeof(inp))

    def _vk(self, vk: int, hold: float) -> None:
        self._send(vk, 0, 0)
        time.sleep(hold)
        self._send(vk, 0, self.KEYUP)

    def type_char(self, ch: str, hold: float) -> None:
        if ch == "\n":
            return self._vk(self.VK_RETURN, hold)
        if ch == "\t":
            return self._vk(self.VK_TAB, hold)
        raw = ch.encode("utf-16-le")
        for unit in struct.unpack("<%dH" % (len(raw) // 2), raw):
            self._send(0, unit, self.UNICODE)
            time.sleep(hold)
            self._send(0, unit, self.UNICODE | self.KEYUP)

    def backspace(self, hold: float) -> None:
        self._vk(self.VK_BACK, hold)


# ------------------------------------------------------------------------------ other

class UnsupportedBackend:
    name = "unsupported"

    def can_type(self, ch: str) -> bool:
        return True

    def open(self) -> None:
        raise BackendError(f"typing is not supported on {sys.platform} (dry-run still works)")

    def close(self) -> None:
        pass


def get_backend(layout: str):
    if sys.platform == "win32":
        return WindowsBackend(layout)
    if sys.platform.startswith("linux"):
        return UInputBackend(layout)
    return UnsupportedBackend()
