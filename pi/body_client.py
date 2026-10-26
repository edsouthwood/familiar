#!/usr/bin/env python3
"""Whiskers' animatronic body — the puppet side of the state bus.

Runs on the Pi Zero W inside the cat. It holds a WebSocket to its table's bus
at ``/t/<slug>/ws/state`` (see server.py), receives the five states —
asleep, idle, listening, thinking, speaking — and drives the puppet's servos
to match. It's just another body on the bus; the on-screen cat is the other.

By default it's a MOCK: instead of moving real servos it prints every move, so
the whole chain (a screen wakes → server broadcasts → the puppet reacts) can
be proven on a laptop before any hardware exists. Pass ``--real`` to drive the
servos on a PCA9685 board for real; ``--calibrate <servo>`` helps you dial in
where each servo's 0° lands and how far it travels (see SERVO_PULSE_US).

The cat's moves:
    asleep    → slumped: eyes shut, ears down, jaw closed, tail still
    idle      → eyes open, slow tail sway, an occasional blink
    listening → eyes wide, a quick one-ear flick every few seconds
    thinking  → ears waggle gently in opposite directions, fast tail wag
    speaking  → jaw flaps (later: driven by the TTS audio envelope)

Two LEDs behind the eyes ride on the same PCA9685 (channels 6-7) and are the
puppet's "is anyone home" tell: dark asleep, lit whenever it is awake, easing
between the two so waking reads as the eyes kindling. See the LED tables below
and ``--leds`` to bench-test them.

Pair it with a table once (the DM mints the code in "Running the table"):
    python pi/body_client.py --pair WXYZ-2345 --host https://familiar.example

That swaps the short code for a long-lived device token, writes it to
``~/.config/familiar/device.json``, and remembers which table this body belongs
to. Every run afterwards needs no arguments at all:
    python pi/body_client.py --real

Run against a local dev server instead (no pairing, no table):
    .venv/bin/python pi/body_client.py --url ws://127.0.0.1:8321/t/<slug>/ws/state
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import threading
import time
from pathlib import Path

import simple_websocket


def load_dotenv() -> None:
    """Populate os.environ from a nearby ``.env`` (this dir, the cwd, and the
    repo root) so ``FAMILIAR_*`` settings can live in a file instead of the
    command line. Deliberately tiny and dependency-free; real values already in
    the environment always win."""
    here = Path(__file__).resolve().parent
    for path in (here / ".env", Path.cwd() / ".env", here.parent / ".env"):
        try:
            text = path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))

# ── Servos ──────────────────────────────────────────────────────────────
# The puppet has SIX servos, but only FOUR things move: the jaw and the tail
# are single servos, while the ears and the eyes are each a PAIR of servos that
# move in unison (one per side of the head). So there are two layers:
#
#   • PHYSICAL CHANNELS — one entry per servo (what you wire and calibrate).
#   • LOGICAL PARTS     — what the animation drives; a part fans its 0–180°
#                         command out to its one or two channels.
#
# Plug the servos into these PCA9685 channels (or edit the numbers to match how
# you actually wired them). Angles are degrees, 0–180.
CHANNEL_NAMES = {
    0: "jaw",
    1: "ears_l",
    2: "ears_r",
    3: "eyes_l",
    4: "eyes_r",
    5: "tail",
}

# Logical parts the Animator speaks in, and the channel(s) each one drives. Ears
# and eyes are pairs: one command goes to both members.
JAW, EARS, EYES, TAIL = "jaw", "ears", "eyes", "tail"
PART_CHANNELS = {
    JAW:  (0,),
    EARS: (1, 2),
    EYES: (3, 4),
    TAIL: (5,),
}

# Mirror-mounted pair members. The two servos of a pair usually sit on opposite
# sides of the head, so "ears up" turns them in OPPOSITE rotational directions.
# Put the reversed servo's channel here and it's driven to (180 - angle) so the
# pair moves together physically. Start empty; during bench calibration you'll
# see one ear/eye swing the wrong way — add its channel here and re-run. (If a
# pair happens to be mounted the same way round, leave it out.)
INVERT: set[int] = set()
INVERT.update({1, 3})  # ears_l, eyes_l mounted reversed relative to their pair

# REST is the relaxed/neutral pose in degrees, PER LOGICAL PART. Both members of
# a pair rest here (mirrored if inverted). The animation angles in
# Animator._render ride on top of these.
REST = {JAW: 10, EARS: 60, EYES: 0, TAIL: 90}


def rest_angles() -> dict[int, float]:
    """REST expanded to every physical channel, mirroring inverted pair members
    — seeds the mock display and the per-servo calibrate loop."""
    out: dict[int, float] = {}
    for part, angle in REST.items():
        for ch in PART_CHANNELS[part]:
            out[ch] = 180.0 - angle if ch in INVERT else float(angle)
    return out


def drive_part(servos: Servos, part: str, angle: float) -> None:
    """Drive a logical part to `angle` (0–180°), fanning it out to its one or two
    channels and mirroring any pair member listed in INVERT so the pair moves
    together. Shared by the Animator and the bench helpers."""
    angle = max(0.0, min(180.0, angle))
    for ch in PART_CHANNELS[part]:
        servos.move(ch, 180.0 - angle if ch in INVERT else angle)


def drive_ears_split(servos: Servos, left: float, right: float) -> None:
    """Drive the two ear servos to *different* angles (same 0–180° logical space
    as drive_part, INVERT still honoured) — lets the thinking waggle move the
    ears in opposite physical directions instead of in unison."""
    for ch, angle in zip(PART_CHANNELS[EARS], (left, right)):
        angle = max(0.0, min(180.0, angle))
        servos.move(ch, 180.0 - angle if ch in INVERT else angle)

# ── Servo calibration (the "where's 0 / how far does it turn" knobs) ──────
# A hobby servo is positioned by the *width* of the pulse it's fed (not its
# voltage): a short pulse drives it to one mechanical end, a long pulse to the
# other. We map each servo's 0–180° command linearly onto its own pulse range:
#
#     angle   0° → min_us   ←  THIS IS WHERE 0 IS: one physical end-stop
#     angle  90° → midpoint
#     angle 180° → max_us   ←  the other end-stop
#
# Two knobs per servo, both set by the (min_us, max_us) pair below:
#
#   • WHERE 0 SITS — min_us picks the pulse that means "0°", but where that
#     lands *physically* also depends on how you press the horn onto the
#     spline. So: power up, command the servo to 0° (or REST) with
#     `--calibrate <servo>` BEFORE fitting the horn, then seat the horn at the
#     pose you want. The horn sets the coarse offset (splines are ~18° apart);
#     nudge min_us to fine-tune.
#
#   • HOW FAR IT ROTATES — the width of the min_us…max_us span. Wider span =
#     more physical travel for the same 0–180° command; narrower = less. If a
#     servo buzzes, strains, or slams a linkage at the extremes, pull the
#     ends in (e.g. 700–2300). 500–2500 µs is a typical SG90 at ~full 180°.
#
# Run `--calibrate jaw` (etc.) to watch where 0/90/180 land, adjust the pair,
# re-run. The animation angles in Animator._render all sit inside 0–180 and
# ride on top of whatever mapping you set here.
# Keyed by PHYSICAL channel — each servo of a pair calibrates independently.
SERVO_PULSE_US = {
    0: (500, 2500),  # jaw
    1: (500, 2500),  # ears_l
    2: (500, 2500),  # ears_r
    3: (500, 2500),  # eyes_l
    4: (500, 2500),  # eyes_r
    5: (500, 2500),  # tail
}

# ── Eye LEDs ────────────────────────────────────────────────────────────
# An LED behind each eye, on the SAME PCA9685 as the servos — it is a 16-channel
# PWM chip and only six channels are servos, so 6 and 7 are free. Wire each LED
# to its channel's 3-pin header:
#
#     PWM pin ──[ 180-330 Ω ]──▶|── GND        (both pins are on that header)
#
# i.e. the output SOURCES the current at logic level (3.3 V, since VCC comes
# from the Pi's 3V3). That caps a channel at 10 mA, which is plenty for an
# indicator LED, and it means the LEDs are DARK at power-up — the chip's
# outputs come out of reset low. Use a red or amber LED: at 3.3 V there is no
# headroom for white/blue (Vf ≈ 3.1 V), and red/amber is the right colour for a
# familiar's eyes anyway. For white/blue, or for a brighter eye, wire the other
# way round instead — V+ ──[R]──▶|── PWM pin, so the output SINKS (25 mA
# allowed, and 5 V across it) — and list the channel in LED_INVERT below,
# because then the LED lights when the output is LOW.
LED_NAMES = {
    6: "eye_l",
    7: "eye_r",
}

# Logical LED part, same idea as PART_CHANNELS: one command, both eyes.
EYES_LED = "eyeglow"
LED_CHANNELS = {EYES_LED: (6, 7)}

# Sink-wired channels (see above): they light when the output is low, so their
# duty cycle is flipped on the way to the hardware. Empty = the source wiring.
LED_INVERT: set[int] = set()

# Per-eye brightness trim, 0-1. LEDs from the same bag can still differ; if one
# eye is hotter than the other, pull it down here rather than rewiring.
LED_TRIM = {6: 1.0, 7: 1.0}

# How bright the eyes are in each state. NOTE the default: every awake state is
# 1.0, and that is deliberate rather than lazy. The LEDs share the servos'
# 50 Hz PWM frame (one chip, one frequency), so any level BETWEEN 0 and 1
# strobes at 50 Hz — visible as a shimmer when the head moves. At exactly 1.0
# the driver uses the chip's full-on bit: steady DC, no flicker. So the eyes
# get their character from transitions (the fade below, the blink dip) instead
# of from a dim steady glow. To make a state dimmer anyway, either accept the
# flicker, or fit a bigger series resistor and leave this at 1.0.
LED_LEVEL = {
    "asleep": 0.0,
    "idle": 1.0,
    "listening": 1.0,
    "thinking": 1.0,
    "speaking": 1.0,
}

# Seconds for a full 0→1 fade, and the slower 1→0 one: waking should look like
# the eyes catching, dozing off like them cooling down.
LED_FADE_UP_S = 0.8
LED_FADE_DOWN_S = 1.6

# What the glow is multiplied by while a blink is in progress — the lid coming
# down. Instant, not faded: a 120 ms blink would not survive a fade.
LED_BLINK_DIP = 0.1


def led_duty(channel: int, level: float) -> float:
    """Logical brightness (0 = dark, 1 = full) → the duty cycle to put on the
    wire. Sink-wired LEDs light when their output is LOW, so the channels in
    LED_INVERT are flipped here — the one place that knows about the wiring."""
    return 1.0 - level if channel in LED_INVERT else level


def drive_led(servos: Servos, part: str, level: float) -> None:
    """Drive a logical LED part to `level` (0-1), fanning it out to its
    channels and applying each one's LED_TRIM. The Animator's counterpart to
    drive_part."""
    level = max(0.0, min(1.0, level))
    for ch in LED_CHANNELS[part]:
        servos.led(ch, level * LED_TRIM.get(ch, 1.0))


class Servos:
    """Driver interface for everything the puppet drives — the servos and the
    eye LEDs, which hang off the same board. Values are clamped and only
    forwarded when they actually change, so subclasses can stay dumb."""

    def __init__(self):
        self._last: dict[int, float] = {}
        self._last_led: dict[int, float] = {}

    def move(self, channel: int, angle: float) -> None:
        angle = max(0.0, min(180.0, angle))
        if round(angle) == round(self._last.get(channel, -999)):
            return
        self._last[channel] = angle
        self._write(channel, angle)

    def led(self, channel: int, level: float) -> None:
        """Set one LED channel's brightness, 0 (dark) to 1 (full)."""
        level = max(0.0, min(1.0, level))
        if abs(level - self._last_led.get(channel, -1.0)) < 1 / 512:
            return  # below what the 12-bit chip could tell apart anyway
        self._last_led[channel] = level
        self._write_led(channel, level)

    def leds_off(self) -> None:
        """Darken every LED. Called on shutdown so a stopped process does not
        leave the eyes glowing, and once at start-up — a sink-wired LED
        (LED_INVERT) is ON while its output sits low, which is where the chip
        powers up."""
        for channel in LED_NAMES:
            self.led(channel, 0.0)

    def _write(self, channel: int, angle: float) -> None:  # pragma: no cover
        raise NotImplementedError

    def _write_led(self, channel: int, level: float) -> None:
        """Put one LED on the wire. Backends with no LEDs wired ignore it, so
        the animation runs unchanged on a puppet that has none."""
        pass

    def banner(self, text: str) -> None:
        """Announce a state change. Subclasses that own the terminal (MockServos)
        override this so the announcement plays nicely with their display."""
        print(text)

    def close(self) -> None:
        """Release any hardware on shutdown. No-op unless a driver needs it."""
        pass


