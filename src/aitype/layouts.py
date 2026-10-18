"""Keyboard layout data.

Linux (uinput) sends *physical* key codes, so we must know which key + modifiers
produce a given character on the layout the desktop is using. Windows injects
unicode directly and doesn't need this, but both platforms use the key adjacency
table to pick realistic typo keys.

Add a layout by adding an entry to LAYOUTS.
"""
from __future__ import annotations

import math

# evdev key codes
KEY_BACKSPACE = 14
KEY_TAB = 15
KEY_ENTER = 28
KEY_LEFTSHIFT = 42
KEY_SPACE = 57
KEY_RIGHTALT = 100  # AltGr

_N = "\x00"  # "no character on this level"

# (first_keycode, unshifted, shifted, altgr) - strings run over consecutive keycodes
_US = [
    (41, "`", "~", ""),
    (2, "1234567890-=", "!@#$%^&*()_+", ""),
    (16, "qwertyuiop[]", "QWERTYUIOP{}", ""),
    (43, "\\", "|", ""),
    (30, "asdfghjkl;'", 'ASDFGHJKL:"', ""),
    (44, "zxcvbnm,./", "ZXCVBNM<>?", ""),
]

_FI = [
    (41, "§", "½", ""),
    (2, "1234567890+´", '!"#¤%&/()=?`', _N + "@£$€" + _N + "{[]}\\" + _N),
    (16, "qwertyuiopå¨", "QWERTYUIOPÅ^", _N * 2 + "€" + _N * 8 + "~"),
    (43, "'", "*", ""),
    (30, "asdfghjklöä", "ASDFGHJKLÖÄ", ""),
    (86, "<", ">", "|"),
    (44, "zxcvbnm,.-", "ZXCVBNM;:_", ""),
]

# adj: (keys, horizontal row offset) used to compute which keys are physical neighbours
LAYOUTS = {
    "us": {
        "rows": _US,
        "dead": frozenset(),
        "adj": [("1234567890", 0.0), ("qwertyuiop", 0.5), ("asdfghjkl", 0.75), ("zxcvbnm", 1.25)],
    },
    "fi": {
        "rows": _FI,
        "dead": frozenset("´`¨^~"),  # dead keys: need a following space to emit the char itself
        "adj": [("1234567890", 0.0), ("qwertyuiopå", 0.5), ("asdfghjklöä", 0.75), ("zxcvbnm", 1.25)],
    },
}


def build_charmap(name: str) -> dict[str, tuple[int, tuple[int, ...], bool]]:
    """char -> (keycode, modifier keycodes, is_dead_key)."""
    layout = LAYOUTS[name]
    dead = layout["dead"]
    cmap: dict[str, tuple[int, tuple[int, ...], bool]] = {
        " ": (KEY_SPACE, (), False),
        "\n": (KEY_ENTER, (), False),
        "\t": (KEY_TAB, (), False),
    }
    for first, base, shift, altgr in layout["rows"]:
        for idx in range(len(base)):
            for chars, mods in ((base, ()), (shift, (KEY_LEFTSHIFT,)), (altgr, (KEY_RIGHTALT,))):
                if idx < len(chars) and chars[idx] != _N:
                    ch = chars[idx]
                    cmap.setdefault(ch, (first + idx, mods, ch in dead))
    return cmap


def neighbors(name: str) -> dict[str, list[str]]:
    """Physical neighbours of each letter/digit (letters only neighbour letters, digits digits)."""
    pos: dict[str, tuple[float, int]] = {}
    for r, (chars, off) in enumerate(LAYOUTS[name]["adj"]):
        for c, ch in enumerate(chars):
            pos[ch] = (off + c, r)
    out: dict[str, list[str]] = {}
    for a, (ax, ay) in pos.items():
        out[a] = [
            b
            for b, (bx, by) in pos.items()
            if b != a and a.isdigit() == b.isdigit() and math.hypot(ax - bx, ay - by) <= 1.3
        ]
    return out
