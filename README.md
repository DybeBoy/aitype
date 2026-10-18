# aitype

Types a text file into whatever window you have focused, at human speed: variable
rhythm, pauses at sentence ends, occasional typos and backspace corrections.

Uses a virtual keyboard (uinput on Linux, SendInput on Windows). No dependencies.

## Usage

    aitype --file text.txt

A 5 second countdown starts; focus the target window. Ctrl-C aborts.

Common options:

    --bpm 260               keystrokes/min between pauses
    --mistakes-per-100 3    approx. typo rate
    --corrected-pct 85      how many typos get noticed and fixed
    --seed 1234             same text + settings + seed = same output
    --dry-run               print plan stats, don't type
    --plan out.json         save the plan, replay later with --plan out.json

## Linux setup

Needs write access to /dev/uinput:

    sudo modprobe uinput
    sudo setfacl -m u:$USER:rw /dev/uinput   # or run as root

On NixOS: `hardware.uinput.enable = true;` and add yourself to the `uinput` group.

## Layouts

`us` and `fi` are built in. The layout decides which characters can be typed and
which "nearby" keys typos land on. Set it with `--layout`.

## Plans

A plan is a JSON list of timed keystrokes (`{"d": 123, "t": "a"}` = wait 123 ms,
type "a"). Generating one is fast; replaying a saved plan gives the exact same
typing again.