class MockServos(Servos):
    """Shows moves instead of driving hardware. On a terminal it draws the
    servos as a live panel and redraws each line where it sits, so a fast flap
    or wag reads as motion rather than a wall of scrolling text. When stdout
    isn't a terminal (piped to a file) it falls back to plain per-channel lines,
    throttled so they stay readable. ``verbose`` removes the throttle."""

    CHANNELS = tuple(CHANNEL_NAMES)  # every physical servo channel, in order
    LEDS = tuple(LED_NAMES)          # then the LEDs, below them in the panel

    def __init__(self, throttle_s: float = 0.05, verbose: bool = False):
        super().__init__()
        self.throttle_s = 0.0 if verbose else throttle_s
        self._tty = sys.stdout.isatty()
        self._lock = threading.Lock()          # serialise cursor moves vs banners
        self._angles: dict[int, float] = rest_angles()  # every channel, so all show
        self._levels: dict[int, float] = {ch: 0.0 for ch in self.LEDS}
        self._last_paint = 0.0
        self._drawn = False                    # has the panel been laid down yet?
        self._last_print: dict[str, float] = {}  # per-row throttle (non-tty)

    def _rows(self) -> int:
        return len(self.CHANNELS) + len(self.LEDS)

    def _line(self, channel: int) -> str:
        angle = self._angles[channel]
        bar = "█" * round(angle / 180 * 20)
        return f"    servo {CHANNEL_NAMES[channel]:<8} {angle:5.0f}°  {bar}"

    def _led_line(self, channel: int) -> str:
        level = self._levels[channel]
        bar = "█" * round(level * 20)
        return f"    led   {LED_NAMES[channel]:<8} {level * 100:4.0f}%  {bar}"

    def _write(self, channel: int, angle: float) -> None:
        self._angles[channel] = angle
        if self._tty:
            self._paint()
        else:
            self._scroll(f"s{channel}", self._line(channel))

    def _write_led(self, channel: int, level: float) -> None:
        # The panel shows the LOGICAL brightness — what the animation asked
        # for — not the possibly-inverted duty that reaches the wire.
        self._levels[channel] = level
        if self._tty:
            self._paint()
        else:
            self._scroll(f"l{channel}", self._led_line(channel))

    def _paint(self) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_paint < self.throttle_s:
                return
            self._last_paint = now
            # Step the cursor back up over the panel (if drawn), then rewrite
            # every line in place — \r\x1b[K clears each before printing.
            out = f"\x1b[{self._rows()}A" if self._drawn else ""
            out += "".join(f"\r\x1b[K{self._line(ch)}\n" for ch in self.CHANNELS)
            out += "".join(f"\r\x1b[K{self._led_line(ch)}\n" for ch in self.LEDS)
            sys.stdout.write(out)
            sys.stdout.flush()
            self._drawn = True

    def _scroll(self, key: str, line: str) -> None:
        now = time.monotonic()
        if now - self._last_print.get(key, 0) < self.throttle_s:
            return
        self._last_print[key] = now
        print(line)

    def banner(self, text: str) -> None:
        with self._lock:
            print(text)
            self._drawn = False  # next paint lays a fresh panel below the header


