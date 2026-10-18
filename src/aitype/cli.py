from __future__ import annotations

import argparse
import random
import sys
import time
from collections import Counter
from pathlib import Path

from . import layouts
from .backends import BackendError, get_backend
from .planner import PlanParams, dump_plan, load_plan, make_plan

_SIMPLIFY = {
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-",
    "\u2026": "...", "\u00a0": " ", "\u2022": "-",
}


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="aitype", description="Type a text file like a human would.")
    ap.add_argument("-f", "--file", type=Path, help=".txt file to type (a new plan is generated from it)")
    ap.add_argument("--plan", type=Path, metavar="PLAN.json",
                    help="with --file: save the generated plan here; without --file: load and type this plan")
    ap.add_argument("--bpm", type=int, default=260, help="typing speed in keystrokes/min between pauses (default 260)")
    ap.add_argument("--mistakes-per-1000", type=int, default=30, metavar="N", help="approx. mistakes per 1000 characters (default 30)")
    ap.add_argument("--corrected-pct", type=int, default=85, metavar="PCT", help="%% of mistakes that get noticed and fixed (default 85)")
    ap.add_argument("--overshoot-pct", type=int, default=20, metavar="PCT", help="%% of fixes where backspace deletes 1-2 extra characters (default 20)")
    ap.add_argument("--seed", type=int, help="RNG seed (same text + settings + seed = same plan)")
    ap.add_argument("--countdown", type=int, default=5, help="seconds to wait before typing starts (default 5)")
    ap.add_argument("--dry-run", action="store_true", help="only create/print the plan, don't type")
    ap.add_argument("--layout", choices=sorted(layouts.LAYOUTS), help="keyboard layout of the target machine (default: from plan, else us)")
    return ap


def _fmt_time(s: float) -> str:
    m, s = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def _sanitize(text: str, can_type):
    out, dropped = [], Counter()
    for ch in text:
        if can_type(ch):
            out.append(ch)
            continue
        rep = _SIMPLIFY.get(ch)
        if rep and all(can_type(c) for c in rep):
            out.append(rep)
        else:
            dropped[ch] += 1
    return "".join(out), dropped


def _summary(plan: dict) -> str:
    m, s = plan["meta"], plan["meta"].get("stats", {})
    return (
        f"{s.get('keystrokes', len(plan['events']))} keystrokes, ~{_fmt_time(s.get('duration_s', 0))}, "
        f"burst {s.get('burst_bpm', '?')} / effective {s.get('effective_bpm', '?')} kpm, "
        f"mistakes {s.get('mistakes', '?')} (fixed {s.get('corrected', '?')}, overshoots {s.get('overshoots', '?')}, left in text {s.get('left_in_text', '?')}), "
        f"seed {m.get('seed', '?')}"
    )


def _play(events: list[dict], backend, countdown: int) -> None:
    rng = random.Random()
    skipped: set[str] = set()
    backend.open()  # open first: creating the virtual device takes a moment
    try:
        for s in range(countdown, 0, -1):
            print(f"\rTyping starts in {s}s - focus the target window...  ", end="", flush=True)
            time.sleep(1)
        print("\rTyping...                                      ")
        due = time.perf_counter()
        total = len(events)
        for n, ev in enumerate(events):
            due += ev["d"] / 1000.0
            wait = due - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
            elif wait < -0.5:       # fell far behind (suspend, load spike): don't burst to catch up
                due = time.perf_counter()
            nxt = events[n + 1]["d"] / 1000.0 if n + 1 < total else 0.1
            hold = min(rng.uniform(0.035, 0.095), max(0.01, nxt * 0.8))
            if "t" in ev:
                if backend.can_type(ev["t"]):
                    backend.type_char(ev["t"], hold)
                else:
                    skipped.add(ev["t"])
            else:
                backend.backspace(hold)
            if n % 100 == 0:
                print(f"\r{n * 100 // total:3d}%", end="", flush=True)
        print("\r100% - done.")
    except KeyboardInterrupt:
        print("\nAborted.")
    finally:
        backend.close()
    if skipped:
        print("Skipped characters the layout can't type:", " ".join(repr(c) for c in sorted(skipped)))


def main(argv=None) -> int:
    ap = _parser()
    args = ap.parse_args(argv)
    if not args.file and not args.plan:
        ap.error("give --file (plan from text) and/or --plan (saved plan)")
    if not 20 <= args.bpm <= 1500:
        ap.error("--bpm must be between 20 and 1500")
    if not 0 <= args.mistakes_per_1000 <= 500:
        ap.error("--mistakes-per-1000 must be between 0 and 500")
    if not 0 <= args.corrected_pct <= 100:
        ap.error("--corrected-pct must be between 0 and 100")
    if not 0 <= args.overshoot_pct <= 100:
        ap.error("--overshoot-pct must be between 0 and 100")
    if args.countdown < 0:
        ap.error("--countdown must be >= 0")

    try:
        if args.file:
            layout = args.layout or "us"
            backend = get_backend(layout)
            text = args.file.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
            text, dropped = _sanitize(text, backend.can_type)
            if dropped:
                print("Dropped untypable characters:", " ".join(f"{c!r}x{n}" for c, n in dropped.items()), file=sys.stderr)
            if not text.strip():
                print("Nothing to type.", file=sys.stderr)
                return 1
            params = PlanParams(args.bpm, args.mistakes_per_1000, args.corrected_pct, args.overshoot_pct, args.seed)
            plan = make_plan(text, params, layout)
            if args.plan:
                dump_plan(plan, args.plan)
                print(f"Plan saved to {args.plan}")
        else:
            plan = load_plan(args.plan)
            layout = args.layout or plan["meta"].get("layout", "us")
            backend = get_backend(layout)
        print("Plan:", _summary(plan))
        if args.dry_run:
            return 0
        _play(plan["events"], backend, args.countdown)
    except (BackendError, OSError, ValueError) as e:
        print(f"aitype: {e}", file=sys.stderr)
        return 1
    return 0
