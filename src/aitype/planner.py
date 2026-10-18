"""Turns text into a typing plan: a list of timed keystrokes.

Plan events are dicts:
    {"d": 123, "t": "a"}            wait 123 ms, then type the character "a"
    {"d": 80,  "k": "backspace"}    wait 80 ms, then press backspace

Everything stochastic goes through one seeded RNG, so (text, params, seed) -> same plan.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
from dataclasses import dataclass

from . import layouts

PLAN_VERSION = 1

MIN_DELAY = 35.0        # ms, nobody types faster than this between keys
RHYTHM_PHI = 0.97       # per-keystroke persistence of the "speed mood" (~6 words)
RHYTHM_SIGMA = 0.17     # spread of the speed mood (log scale)
NOISE_SIGMA = 0.22      # independent per-key jitter (log scale)


@dataclass
class PlanParams:
    bpm: int = 260
    mistakes_per_100: int = 3
    corrected_pct: int = 85
    seed: int | None = None


# --- text analysis -----------------------------------------------------------

# finger index per key: 0-3 left hand (pinky..index), 4-7 right hand (index..pinky)
_FINGERS = ["qaz1", "wsx2", "edc3", "rfvtgb45", "yhnujm67", "ik8,", "ol9.", "p0;/-'äöå"]
FINGER = {ch: f for f, chars in enumerate(_FINGERS) for ch in chars}

FAST_BIGRAMS = set(
    "th he in er an re on at en nd ti es or te of ed is it al ar st to nt ng se ha as ou io le ve "
    "co me de hi ri ro ic ne ea ra ce li ch ll be ma si om ur "
    "ta ka ja ai oi ut ni na la ki ss tt".split()
)

COMMON_WORDS = set(
    "the and of to a in is it that for on with as was he she are be this by at or an not but have "
    "from you i we they can will its his her had were been "
    "ja on ei että se oli kun mutta tai ovat myös ole niin jos vain sekä joka tämä kuin".split()
)

_WORD_RE = re.compile(r"\w+(?:['’\-]\w+)*")


def _analyse(text: str):
    """Per-character speed factor + info about where words start."""
    n = len(text)
    speed = [1.0] * n
    wstart: dict[int, tuple[int, bool, bool, bool, bool]] = {}
    matches = list(_WORD_RE.finditer(text))
    lens = [len(m.group()) for m in matches]
    for wi, m in enumerate(matches):
        word = m.group()
        lo, hi = max(0, wi - 6), min(len(lens), wi + 7)
        local = sum(lens[lo:hi]) / (hi - lo)
        f = min(1.12, max(0.92, 1 + 0.05 * (local - 4.8)))  # dense/long-word passages are slower
        has_digit = any(c.isdigit() for c in word)
        if word.lower() in COMMON_WORDS:
            f *= 0.88                                        # practiced chunks fly
        elif len(word) >= 9:
            f *= 1.08
        if has_digit:
            f *= 1.15
        for t in range(m.start(), m.end()):
            speed[t] = f
        j = m.start() - 1
        while j >= 0 and text[j] in " \t\"'“‘([":
            j -= 1
        prev = text[j] if j >= 0 else "\n"
        sent_start = prev in ".!?\n"
        after_clause = prev in ",;:"
        proper = len(word) > 1 and word[0].isupper() and not sent_start and not word.isupper()
        wstart[m.start()] = (len(word), sent_start, after_clause, has_digit, proper)
    return speed, wstart


def _key_factor(prev: str, ch: str) -> float:
    """Motor-level cost of hitting `ch` after `prev` (hand alternation, same finger, shift...)."""
    f = 1.0
    lc = ch.lower()
    fi = FINGER.get(lc)
    if prev:
        lp = prev.lower()
        pf = FINGER.get(lp)
        if fi is not None and pf is not None:
            if fi == pf and lp != lc:
                f *= 1.22        # same finger, different key: slow
            elif fi // 4 != pf // 4:
                f *= 0.90        # alternating hands: fast
        if lp + lc in FAST_BIGRAMS:
            f *= 0.82
    if ch.isupper():
        f *= 1.30
    elif ch.isdigit():
        f *= 1.25
    elif not ch.isalpha() and ch not in " \n\t":
        f *= 1.35
    if fi in (0, 7):
        f *= 1.06                # pinkies are weak
    return f


# --- the typist --------------------------------------------------------------

class _Typist:
    def __init__(self, text: str, p: PlanParams, k: float, nb: dict[str, list[str]]):
        self.text, self.p, self.nb = text, p, nb
        self.rng = random.Random(p.seed)
        self.base = 60000.0 / p.bpm * k
        self.pscale = min(2.0, max(0.6, (self.base / 230.0) ** 0.5))  # slow typists pause longer
        self.speed, self.wstart = _analyse(text)
        self.x = 0.0
        self.mult = 1.0
        self.pending = 0.0       # extra pause (ms) to add before the next keystroke
        self.prev = ""
        self.sent_start = 0
        self.events: list[dict] = []
        self.raw: list[float] = []   # intrinsic keystroke intervals, without pauses (for calibration)
        self.n_err = 0
        self.n_fixed = 0
        eligible = sum(c.isalnum() for c in text)
        per_char = p.mistakes_per_100 / 100.0
        self.p_err = min(0.5, per_char * len(text) / eligible) if eligible else 0.0
        self.p_fix = p.corrected_pct / 100.0

    # helpers
    def _ln(self, median: float, sigma: float = 0.4) -> float:
        return median * self.rng.lognormvariate(0.0, sigma)

    def _rhythm(self) -> float:
        self.x = RHYTHM_PHI * self.x + math.sqrt(1 - RHYTHM_PHI ** 2) * RHYTHM_SIGMA * self.rng.gauss(0, 1)
        self.mult = math.exp(self.x)
        return self.mult

    # primitives
    def _type(self, ch: str, speed: float = 1.0, careful: float = 1.0) -> None:
        raw = self.base * self._rhythm() * _key_factor(self.prev, ch) * speed * careful
        raw *= self.rng.lognormvariate(-NOISE_SIGMA ** 2 / 2, NOISE_SIGMA)
        raw = max(raw, MIN_DELAY)
        self.raw.append(raw)
        self.events.append({"d": round(raw + self.pending), "t": ch})
        self.pending = 0.0
        self.prev = ch

    def _backspace(self) -> None:
        d = max(40.0, self.rng.gauss(80, 18)) * self.pscale
        self.events.append({"d": round(d + self.pending), "k": "backspace"})
        self.pending = 0.0
        self.prev = ""

    # text-driven pauses
    def _pre(self, idx: int) -> None:
        info = self.wstart.get(idx)
        if not info:
            return
        length, sent_start, after_clause, has_digit, proper = info
        r = self.rng
        p_think = 0.015 + (0.04 if after_clause else 0.0) + (0.05 if sent_start else 0.0)
        if r.random() < p_think:                                   # "what do I say next?"
            self.pending += min(self._ln(1100, 0.5) * self.pscale, 6000.0)
        if length > 6:                                             # long word: plan the spelling
            self.pending += (length - 6) * r.uniform(8, 28)
        if has_digit:
            self.pending += self._ln(200)
        if proper:
            self.pending += self._ln(90, 0.5)
        self.pending += r.uniform(0, 50)                           # micro-hesitation

    def _post(self, idx: int) -> None:
        t = self.text
        ch = t[idx]
        nxt = t[idx + 1] if idx + 1 < len(t) else ""
        if ch in ",;:":
            self.pending += self._ln(220) * self.pscale
        elif ch in ".!?" and (not nxt or nxt.isspace() or nxt in "\"')”’»"):
            span = idx - self.sent_start                           # longer sentence -> longer breather
            self.pending += (self._ln(550) + min(span * 3.0, 500.0)) * self.pscale
            self.sent_start = idx + 1
        elif ch == "\n":
            prev = t[idx - 1] if idx else ""
            if prev == "\n":
                self.pending += self._ln(900) * self.pscale
            elif prev and prev in ".!?":
                self.pending += self._ln(1200) * self.pscale
            else:
                self.pending += self._ln(500) * self.pscale
            self.sent_start = idx + 1

    def _char(self, idx: int) -> None:
        self._pre(idx)
        self._type(self.text[idx], self.speed[idx])
        self._post(idx)

    # mistakes
    def _p_now(self) -> float:
        # speed-accuracy tradeoff: fast stretches produce more errors
        return min(0.5, self.p_err * self.mult ** -1.2)

    def _make_error(self, i: int):
        t = self.text
        ch = t[i]
        nxt = t[i + 1] if i + 1 < len(t) else ""
        cands = self.nb.get(ch.lower(), [])
        kinds = ["sub", "swap", "omit", "dup", "ins"]
        w = [0.45, 0.20, 0.12, 0.13, 0.10]
        if not cands:
            w[0] = w[4] = 0.0
        if not (ch.isalpha() and nxt.isalpha() and nxt.lower() != ch.lower()):
            w[1] = 0.0
        kind = self.rng.choices(kinds, w)[0]

        def near() -> str:
            c = self.rng.choice(cands)
            return c.upper() if ch.isupper() else c

        if kind == "sub":
            return [near()], 1, kind
        if kind == "swap":
            return [nxt, ch], 2, kind
        if kind == "omit":
            return [], 1, kind
        if kind == "dup":
            return [ch, ch], 1, kind
        nb = near()
        return ([nb, ch] if self.rng.random() < 0.5 else [ch, nb]), 1, kind

    def _lag(self, j: int, kind: str) -> int:
        """How many more characters get typed before the typist notices the mistake."""
        t = self.text
        end = t.find("\n", j)
        end = len(t) if end == -1 else end
        r = self.rng.random()
        if r < 0.40:
            lag = 0                                  # noticed right away
        elif r < 0.72:
            lag = self.rng.randint(1, 3)
        elif r < 0.90:                               # noticed at the end of the word
            k = j
            while k < end and t[k].isalnum():
                k += 1
            lag = k - j + 1
        else:
            lag = self.rng.randint(4, 12)            # noticed late
        if kind == "omit":
            lag = max(lag, 1)                        # nothing visibly wrong until more text appears
        return min(lag, end - j)

    def run(self) -> None:
        t, n, i = self.text, len(self.text), 0
        while i < n:
            if t[i].isalnum() and self.p_err and self.rng.random() < self._p_now():
                self._pre(i)
                wrong, consume, kind = self._make_error(i)
                self.n_err += 1
                for w in wrong:
                    self._type(w, self.speed[i])
                j = i + consume
                if self.rng.random() < self.p_fix:
                    lag = self._lag(j, kind)
                    for q in range(j, j + lag):
                        self._char(q)
                    self.pending += self._ln(330) * self.pscale          # reaction time
                    for _ in range(len(wrong) + lag):
                        self._backspace()
                    self.pending += max(40.0, self.rng.gauss(130, 40))   # regroup
                    for q in range(i, j + lag):                          # retype, a bit more careful
                        self._type(t[q], self.speed[q], 1.12)
                    self.n_fixed += 1
                    i = j + lag
                else:
                    i = j
                continue
            self._char(i)
            i += 1
        if self.events:
            self.events[0]["d"] = 0


def make_plan(text: str, params: PlanParams, layout: str = "us") -> dict:
    if params.seed is None:
        params.seed = random.SystemRandom().randrange(2 ** 32)
    nb = layouts.neighbors(layout)
    k = 1.0
    for _ in range(4):   # calibrate so the intrinsic keystroke rate matches --bpm
        ty = _Typist(text, params, k, nb)
        ty.run()
        if not ty.raw:
            break
        got = 60000.0 / (sum(ty.raw) / len(ty.raw))
        ratio = got / params.bpm
        if abs(ratio - 1.0) < 0.015:
            break
        k *= ratio
    events = ty.events
    total_ms = sum(e["d"] for e in events)
    burst = 60000.0 / (sum(ty.raw) / len(ty.raw)) if ty.raw else 0.0
    return {
        "version": PLAN_VERSION,
        "meta": {
            "seed": params.seed,
            "bpm": params.bpm,
            "mistakes_per_100": params.mistakes_per_100,
            "corrected_pct": params.corrected_pct,
            "layout": layout,
            "source_chars": len(text),
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "stats": {
                "keystrokes": len(events),
                "duration_s": round(total_ms / 1000, 1),
                "burst_bpm": round(burst),
                "effective_bpm": round(len(events) / (total_ms / 60000.0)) if total_ms else 0,
                "mistakes": ty.n_err,
                "corrected": ty.n_fixed,
                "left_in_text": ty.n_err - ty.n_fixed,
            },
        },
        "events": events,
    }


def simulate(events: list[dict]) -> str:
    """Replay a plan into a string (what ends up on screen)."""
    buf: list[str] = []
    for e in events:
        if "t" in e:
            buf.append(e["t"])
        elif e.get("k") == "backspace" and buf:
            buf.pop()
    return "".join(buf)


def dump_plan(plan: dict, path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write('{\n  "version": %d,\n  "meta": %s,\n  "events": [\n' % (plan["version"], json.dumps(plan["meta"], ensure_ascii=False)))
        f.write(",\n".join("    " + json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in plan["events"]))
        f.write("\n  ]\n}\n")


def load_plan(path) -> dict:
    with open(path, encoding="utf-8") as f:
        plan = json.load(f)
    if plan.get("version") != PLAN_VERSION or not isinstance(plan.get("events"), list):
        raise ValueError(f"{path}: not an aitype v{PLAN_VERSION} plan")
    for e in plan["events"]:
        ok = isinstance(e.get("d"), (int, float)) and e["d"] >= 0 and (
            ("t" in e and isinstance(e["t"], str) and len(e["t"]) == 1) or e.get("k") == "backspace"
        )
        if not ok:
            raise ValueError(f"{path}: bad event {e!r}")
    plan.setdefault("meta", {})
    return plan