class Pca9685Servos(Servos):
    """Real hardware: the servos on a PCA9685 16-channel PWM board over I2C.

        Pi 3V3 → PCA9685 VCC,  Pi GND → GND,  Pi SDA/SCL → SDA/SCL
        Servo power (5–6V, its OWN supply) → PCA9685 V+   (NOT the Pi's 5V)

    Needs (on the Pi, with I2C enabled via `raspi-config`):
        pip install adafruit-circuitpython-servokit

    Each channel's 0–180° is mapped onto its own pulse-width range from
    SERVO_PULSE_US — see the calibration notes up there for where 0 lands and
    how to widen/narrow the travel. Nothing else in this class needs touching
    to tune the movement; it all comes from that table and REST.

    The same chip drives the eye LEDs on its spare channels (LED_NAMES). It is
    a PWM chip, not a servo chip — ServoKit just wraps six of the sixteen
    channels in pulse-width maths — so the LED channels are driven raw, through
    the PCA9685 object ServoKit built.
    """

    def __init__(self):
        super().__init__()
        from adafruit_servokit import ServoKit  # imported lazily; Pi-only dep

        # 50 Hz (ServoKit's default) is the standard hobby-servo frame rate.
        # It is the LEDs' frame rate too — one chip, one frequency — which is
        # why LED_LEVEL keeps the awake states at a flicker-free full on.
        self._kit = ServoKit(channels=16)
        for channel, (min_us, max_us) in SERVO_PULSE_US.items():
            servo = self._kit.servo[channel]
            servo.actuation_range = 180              # 0–180° commands
            servo.set_pulse_width_range(min_us, max_us)  # ← the calibration
        wired = ", ".join(f"{CHANNEL_NAMES[ch]} ch{ch} {lo}-{hi}µs"
                          for ch, (lo, hi) in SERVO_PULSE_US.items())
        print(f"PCA9685 ready — {wired}")

        # ServoKit owns the underlying PCA9685; its raw channels are how the
        # LEDs get driven. If a future version renames it, say so and carry on
        # with the servos rather than refusing to run the puppet at all.
        pca = getattr(self._kit, "_pca", None)
        self._leds = getattr(pca, "channels", None)
        if self._leds is None:
            print("  note: cannot reach the PCA9685's raw channels — eye LEDs "
                  "disabled, servos unaffected")
        else:
            lit = ", ".join(f"{name} ch{ch}" for ch, name in LED_NAMES.items())
            print(f"  eye LEDs — {lit}")
            self.leds_off()  # the chip powers up with its outputs low

    def _write(self, channel: int, angle: float) -> None:
        self._kit.servo[channel].angle = angle

    def _write_led(self, channel: int, level: float) -> None:
        if self._leds is None:
            return
        # duty_cycle is 16-bit; 0xFFFF is special-cased by the driver into the
        # chip's full-ON bit, i.e. a steady output with no 50 Hz chopping.
        self._leds[channel].duty_cycle = round(led_duty(channel, level) * 0xFFFF)

    def close(self) -> None:
        # angle = None cuts the PWM pulses so the servos go slack on shutdown
        # instead of straining to hold a pose (and buzzing).
        for channel in SERVO_PULSE_US:
            try:
                self._kit.servo[channel].angle = None
            except Exception:
                pass
        try:
            self.leds_off()  # don't leave the eyes glowing over a dead process
        except Exception:
            pass


