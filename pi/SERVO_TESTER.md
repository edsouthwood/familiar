# Servo tester

`servo_tester.py` — an interactive keyboard servo range-finder that reports
paste-ready values for `body_client.py`.

You jog one servo's **raw pulse width** up and down with the arrow keys until it
physically sits where you want, mark the poses, and it prints the exact
`SERVO_PULSE_US` / `REST` lines to drop into `body_client.py`. It reuses that
file's channel names and `parse_pin_map`, so the two can't drift apart.

## Running it

```bash
# Mock — no hardware, just shows the pulse as you jog (rehearse the controls)
pi/servo_tester.py

# Real, off the Pi's GPIO via pigpio (same wiring as GpioServos)
pi/servo_tester.py --gpio jaw=18,eyes_l=27,eyes_r=22,tail=17

# Real, via the PCA9685 board
pi/servo_tester.py --pca
```

The servo names are **`jaw, ears_l, ears_r, eyes_l, eyes_r, tail`** (the ears and
eyes are a pair each — wire and jog them by side). Wire up several servos and
**TAB** cycles between them, so you can walk the whole puppet in one sitting.

> **Nothing moving?** With no `--gpio`/`--pca` flag the tester runs in **mock
> mode**: the on-screen pulse still updates as you jog, but no hardware is driven
> — it's easy to jog for ages wondering why nothing moves. It now prints a loud
> `MOCK MODE` banner on start; if you see it, re-run with `--gpio <name=pin,…>`
> or `--pca`. See [Nothing moving?](#nothing-moving) below for the full checklist.

## Keys

| Key       | Action                                                       |
| --------- | ------------------------------------------------------------ |
| ↑ / ↓     | jog the pulse up / down by the current step                  |
| → / ←     | bigger / smaller step (1 → 50 µs)                            |
| `n` / `x` | mark here as the **min** (= 0°) / **max** (= 180°) end-stop  |
| `r`       | mark here as **REST** (the neutral pose)                     |
| `m`       | mark a **named** pose (e.g. "open", "shut")                  |
| `c`       | jump to centre (1500 µs)                                     |
| space     | relax the servo (cut the pulse); any jog re-engages it       |
| TAB       | switch to the next wired servo                               |
| `p`       | print the calibration snippet so far                         |
| `q` / Esc | quit and print the full report                               |

## What it prints

Mark the two end-stops and rest for the jaw and you get, ready to paste:

```python
SERVO_PULSE_US = {
    JAW:     (620, 1180),
}
REST = {  # (only the parts you measured; keep the rest)
    JAW: 26,   # 700µs
}
# extra poses (angles for Animator._render):
    # jaw open: 1180µs → 180°
```

The two end-stops become the servo's 0°/180° span, and REST (plus any named
poses) is reported as the **angle** it lands on within that span — which is
exactly what `Animator._render` needs when you tune the hard-coded pose angles
(ears 130, eyelids 0, and so on).

## Notes

- **Safety rail.** The jog is clamped to 500–2500 µs (override with
  `--pulse-min` / `--pulse-max`) so you don't drive a servo into its mechanical
  stops, where it buzzes, strains, or strips a linkage. On the PCA9685 those
  same limits set the board's pulse range, so the pulse shown on screen is
  always exactly what the servo receives.
- **Needs a real terminal.** It puts the TTY into raw mode to read arrow keys,
  so it can't run over a pipe or a non-interactive shell.
- **Try mock mode first** (no arguments) to learn the controls before wiring up
  any hardware.

## Nothing moving?

If you jog and no servo moves, work down this list — it's almost always one of
these, and only the first is code:

1. **You're in mock mode.** No `--gpio`/`--pca` flag means nothing is driven.
   The panel still updates, so it looks live. Re-run with a backend flag; the
   `MOCK MODE` banner on start tells you when you've forgotten it.
2. **Wrong servo names.** Valid names are `jaw, ears_l, ears_r, eyes_l, eyes_r,
   tail`. Anything else (e.g. `ears`, `eyelids`) is rejected with
   `unknown servo …` — the eyes and ears are pairs, so name each side.
3. **`pigpiod` isn't running** (`--gpio` only). Start it with
   `sudo systemctl enable --now pigpiod`. Without it the tool exits immediately
   with a daemon error rather than jogging silently.
4. **No servo power.** A servo needs its **V+ on a separate 5–6 V supply**, not
   the Pi's 5 V pin — signal alone won't move it. The supply's ground **must be
   tied to a Pi ground** (common ground), or the pulse has no reference.
5. **Wrong pin / not the active servo.** `--gpio jaw=18` wires GPIO **18** (BCM
   numbering) to the jaw; check the signal wire is on that pin. `TAB` switches
   which servo the jog controls — the active one has a `▸` marker.

## Options

| Flag              | Meaning                                             |
| ----------------- | --------------------------------------------------- |
| `--gpio MAP`      | drive real servos off the Pi's GPIO via pigpio, e.g. `--gpio jaw=18,tail=17` (BCM pins) |
| `--pca`           | drive real servos via the PCA9685 board             |
| `--pulse-min US`  | lowest pulse the jog will reach, and the PCA9685 range floor (default 500 µs) |
| `--pulse-max US`  | highest pulse the jog will reach, and the PCA9685 range ceiling (default 2500 µs) |
