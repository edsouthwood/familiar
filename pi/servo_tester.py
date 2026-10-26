#!/usr/bin/env python3
"""Interactive servo range-finder — jog one servo with the keyboard to discover
the pulse widths each puppet part needs, then paste the numbers into
``body_client.py``.

``body_client.py`` drives the puppet from a fixed table (SERVO_PULSE_US + REST):
each servo's 0–180° command is mapped linearly onto a min_us…max_us pulse span,
and REST is the neutral angle. This tool is how you *find* those numbers on the
bench: pick a servo, nudge its raw pulse width up and down until it sits at each
pose you want (one end-stop, the other end-stop, neutral), mark them, and it
prints the ``SERVO_PULSE_US`` / ``REST`` lines ready to drop in.

    Mock (no hardware — just prints the pulse as you jog):
        python pi/servo_tester.py

    Real, straight off the Pi's GPIO (needs pigpiod — see body_client.py):
        python pi/servo_tester.py --gpio jaw=18,eyes_l=27,eyes_r=22,tail=17
        (servo names: jaw, ears_l, ears_r, eyes_l, eyes_r, tail — the eyes and
         ears are a pair each, so wire/jog them by side)

    Real, via the PCA9685 board (needs adafruit-circuitpython-servokit):
        python pi/servo_tester.py --pca

Wire up as many servos as you like; TAB cycles between them, so you can walk the
whole puppet in one sitting and copy out the full table at the end.

Keys:
    w / s        jog the pulse up / down by the current step
    d / a        bigger / smaller step   (arrow keys work too, when the
                 terminal reports them in a form we recognise)
    n            mark HERE as the min_us end (this pose becomes 0°)
    x            mark HERE as the max_us end (this pose becomes 180°)
    r            mark HERE as REST (the neutral pose)
    m NAME↵      mark HERE as a named pose (e.g. "open", "shut") — reported too
    c            jump to centre (1500 µs)
    space        relax the servo (cut the pulse); any jog re-engages it
    TAB          switch to the next wired servo
    p            print the calibration snippet so far
    q            quit and print the full report
"""

from __future__ import annotations

import argparse
import os
import select
import sys
import termios
import tty
from pathlib import Path

# Reuse the single source of truth for channel numbers/names and the pin parser
# so this tool and body_client.py can never drift apart. Running the script puts
# its own dir (pi/) on sys.path[0], so a plain import finds the sibling module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from body_client import CHANNEL_NAMES, REST, SERVO_PULSE_US, parse_pin_map  # noqa: E402

# Safety rails for the jog. 500–2500 µs is a typical hobby servo's full sweep;
# going much past the ends is how you make a servo buzz, strain, or strip a
# linkage, so we clamp there. Override with --pulse-min/--pulse-max if a
# particular servo genuinely wants more (rare) or you want a tighter sandbox.
DEFAULT_PULSE_MIN = 500
DEFAULT_PULSE_MAX = 2500
CENTRE_US = 1500
STEPS = (1, 2, 5, 10, 25, 50)          # µs per keypress; → / ← walk this list


# ── Raw-pulse backends ───────────────────────────────────────────────────
# body_client's drivers speak *angles* (0–180° through the calibration table);
# here we need the raw microseconds underneath that, since finding the range IS
# the thing the calibration table records. Small self-contained drivers, one per
# way of wiring a servo.
class MockBackend:
    """No hardware: the on-screen panel shows the pulse you're commanding, which
    is all you need to rehearse the tool before wiring anything up."""

    def __init__(self):
        self.channels = list(CHANNEL_NAMES)  # offer all four to jog around

    def send(self, channel: int, us: int) -> None:
        pass  # the live panel already shows `us`; nothing to drive

    def relax(self, channel: int) -> None:
        pass

    def close(self) -> None:
        pass