class GpioServos(Servos):
    """Drive servos straight from the Pi's GPIO with pigpio — DMA-timed PWM on
    any pin, glitch-free (plain software PWM makes servos jitter). The stop-gap
    until the PCA9685 arrives.

    Only the channels present in ``pins`` are wired; moves to any other channel
    are ignored, so the whole animation still runs with just a couple of servos
    connected. Angle 0–180° maps linearly onto ``min_us``–``max_us`` pulse
    widths — re-measure those for your servos (most hum or strain if driven past
    their real end-stops; narrow the range if so).

    Eye LEDs work here too — name them like any other channel
    (``--gpio eye_l=23,eye_r=24``) and they get ordinary PWM at LED_PWM_HZ
    rather than servo pulses. Unlike the PCA9685 they are on their own
    frequency, so partial brightness does not flicker.

    Setup:  sudo apt install pigpio  &&  sudo systemctl enable --now pigpiod
            pip install pigpio
    Wiring: servo signal → the GPIO pin (BCM numbering)
            servo V+ → your external 5V supply   (NOT the Pi's 5V pin)
            servo GND, supply GND and a Pi GND all tied together (common ground)
            LED: GPIO pin ──[180-330 Ω]──▶|── a Pi GND
    """

    LED_PWM_HZ = 500     # well above anything an eye can see strobe
    LED_STEPS = 1000     # duty-cycle resolution pigpio scales to the real range

    def __init__(self, pins: dict, min_us: int = 500, max_us: int = 2500):
        super().__init__()
        import pigpio  # pip install pigpio; talks to the pigpiod daemon

        self._pi = pigpio.pi()  # connects to pigpiod on localhost
        if not self._pi.connected:
            raise SystemExit("pigpio daemon not running — start it with: sudo pigpiod")
        # One map in, two out: servo pins get timed pulses, LED pins get PWM.
        self._pins = {ch: pin for ch, pin in pins.items() if ch not in LED_NAMES}
        self._led_pins = {ch: pin for ch, pin in pins.items() if ch in LED_NAMES}
        self._min_us, self._max_us = min_us, max_us
        if self._pins:
            wired = ", ".join(f"{CHANNEL_NAMES[ch]}=GPIO{pin}"
                              for ch, pin in self._pins.items())
            print(f"GPIO servos: {wired}  (pulse {min_us}-{max_us}µs)")
        for ch, pin in self._led_pins.items():
            self._pi.set_PWM_frequency(pin, self.LED_PWM_HZ)
            self._pi.set_PWM_range(pin, self.LED_STEPS)
        if self._led_pins:
            wired = ", ".join(f"{LED_NAMES[ch]}=GPIO{pin}"
                              for ch, pin in self._led_pins.items())
            print(f"GPIO LEDs:   {wired}  ({self.LED_PWM_HZ} Hz)")
            self.leds_off()

    def _write(self, channel: int, angle: float) -> None:
        pin = self._pins.get(channel)
        if pin is None:
            return  # this servo isn't wired up — ignore silently
        pulse = self._min_us + (self._max_us - self._min_us) * angle / 180.0
        self._pi.set_servo_pulsewidth(pin, int(round(pulse)))

    def _write_led(self, channel: int, level: float) -> None:
        pin = self._led_pins.get(channel)
        if pin is None:
            return  # this LED isn't wired up — ignore silently
        self._pi.set_PWM_dutycycle(pin, round(led_duty(channel, level) * self.LED_STEPS))

    def close(self) -> None:
        # Stop the pulses so the servos go slack instead of buzzing, put the
        # eyes out, then drop the pigpio connection.
        for pin in self._pins.values():
            try:
                self._pi.set_servo_pulsewidth(pin, 0)
            except Exception:
                pass
        for pin in self._led_pins.values():
            try:
                self._pi.set_PWM_dutycycle(pin, 0)
            except Exception:
                pass
        self._pi.stop()


def parse_pin_map(spec: str) -> dict:
    """Turn "jaw=18,eye_l=23" into {0: 18, 6: 23} (BCM pin numbers). Servos and
    eye LEDs share one name space here — GpioServos tells them apart by
    channel, since LED_NAMES and CHANNEL_NAMES never overlap."""
    by_name = {name: ch for ch, name in CHANNEL_NAMES.items()}
    by_name.update({name: ch for ch, name in LED_NAMES.items()})
    pins: dict = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, pin = part.partition("=")
        name = name.strip().lower()
        if name not in by_name:
            raise SystemExit(
                f"--gpio: unknown servo/LED {name!r}; pick from {', '.join(by_name)}")
        try:
            pins[by_name[name]] = int(pin)
        except ValueError:
            raise SystemExit(f"--gpio: bad pin in {part!r} — expected e.g. jaw=18")
    if not pins:
        raise SystemExit("--gpio needs at least one name=pin, e.g. --gpio jaw=18,eye_l=23")
    return pins


