#!/usr/bin/env python3
"""Synthesise the cat sound pack (static/sounds/cat/*.wav).

The familiar plays a short creature noise when it starts thinking and when it
finishes answering. Recorded meows are nicer, but their licences rarely allow
redistribution, so the shipped pack is generated here: a harmonic tone whose
pitch rises and falls ("mi-AOW") through a vowel-like filter that opens from
"ee" to "ah" to "oo". Standard library only, deterministic, MIT like the rest.

Drop your own .mp3/.wav/.ogg clips into static/sounds/<pack>/ to replace them.

    python3 tools/synth_meows.py
"""

import math
import random
import struct
import wave
from pathlib import Path

RATE = 22050
OUT = Path(__file__).resolve().parent.parent / "static" / "sounds" / "cat"

# Each meow: (seconds, start Hz, peak Hz, end Hz, where the peak falls 0..1).
MEOWS = {
    "meow-1": (0.55, 520, 760, 430, 0.35),
    "meow-2": (0.40, 640, 880, 600, 0.30),   # short, bright
    "meow-3": (0.75, 460, 700, 360, 0.45),   # long, plaintive
    "meow-4": (0.30, 700, 820, 690, 0.50),   # a chirrup
    "meow-5": (0.60, 560, 900, 480, 0.25),
}

# Formants (Hz) for the mouth opening: "ee" -> "ah" -> "oo".
VOWELS = [(300, 2300), (750, 1200), (400, 850)]


def _lerp(a, b, t):
    return a + (b - a) * t


def _contour(t, start, peak, end, at):
    """Pitch at time fraction t: a smooth rise to the peak, then a fall."""
    if t < at:
        return _lerp(start, peak, math.sin(t / at * math.pi / 2))
    return _lerp(peak, end, 1 - math.cos((t - at) / (1 - at) * math.pi / 2))


def _formants(t):
    if t < 0.5:
        a, b, u = VOWELS[0], VOWELS[1], t / 0.5
    else:
        a, b, u = VOWELS[1], VOWELS[2], (t - 0.5) / 0.5
    return _lerp(a[0], b[0], u), _lerp(a[1], b[1], u)


def _gain(freq, formant, width):
    """A soft resonance: harmonics near a formant come through louder."""
    return 1 / (1 + ((freq - formant) / width) ** 2)


def meow(seconds, start, peak, end, at, seed):
    rng = random.Random(seed)
    n = int(seconds * RATE)
    phase = 0.0
    samples = []
    for i in range(n):
        t = i / n
        f0 = _contour(t, start, peak, end, at) * (1 + 0.012 * math.sin(i / RATE * 2 * math.pi * 6))
        phase += 2 * math.pi * f0 / RATE
        f1, f2 = _formants(t)
        s = 0.0
        for h in range(1, 12):
            fh = f0 * h
            if fh > RATE / 2:
                break
            s += math.sin(phase * h) / h * (_gain(fh, f1, 160) + 0.7 * _gain(fh, f2, 260))
        s += rng.uniform(-1, 1) * 0.04  # breath
        # Fast attack, a body, then a tail that trails off.
        env = min(1.0, t / 0.06) * (1 - t) ** 0.8 * (0.85 + 0.15 * math.sin(t * math.pi))
        samples.append(s * env)
    peak_amp = max(abs(x) for x in samples) or 1
    return [x / peak_amp * 0.7 for x in samples]


def write(path, samples):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(x * 32767)) for x in samples))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for seed, (name, params) in enumerate(MEOWS.items()):
        write(OUT / f"{name}.wav", meow(*params, seed=seed))
        print(f"wrote {OUT / name}.wav")


if __name__ == "__main__":
    main()