class GpioBackend:
    """Raw pulses straight off the Pi's GPIO via pigpio — same wiring as
    body_client's GpioServos (signal → BCM pin, servo V+ → external 5V, grounds
    common)."""

    def __init__(self, pins: dict):
        import pigpio

        self._pi = pigpio.pi()
        if not self._pi.connected:
            raise SystemExit("pigpio daemon not running — start it with: sudo pigpiod")
        self._pins = pins
        self.channels = list(pins)

    def send(self, channel: int, us: int) -> None:
        self._pi.set_servo_pulsewidth(self._pins[channel], int(us))

    def relax(self, channel: int) -> None:
        self._pi.set_servo_pulsewidth(self._pins[channel], 0)  # 0 = pulses off

    def close(self) -> None:
        for pin in self._pins.values():
            try:
                self._pi.set_servo_pulsewidth(pin, 0)
            except Exception:
                pass
        self._pi.stop()


class PcaBackend:
    """Raw pulses via the PCA9685 board. ServoKit only speaks angles, so we pin
    each servo's 0–180° span to the jog's pulse range once and translate our
    target microseconds back into the angle that produces them — giving direct
    µs control on top. Matching the range to the jog limits keeps the on-screen
    pulse honest even when --pulse-min/--pulse-max widen it."""

    def __init__(self, pulse_min: int, pulse_max: int):
        from adafruit_servokit import ServoKit

        self._lo, self._hi = pulse_min, pulse_max
        self._kit = ServoKit(channels=16)
        for channel in CHANNEL_NAMES:
            servo = self._kit.servo[channel]
            servo.actuation_range = 180
            servo.set_pulse_width_range(self._lo, self._hi)
        self.channels = list(CHANNEL_NAMES)

    def send(self, channel: int, us: int) -> None:
        us = max(self._lo, min(self._hi, us))
        self._kit.servo[channel].angle = (us - self._lo) / (self._hi - self._lo) * 180

    def relax(self, channel: int) -> None:
        self._kit.servo[channel].angle = None  # cut the PWM so it goes slack

    def close(self) -> None:
        for channel in self.channels:
            try:
                self._kit.servo[channel].angle = None
            except Exception:
                pass


# ── Keyboard ─────────────────────────────────────────────────────────────
class Keyboard:
    """Read single keypresses (including arrow keys) from a raw terminal, with a
    context manager that restores cooked mode on the way out."""

    # Normal cursor mode sends 'Esc [ A'; application cursor mode (many real
    # terminals, incl. the Pi console under some setups) sends 'Esc O A'. Accept
    # both so arrow keys work wherever possible — but wasd is the reliable path.
    ARROWS = {"[A": "UP", "[B": "DOWN", "[C": "RIGHT", "[D": "LEFT",
              "OA": "UP", "OB": "DOWN", "OC": "RIGHT", "OD": "LEFT"}

    def __enter__(self):
        self._fd = sys.stdin.fileno()
        self._saved = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc):
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)

    def key(self) -> str:
        """Block for one key. Returns 'UP'/'DOWN'/'LEFT'/'RIGHT' for the arrows,
        'ESC' for a bare Escape, or the literal character otherwise."""
        ch = sys.stdin.read(1)
        if ch != "\x1b":
            return ch
        # Escape: an arrow sends '\x1b[A' etc. A lone Esc has nothing queued
        # behind it, so peek with a short timeout to tell them apart. 0.3s (not
        # a few ms) so the trailing bytes of an arrow key still arrive in time
        # over a laggy SSH link; the only cost is a bare Esc taking that long to
        # register, which is fine since 'q' also quits.
        ready, _, _ = select.select([sys.stdin], [], [], 0.3)
        if not ready:
            return "ESC"
        return self.ARROWS.get(sys.stdin.read(2), "ESC")

    def line(self, prompt: str) -> str:
        """Read a whole line in cooked mode (for typing a pose name)."""
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
        try:
            sys.stdout.write(prompt)
            sys.stdout.flush()
            return sys.stdin.readline().strip()
        finally:
            tty.setcbreak(self._fd)


# ── The tester ───────────────────────────────────────────────────────────
class ServoState:
    """Per-servo scratch: where the pulse is now and every pose you've marked."""

    def __init__(self, channel: int, start_us: int):
        self.channel = channel
        self.us = start_us
        self.min_us: int | None = None
        self.max_us: int | None = None
        self.rest_us: int | None = None
        self.poses: dict[str, int] = {}  # extra named poses → µs

    def angle_of(self, us: int) -> float | None:
        """Where `us` lands on the 0–180° scale set by the marked min/max, or
        None until both ends are marked."""
        if self.min_us is None or self.max_us is None or self.max_us == self.min_us:
            return None
        span = self.max_us - self.min_us
        return max(0.0, min(180.0, (us - self.min_us) / span * 180))