# ── Animator ────────────────────────────────────────────────────────────
class Animator:
    """Turns the current state into continuous servo motion (and the eye glow)
    on a background thread. The websocket loop just sets ``self.state`` (and
    optional ``self.jaw_level`` from future viseme messages); this class owns
    the poses, the blink cadence, the sway, the wag, the flap and the LEDs."""

    TICK = 0.05  # 20 Hz — smooth enough for a puppet, gentle on a Pi Zero
    JAW_LEVEL_TTL = 0.25  # a viseme older than this is stale; resume flapping

    def __init__(self, servos: Servos):
        self.servos = servos
        self.state = "asleep"
        self.jaw_level: float | None = None  # set by a viseme msg; overrides flap
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._t0 = time.monotonic()
        self._jaw_level_at = 0.0
        self._next_blink = 0.0
        self._blink_until = 0.0
        self._next_flick = 0.0
        self._flick_until = 0.0
        self._flick_side = 0
        self._glow = 0.0  # eye-LED brightness, eased toward LED_LEVEL[state]

    def set_state(self, state: str) -> None:
        with self._lock:
            if state == self.state:
                return
            self.state = state
            self.jaw_level = None
        self.servos.banner(f"\n[{state}]  {self._describe(state)}")

    def set_jaw_level(self, level: float) -> None:
        # A future {"type":"viseme","level":0-1} message drives the jaw directly
        # from the TTS envelope; falls back to the synthetic flap when absent.
        with self._lock:
            self.jaw_level = max(0.0, min(1.0, level))
            self._jaw_level_at = time.monotonic()

    @staticmethod
    def _describe(state: str) -> str:
        return {
            "asleep": "slumped — eyes dark and shut, ears down, jaw closed, tail still",
            "idle": "awake — eyes lit, slow tail sway, the odd blink",
            "listening": "eyes wide, the odd one-ear flick",
            "thinking": "ears waggling in antiphase, tail wagging fast",
            "speaking": "jaw flapping",
        }.get(state, "(unknown state — holding rest pose)")

    def run(self) -> None:
        while not self._stop.wait(self.TICK):
            now = time.monotonic()
            with self._lock:
                # A viseme stream that stalls mid-utterance would otherwise hold
                # the jaw at its last angle until the state changed.
                if (self.jaw_level is not None
                        and now - self._jaw_level_at > self.JAW_LEVEL_TTL):
                    self.jaw_level = None
                state, jaw_level = self.state, self.jaw_level
            self._render(state, jaw_level, now - self._t0)

    def stop(self) -> None:
        self._stop.set()

    def relax(self) -> None:
        """Ease everything back to rest and put the eyes out — call on
        shutdown, so a stopped puppet looks asleep rather than dead."""
        for part, angle in REST.items():
            self._move(part, angle)
        self._glow = 0.0
        drive_led(self.servos, EYES_LED, 0.0)

    def _move(self, part: str, angle: float) -> None:
        drive_part(self.servos, part, angle)

    def _render(self, state: str, jaw_level: float | None, t: float) -> None:
        mv = self._move

        # Ears: down when asleep, at the awake pose (REST, 60°) otherwise — the
        # model's linkage can't take more than 60°, so no state goes past it.
        # Thinking adds a gentle ±10° antiphase waggle, centred 10° low so the
        # peaks just touch 60° rather than overshooting.
        if state == "thinking":
            delta = 10 * math.sin(t * 6)  # ~1 Hz back-and-forth
            base = REST[EARS] - 10
            drive_ears_split(self.servos, base + delta, base - delta)
        elif state == "listening":
            # Listening cue: a quick single-ear flick every few seconds — one
            # ear dips ~12° and pops back, the other holds still.
            side = self._ear_flick(t)
            left = REST[EARS] - (12 if side == 0 else 0)
            right = REST[EARS] - (12 if side == 1 else 0)
            drive_ears_split(self.servos, left, right)
        else:
            mv(EARS, 20 if state == "asleep" else REST[EARS])

        # Eyes: shut asleep, otherwise open with an occasional blink while idle.
        blink = state == "idle" and self._blinking(t)
        if state == "asleep":
            mv(EYES, 90)
        else:
            mv(EYES, 85 if blink else REST[EYES])

        # Eye LEDs: dark asleep, lit awake — the puppet's "is anyone home" tell,
        # readable across a table in a way a 10° ear is not. The level EASES
        # toward its target (LED_FADE_UP_S / _DOWN_S) rather than snapping, so
        # waking looks like the eyes catching and dozing off like them cooling.
        # The blink dip rides on top and is instant: a 120 ms blink would not
        # survive a fade. An unrecognised state counts as awake — a body that
        # does not know a new state should still look alive.
        target = LED_LEVEL.get(state, LED_LEVEL["idle"])
        step = self.TICK / max(LED_FADE_UP_S if target > self._glow
                               else LED_FADE_DOWN_S, self.TICK)
        self._glow += max(-step, min(step, target - self._glow))
        drive_led(self.servos, EYES_LED,
                  self._glow * (LED_BLINK_DIP if blink else 1.0))

        # Tail: still asleep, slow sway idle, fast wag thinking, gentle when
        # listening/speaking.
        if state == "asleep":
            mv(TAIL, REST[TAIL])
        elif state == "thinking":
            mv(TAIL, REST[TAIL] + 55 * math.sin(t * 9))    # fast, wide wag
        elif state == "idle":
            mv(TAIL, REST[TAIL] + 25 * math.sin(t * 1.4))  # slow sway
        else:
            mv(TAIL, REST[TAIL] + 10 * math.sin(t * 3))

        # Jaw: a viseme level (if we get one) wins; else flap while speaking,
        # otherwise closed.
        if jaw_level is not None:
            mv(JAW, REST[JAW] + jaw_level * 50)
        elif state == "speaking":
            flap = 0.5 * (1 + math.sin(t * 14)) * random.uniform(0.7, 1.0)
            mv(JAW, REST[JAW] + flap * 30)
        else:
            mv(JAW, REST[JAW])

    def _blinking(self, t: float) -> bool:
        # A ~120 ms blink every 3–6 s.
        if t >= self._next_blink:
            self._next_blink = t + random.uniform(3, 6)
            self._blink_until = t + 0.12
        return t < self._blink_until

    def _ear_flick(self, t: float) -> int | None:
        # A ~200 ms one-ear flick every 2–4 s while listening (the first render
        # in the state usually flicks at once — a little "I heard you").
        if t >= self._next_flick:
            self._next_flick = t + random.uniform(2, 4)
            self._flick_until = t + 0.2
            self._flick_side = random.randint(0, 1)
        return self._flick_side if t < self._flick_until else None


# ── Pairing ─────────────────────────────────────────────────────────────
# A body is not a person: it cannot follow an invite link and type a name. So
# it is paired once, with a short code the DM mints in the browser, and from
# then on it holds a token of its own.
#
# One credential for a website and for an object on a shelf would be one
# credential too few: you could not revoke the cat without locking out the
# table, or rotate the table's password without a trip to the cat.

DEFAULT_DEVICE_FILE = Path.home() / ".config" / "familiar" / "device.json"


def device_file() -> Path:
    return Path(os.environ.get("FAMILIAR_DEVICE_FILE") or DEFAULT_DEVICE_FILE)


def load_device() -> dict:
    """What this body was told when it paired, or {} if it never has."""
    try:
        return json.loads(device_file().read_text())
    except (OSError, ValueError):
        return {}


def save_device(doc: dict) -> Path:
    """Write the token where only this user can read it.

    0600 and a 0700 directory: this file is the whole credential, and a Pi in a
    puppet is a machine other people are physically near.
    """
    path = device_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_text(json.dumps(doc, indent=2) + "\n")
    os.chmod(path, 0o600)
    return path


def ws_url_for(host: str, ws_path: str) -> str:
    """Turn the host you paired against into the bus URL to hold.

    https → wss and http → ws, so pairing against a real deployment cannot
    accidentally leave the body talking in the clear.
    """
    from urllib.parse import urlsplit
    parts = urlsplit(host if "://" in host else "https://" + host)
    scheme = "ws" if parts.scheme == "http" else "wss"
    return f"{scheme}://{parts.netloc}{ws_path}"