class Tester:
    def __init__(self, backend, pulse_min: int, pulse_max: int):
        self.backend = backend
        self.lo, self.hi = pulse_min, pulse_max
        self.states = {ch: ServoState(ch, CENTRE_US) for ch in backend.channels}
        self.order = list(backend.channels)
        self.active = self.order[0]
        self.step_i = STEPS.index(25) if 25 in STEPS else len(STEPS) // 2
        self.relaxed: set[int] = set()
        self.message = ""
        self._drawn = 0  # how many lines the live panel currently occupies

    @property
    def step(self) -> int:
        return STEPS[self.step_i]

    @property
    def state(self) -> ServoState:
        return self.states[self.active]

    def drive(self, engage: bool = True) -> None:
        """Push the active servo's current pulse to the hardware."""
        if engage:
            self.relaxed.discard(self.active)
        self.backend.send(self.active, self.state.us)

    def jog(self, delta: int) -> None:
        s = self.state
        s.us = max(self.lo, min(self.hi, s.us + delta))
        self.drive()

    def handle(self, kb: Keyboard, k: str) -> bool:
        """Act on one key. Returns False to quit."""
        s = self.state
        self.message = ""
        if k in ("q", "\x03"):            # q / Ctrl-C  (a bare/misread Esc is a
            return False                 # no-op so a stray arrow can't quit)
        elif k in ("UP", "w"):
            self.jog(self.step)
        elif k in ("DOWN", "s"):
            self.jog(-self.step)
        elif k in ("RIGHT", "d"):
            self.step_i = min(len(STEPS) - 1, self.step_i + 1)
        elif k in ("LEFT", "a"):
            self.step_i = max(0, self.step_i - 1)
        elif k == "c":
            s.us = CENTRE_US
            self.drive()
        elif k == "n":
            s.min_us = s.us
            self.message = f"min_us = {s.us} (this pose is now 0°)"
        elif k == "x":
            s.max_us = s.us
            self.message = f"max_us = {s.us} (this pose is now 180°)"
        elif k == "r":
            s.rest_us = s.us
            self.message = f"REST marked at {s.us}µs"
        elif k == "m":
            name = kb.line("  name this pose: ")
            if name:
                s.poses[name] = s.us
                self.message = f"pose {name!r} = {s.us}µs"
        elif k == "\t":
            i = (self.order.index(self.active) + 1) % len(self.order)
            self.active = self.order[i]
        elif k == " ":
            self.backend.relax(self.active)
            self.relaxed.add(self.active)
            self.message = "relaxed — jog to re-engage"
        elif k == "p":
            self.print_report(final=False)
        return True

    # ── display ──
    def _panel_lines(self) -> list[str]:
        lines = [f"  step {self.step:>3}µs      {self.message}"]
        for ch in self.order:
            s = self.states[ch]
            here = "▸" if ch == self.active else " "
            slack = "  (relaxed)" if ch in self.relaxed else ""
            angle = s.angle_of(s.us)
            ang = f"{angle:5.0f}°" if angle is not None else "   -- "
            marks = []
            marks.append(f"min {s.min_us}" if s.min_us is not None else "min --")
            marks.append(f"max {s.max_us}" if s.max_us is not None else "max --")
            marks.append(f"rest {s.rest_us}" if s.rest_us is not None else "rest --")
            lines.append(
                f"  {here} {CHANNEL_NAMES[ch]:<8} {s.us:>4}µs  {ang}   "
                f"{'  '.join(marks)}{slack}"
            )
        return lines

    def paint(self) -> None:
        lines = self._panel_lines()
        out = f"\x1b[{self._drawn}A" if self._drawn else ""
        out += "".join(f"\r\x1b[K{ln}\n" for ln in lines)
        sys.stdout.write(out)
        sys.stdout.flush()
        self._drawn = len(lines)

    # ── report ──
    def print_report(self, final: bool) -> None:
        # Break the live panel so the report scrolls beneath it cleanly.
        sys.stdout.write("\n")
        self._drawn = 0
        touched = [s for ch in self.order if (s := self.states[ch]).min_us is not None
                   or s.max_us is not None or s.rest_us is not None or s.poses]
        header = "final report" if final else "snippet so far"
        print(f"\n── {header} — paste into pi/body_client.py ──\n")
        if not touched:
            print("  (nothing marked yet — jog to a pose and press n / x / r)\n")
            return

        pulse_lines, rest_lines, notes = [], [], []
        for s in touched:
            const = CHANNEL_NAMES[s.channel].upper()  # jaw → JAW, etc.
            lo = s.min_us if s.min_us is not None else "?"
            hi = s.max_us if s.max_us is not None else "?"
            pulse_lines.append(f"    {const + ':':<9}({lo}, {hi}),")
            if s.rest_us is not None:
                ang = s.angle_of(s.rest_us)
                rest = round(ang) if ang is not None else "?"
                rest_lines.append(f"    {const}: {rest},"
                                  f"   # {s.rest_us}µs")
            for name, us in s.poses.items():
                ang = s.angle_of(us)
                a = f"{ang:.0f}°" if ang is not None else "?"
                notes.append(f"    # {CHANNEL_NAMES[s.channel]} {name}: "
                             f"{us}µs → {a}")

        print("SERVO_PULSE_US = {")
        for ln in pulse_lines:
            print(ln)
        print("}")
        if rest_lines:
            print("\nREST = {  # (only the parts you measured; keep the rest)")
            for ln in rest_lines:
                print(ln)
            print("}")
        if notes:
            print("\n# extra poses (angles for Animator._render):")
            for ln in notes:
                print(ln)
        print()

    def run(self) -> None:
        print(__doc__.split("Keys:")[1].rstrip())  # reprint the key legend
        print(f"\n  pulse limits {self.lo}–{self.hi}µs   "
              f"{len(self.order)} servo(s): "
              f"{', '.join(CHANNEL_NAMES[c] for c in self.order)}\n")
        if isinstance(self.backend, MockBackend):
            # The mock panel updates exactly like the real thing, so without this
            # it's easy to jog for ages wondering why no servo moves.
            print("  ****************************************************************")
            print("  **  MOCK MODE — NO HARDWARE IS BEING DRIVEN.                  **")
            print("  **  The pulse below updates as you jog, but nothing moves.    **")
            print("  **  Re-run with --gpio <name=pin,...> or --pca to drive real  **")
            print("  **  servos (valid names: jaw, ears_l, ears_r, eyes_l,         **")
            print("  **  eyes_r, tail).                                            **")
            print("  ****************************************************************\n")
        self.drive()
        with Keyboard() as kb:
            self.paint()
            while True:
                k = kb.key()
                if not self.handle(kb, k):
                    break
                self.paint()
        self.print_report(final=True)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Jog a servo by keyboard to find its pulse range for body_client.py.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--gpio", metavar="MAP",
                   help="drive real servos off the Pi's GPIO via pigpio, e.g. "
                        "--gpio jaw=18,tail=17 (BCM pins)")
    g.add_argument("--pca", action="store_true",
                   help="drive real servos via the PCA9685 board")
    p.add_argument("--pulse-min", type=int, default=DEFAULT_PULSE_MIN,
                   help=f"lowest pulse the jog will reach (default {DEFAULT_PULSE_MIN}µs)")
    p.add_argument("--pulse-max", type=int, default=DEFAULT_PULSE_MAX,
                   help=f"highest pulse the jog will reach (default {DEFAULT_PULSE_MAX}µs)")
    args = p.parse_args()

    if args.gpio:
        backend = GpioBackend(parse_pin_map(args.gpio))
    elif args.pca:
        backend = PcaBackend(args.pulse_min, args.pulse_max)
    else:
        backend = MockBackend()

    if not sys.stdin.isatty():
        raise SystemExit("servo_tester needs an interactive terminal (a TTY).")

    tester = Tester(backend, args.pulse_min, args.pulse_max)
    try:
        tester.run()
    finally:
        backend.close()


if __name__ == "__main__":
    main()