def pair(code: str, host: str) -> dict:
    """Swap a pairing code for this body's own token, and remember it.

    Deliberately one shot with a clear failure: a code is single-use and dies
    after a few minutes, so "it did not work" almost always means "mint another
    one", and retrying silently would just use up the DM's patience instead.
    """
    import urllib.error
    import urllib.request

    base = host if "://" in host else "https://" + host
    url = base.rstrip("/") + "/api/pair"
    request = urllib.request.Request(
        url,
        data=json.dumps({"code": code}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode()).get("error", "")
        except Exception:
            pass
        raise SystemExit(f"pairing failed ({exc.code}): {detail or exc.reason}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"could not reach {url}: {exc.reason}")

    doc = {
        "token": body["token"],
        "host": base.rstrip("/"),
        "url": ws_url_for(base, body["wsPath"]),
        "table": body["table"]["name"],
        "slug": body["table"]["slug"],
        "name": body["device"]["name"],
        "device_id": body["device"]["id"],
    }
    path = save_device(doc)
    print(f"paired as {doc['name']!r} with {doc['table']!r}")
    print(f"token saved to {path} (keep it; the code is now spent)")
    print(f"run it with:  python {Path(__file__).name} --real")
    return doc


# ── Bus client ──────────────────────────────────────────────────────────
def auth_headers(device: dict) -> dict:
    """The device token, as a bearer header.

    A browser on the bus sends its member cookie; a body sends this. Browsers
    cannot set headers on a WebSocket and a Pi has no cookie jar, so the two
    never collide.
    """
    token = device.get("token") or os.environ.get("FAMILIAR_DEVICE_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def normalize_url(url: str) -> str:
    """Tidy a user-supplied WS URL and fail loudly on the mistakes that would
    otherwise surface as h11's cryptic "Illegal target characters": a stray
    trailing space, or a missing path (the handshake needs a non-empty target,
    e.g. ``/t/<slug>/ws/state``)."""
    url = url.strip()
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if not parts.path:
        raise SystemExit(
            f"bad --url {url!r}: missing path — did you mean "
            f"{url}/t/<table>/ws/state ?")
    return url


def run(url: str, servos: Servos, device: dict | None = None) -> None:
    url = normalize_url(url)
    animator = Animator(servos)
    thread = threading.Thread(target=animator.run, daemon=True)
    thread.start()

    headers = auth_headers(device or {})
    backoff = 1.0
    try:
        while True:
            try:
                print(f"connecting to {url} ...")
                ws = simple_websocket.Client(url, headers=headers)
                print("connected — Whiskers' body is live")
                backoff = 1.0
                while True:
                    raw = ws.receive()  # blocks until a message or disconnect
                    if raw is None:
                        break
                    _handle(raw, animator)
            except (simple_websocket.ConnectionClosed,
                    simple_websocket.ConnectionError,
                    ConnectionError, OSError) as exc:
                # simple_websocket's ConnectionError subclasses RuntimeError,
                # NOT the builtin of the same name -- so listing it explicitly
                # is the whole difference between backing off and crashing.
                # It is raised when the *handshake* is refused (a 401 from a
                # proxy, a 404 at a path that has moved), which is exactly when
                # a body must keep trying rather than die and let systemd
                # restart it 22 times.
                status = getattr(exc, "status_code", None)
                hint = {
                    401: "  (the proxy refused it — a device sends Bearer and "
                         "cannot also do Basic; see deploy/Caddyfile-snippet)",
                    404: "  (no such path — is FAMILIAR_WS_URL overriding the "
                         "paired one? the bus is /t/<slug>/ws/state now)",
                }.get(status, "")
                print(f"disconnected ({exc}); retrying in {backoff:.0f}s{hint}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)  # self-heal across restarts/WiFi blips
    except KeyboardInterrupt:
        print("\nshutting down — relaxing servos, eyes out")
    finally:
        animator.stop()
        animator.relax()
        servos.close()


def run_demo(servos: Servos, hold_s: float = 4.0) -> None:
    """Cycle through the five states on a loop, no server needed — for bench-
    testing servos. Ctrl-C to stop."""
    animator = Animator(servos)
    thread = threading.Thread(target=animator.run, daemon=True)
    thread.start()
    states = ["asleep", "idle", "listening", "thinking", "speaking"]
    print(f"demo mode — cycling states every {hold_s:.0f}s (Ctrl-C to stop)")
    try:
        while True:
            for state in states:
                animator.set_state(state)
                time.sleep(hold_s)
    except KeyboardInterrupt:
        print("\nshutting down — relaxing servos, eyes out")
    finally:
        animator.stop()
        animator.relax()
        servos.close()


def run_calibrate(servos: Servos, name: str, hold_s: float = 2.0) -> None:
    """Walk ONE servo through 0° → 90° → 180° → REST on a loop so you can see
    where each pose physically lands — the practical way to find "where 0 is"
    and set the horn / the SERVO_PULSE_US range. Ctrl-C stops; servo relaxes."""
    by_name = {n: ch for ch, n in CHANNEL_NAMES.items()}
    channel = by_name.get(name.lower())
    if channel is None:
        raise SystemExit(
            f"--calibrate: unknown servo {name!r}; pick from {', '.join(by_name)}")
    lo, hi = SERVO_PULSE_US.get(channel, ("?", "?"))
    rest = rest_angles().get(channel, 90.0)
    steps = [
        (f"0°   (min_us={lo} — the 'zero' end)", 0),
        ("90°  (midpoint)", 90),
        (f"180° (max_us={hi} — the far end)", 180),
        (f"REST ({rest:.0f}° — neutral pose)", rest),
    ]
    print(f"calibrating {name} — channel {channel}, pulse {lo}-{hi}µs "
          f"(Ctrl-C to stop)")
    print("  Watch where each pose lands. To move 0, nudge min_us or reseat the")
    print("  horn; to change how far it turns, widen/narrow the range. Edit")
    print("  SERVO_PULSE_US (or REST), then re-run.\n")
    try:
        while True:
            for label, angle in steps:
                print(f"  → {label}")
                servos.move(channel, angle)
                time.sleep(hold_s)
    except KeyboardInterrupt:
        print("\nstopping — relaxing servo")
    finally:
        servos.close()


def run_eyes(servos: Servos) -> None:
    """Bench helper for mounting and adjusting the eyelids: toggle the eyes
    between OPEN and CLOSED with a keypress so you can see exactly where each
    pose lands and seat the horns/linkages. Both eye servos move together
    (mirrored if listed in INVERT), just as they do in the animation.

        open   = REST[EYES]  — the normal open-eyed pose
        closed = 90°         — the shut/asleep pose

    To adjust where the poses land: nudge REST[EYES] for 'open', or the
    eyes_l/eyes_r rows of SERVO_PULSE_US for where 0°/180° physically sit, then
    re-run. Press Enter to toggle; Ctrl-C stops and relaxes the servos."""
    open_angle = float(REST[EYES])
    closed_angle = 90.0
    channels = ", ".join(CHANNEL_NAMES[ch] for ch in PART_CHANNELS[EYES])
    print(f"eye helper — eyes drive {channels}")
    print(f"  open = {open_angle:.0f}°   closed = {closed_angle:.0f}°")
    print("  press Enter to toggle open/closed; Ctrl-C to stop\n")
    closed = False
    try:
        while True:
            angle = closed_angle if closed else open_angle
            drive_part(servos, EYES, angle)
            label = "CLOSED" if closed else "OPEN"
            nxt = "open" if closed else "closed"
            print(f"  eyes: {label:<6} ({angle:.0f}°)   [Enter → {nxt}]")
            input()
            closed = not closed
    except (KeyboardInterrupt, EOFError):
        print("\nstopping — relaxing servos")
    finally:
        servos.close()


def run_leds(servos: Servos) -> None:
    """Bench helper for the eye LEDs: one eye at a time, then a few brightness
    levels, on a loop. Ctrl-C stops and puts them out.

    What each step tells you:
      • one eye at a time — that both LEDs light, and which is on which channel.
        A dead one is usually in backwards (an LED only conducts one way) or
        has a resistor that is far too big.
      • 25 % / 50 %       — FLICKER. On the PCA9685 the LEDs share the servos'
        50 Hz frame, so a part-lit eye strobes at 50 Hz and can shimmer as the
        head moves. That is why LED_LEVEL keeps every awake state at 1.0, where
        the driver switches to the chip's full-on bit — steady, no chopping.
        (On --gpio the LEDs have their own 500 Hz, so these steps look smooth.)
      • both at full      — the brightness you will actually live with. Change
        the series resistor to trim it; LED_TRIM to balance one eye against the
        other.
    """
    left, right = LED_CHANNELS[EYES_LED]
    steps = [
        ("both off", 0.0, 0.0),
        (f"{LED_NAMES[left]} only, full", 1.0, 0.0),
        (f"{LED_NAMES[right]} only, full", 0.0, 1.0),
        ("both at 25%  ← flicker check", 0.25, 0.25),
        ("both at 50%  ← flicker check", 0.5, 0.5),
        ("both at full ← steady, no PWM", 1.0, 1.0),
    ]
    print(f"eye LEDs — {LED_NAMES[left]} ch{left}, {LED_NAMES[right]} ch{right} "
          "(Ctrl-C to stop)")
    print("  Wiring, per eye: PWM pin ──[180-330 Ω]──▶|── GND, red or amber.")
    print("  Both those pins are on that channel's own 3-pin header.\n")
    try:
        while True:
            for label, l_level, r_level in steps:
                print(f"  → {label}")
                servos.led(left, l_level * LED_TRIM.get(left, 1.0))
                servos.led(right, r_level * LED_TRIM.get(right, 1.0))
                time.sleep(1.5)
    except KeyboardInterrupt:
        print("\nstopping — eyes out")
    finally:
        servos.leds_off()
        servos.close()


def _handle(raw: str, animator: Animator) -> None:
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return
    kind = msg.get("type")
    if kind == "state" and isinstance(msg.get("state"), str):
        animator.set_state(msg["state"])
    elif kind == "viseme" and isinstance(msg.get("level"), (int, float)):
        animator.set_jaw_level(float(msg["level"]))
    # Unknown types are ignored on purpose — the protocol can grow without
    # breaking older bodies.


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(description="Whiskers' animatronic body client (mock by default).")
    p.add_argument("--pair", metavar="CODE",
                   help="claim a pairing code the DM minted (\"Running the table\" → "
                        "Bodies), store this body's own token, and exit")
    p.add_argument("--host", default=os.environ.get("FAMILIAR_HOST", ""),
                   help="where the brain lives, for --pair, e.g. "
                        "https://familiar.example (env: FAMILIAR_HOST)")
    p.add_argument("--forget", action="store_true",
                   help="delete the stored device token from this Pi")
    p.add_argument("--url", default="",
                   help="WebSocket URL of the brain's bus, overriding the paired one "
                        "— e.g. ws://127.0.0.1:8321/t/<slug>/ws/state")
    p.add_argument("--real", action="store_true",
                   help="drive real servos via PCA9685 instead of printing")
    p.add_argument("--gpio", metavar="MAP",
                   help="drive servos (and eye LEDs) straight from the Pi's GPIO via "
                        "pigpio, e.g. --gpio jaw=18,eye_l=23 (BCM pins) — the PCA9685 "
                        "stop-gap")
    p.add_argument("--demo", action="store_true",
                   help="cycle the five states on a loop instead of connecting to the "
                        "brain — for bench-testing servos without the web app")
    p.add_argument("--calibrate", metavar="SERVO",
                   help="drive ONE servo (jaw/ears_l/ears_r/eyes_l/eyes_r/tail) through "
                        "0/90/180°/REST on a loop to find where 0 lands and set its travel")
    p.add_argument("--eyes", action="store_true",
                   help="eyelid helper: toggle both eyes open/closed with Enter to mount "
                        "and adjust the eyelids (see where open vs closed lands)")
    p.add_argument("--leds", action="store_true",
                   help="eye-LED helper: cycle one eye / the other / a few brightness "
                        "levels, to check the wiring, the resistors and for flicker")
    p.add_argument("--verbose", action="store_true",
                   help="print every servo write (mock only; no throttling)")
    args = p.parse_args()

    # Pairing and forgetting touch no hardware and connect to no bus, so they
    # happen before a servo driver is ever constructed.
    if args.pair:
        if not args.host:
            raise SystemExit("--pair needs --host, e.g. --host https://familiar.example")
        pair(args.pair, args.host)
        return
    if args.forget:
        path = device_file()
        path.unlink(missing_ok=True)
        print(f"forgotten — {path} is gone. The table still lists this body until "
              "the DM forgets it there too.")
        return

    # Where to connect, most deliberate first: a --url typed on this command
    # line, then whatever pairing wrote down, then FAMILIAR_WS_URL, then a local
    # dev server.
    #
    # PAIRING BEATS THE ENVIRONMENT, and that ordering was learned the hard way.
    # It used to be the other way round -- FAMILIAR_WS_URL supplied the default
    # for --url, so it silently outranked everything. A puppet that had been
    # paired minutes earlier kept dialling a hostname left in a .env file from
    # the previous deployment, and said nothing about why. An environment
    # variable is ambient and easy to forget; pairing is an act somebody
    # performed on purpose, with a code, this week. The recent deliberate thing
    # wins, and if the stale one disagrees it is *said out loud* rather than
    # quietly obeyed.
    device = load_device()
    env_url = os.environ.get("FAMILIAR_WS_URL", "").strip()
    url = args.url or device.get("url") or env_url
    if env_url and device.get("url") and env_url != device["url"]:
        print(f"note: ignoring FAMILIAR_WS_URL={env_url!r} — this body is paired "
              f"with {device.get('table')!r} at {device['url']}.\n"
              "      Delete it from .env or the unit's EnvironmentFile if it is "
              "a leftover.")
    # No fallback URL: the bus lives at /t/<slug>/ws/state now, so a tableless
    # default cannot exist — the old ws://127.0.0.1:8321/ws/state would 404 and
    # reconnect-loop forever, which reads as a broken puppet, not a config error.
    if not url and not (args.demo or args.calibrate or args.eyes or args.leds):
        raise SystemExit(
            "not paired yet and nowhere to connect. Either pair this body — the "
            "DM mints a code under \"Running the table\" → Bodies, then\n"
            "    python body_client.py --pair <CODE> --host https://your.server\n"
            "— or pass the bus directly for a local server, e.g.\n"
            "    --url ws://127.0.0.1:8321/t/<slug>/ws/state")

    if args.real:
        servos: Servos = Pca9685Servos()
    elif args.gpio:
        servos = GpioServos(parse_pin_map(args.gpio))
    else:
        servos = MockServos(verbose=args.verbose)

    if args.calibrate:
        run_calibrate(servos, args.calibrate)
    elif args.eyes:
        run_eyes(servos)
    elif args.leds:
        run_leds(servos)
    elif args.demo:
        run_demo(servos)
    else:
        run(url, servos, device)


if __name__ == "__main__":
    main()
