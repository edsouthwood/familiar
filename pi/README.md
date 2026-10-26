# The puppet — Pi Zero setup

`body_client.py` is the puppet side of the familiar. It holds a WebSocket to
the brain server's `/ws/state`, receives the five states (asleep, idle,
listening, thinking, speaking), and drives the cat's servos — and the LEDs
behind its eyes — to match. It's just another body on the bus — the on-screen
cat is the other, and both stay in lockstep.

By default it's a **mock**: it prints every servo move instead of driving
hardware, so you can prove the whole chain (a screen wakes → the brain
broadcasts → the puppet reacts) before wiring a single servo. Going real is a
one-method change (see [Going real](#going-real)).

You only need this one file on the Pi — not the rest of the repo (that's the
brain server, with heavy deps like `piper-tts` you don't want on a Zero).

## What you need

Roughly £45–70 / $50–80 all in, most of it the Pi.

| Part | Why | Rough cost |
|---|---|---|
| Raspberry Pi Zero 2 W (or any Pi) on WiFi | runs this one file; **Raspberry Pi OS Lite** is plenty, there is no GUI | £15–20 |
| microSD card (8 GB+) and a USB power supply | | £8–12 |
| PCA9685 16-channel servo board | one I2C board drives all six servos and keeps their timing off the Pi's CPU | £4–6 |
| 6× SG90 micro servos | jaw ×1, ears ×2, eyelids ×2, tail ×1 — the ears and eyes are *pairs*, one per side of the head | £10–15 |
| 2× 5 mm LEDs (**red or amber**) + 2× 180–330 Ω resistors | the eyes light up when it is awake; they run off two spare channels of the same PCA9685 (see [Eyes that light up](#eyes-that-light-up)) | £1 |
| 5–6 V 3 A supply for the servos | **their own**, not the Pi's 5 V pin (see [Power](#power)) | £8–12 |
| Dupont jumpers, and a cat | the cat is the hard part | — |

Six servos, four moving parts: the ears move as a pair and so do the eyelids,
so the animation drives four *logical* parts and each fans out to its one or
two channels. See `CHANNEL_NAMES` and the parts table at the top of
`body_client.py`. The two eye LEDs are a fifth logical part, fanning out the
same way (`LED_CHANNELS`).

You can start with none of the hardware — the client is a mock until you pass
`--real` — and with two servos straight off the GPIO before the PCA9685
arrives (see [A couple of servos straight off the GPIO](#a-couple-of-servos-straight-off-the-gpio-no-pca9685-yet)).

## Install

Raspberry Pi OS ships pip pre-configured with **piwheels** (precompiled ARM
wheels), which turns slow source builds into quick downloads — important on a
Zero. Install into a venv:

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip

# copy just this file (or git clone and ignore the server bits)
mkdir -p ~/familiar-body && cd ~/familiar-body
scp you@laptop:path/to/familiar/pi/body_client.py .   # or however you like

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install simple-websocket        # the only dep for the mock
```

## Pair it with a table (once)

A body is not a person: it cannot follow an invite link and type a name. So it
is paired instead, with a short code that the DM mints in the browser.

1. **In the browser**, on the table's page → *Running the table* → **Bodies** →
   **Pair a puppet**. A code appears (`WXYZ-2345`), along with the exact line to
   run. It works **once**, and only for about fifteen minutes.
2. **On the Pi**:

   ```bash
   .venv/bin/python body_client.py --pair WXYZ-2345 --host https://familiar.example.com
   ```

   With the server on a laptop (tier 2), point it at the laptop on the same
   wifi instead: `--host http://<laptop-ip>:8321`. Plain http is fine here —
   only a browser's microphone needs HTTPS.

That swaps the code for this body's own long-lived token and writes it to
`~/.config/familiar/device.json` (mode 0600), along with which table it belongs
to and the bus URL to hold. Nothing else needs configuring: from now on

```bash
.venv/bin/python body_client.py
```

connects to the right table on its own.

Why a code rather than a password: the token in that file lives inside a
physical object in somebody's house. It is *this cat's* credential, so the DM
can revoke it (**Forget**, in the same panel) without locking out the table,
and can rotate the table's own passwords without a trip to the cat.

A paired body **listens but does not drive**: it receives every state and
viseme, and anything it sends is ignored. If the cat is ever picked up and
taken, the worst the token buys is watching a jaw move.

`--forget` on the Pi deletes the stored token; **Forget** in the browser kills
it server-side. Do the browser one if the device is gone — that is the half
that stops it connecting.

## Run (mock — do this first)

Now wake the familiar in a browser — the Pi prints its servo poses following
each state. That confirms the network path, TLS and pairing all work before any
hardware exists.

```bash
.venv/bin/python body_client.py          # paired: no arguments needed
```

Against a dev server on your laptop, unpaired, pass the bus URL directly — note
it carries the table's slug:

```bash
.venv/bin/python body_client.py --url ws://127.0.0.1:8321/t/<slug>/ws/state
```

| flag / env | purpose |
|------------|---------|
| `--pair CODE --host URL` | claim a code the DM minted, store this body's token, exit |
| `--forget` | delete the stored token from this Pi |
| `--url` | the bus URL, overriding the paired one — `ws://host/t/<slug>/ws/state` |
| `FAMILIAR_WS_URL` | same, but **lower priority than pairing** — a paired body ignores it and says so |
| `FAMILIAR_DEVICE_FILE` | where the token lives (default `~/.config/familiar/device.json`) |
| `FAMILIAR_DEVICE_TOKEN` | the token from the environment instead of a file |
| `--verbose` | print every servo write (mock only; no throttling) |
| `--real` | drive real servos via the PCA9685 instead of printing |
| `--gpio MAP` | drive servos **and eye LEDs** straight from GPIO, e.g. `--gpio jaw=18,eye_l=23` (the PCA9685 stop-gap) |
| `--demo` | cycle the five states on a loop, no server — bench-test servos on their own |
| `--calibrate SERVO` | drive one servo (`jaw`/`ears_l`/`ears_r`/`eyes_l`/`eyes_r`/`tail`) through 0/90/180°/REST to find where 0 lands and set its travel |
| `--eyes` | eyelid helper: toggle **both** eyes open/closed with Enter, to mount and adjust the eyelids (see [Fitting the eyelids](#fitting-the-eyelids)) |
| `--leds` | eye-LED helper: cycle one eye / the other / a few brightness levels, to check wiring, resistors and flicker (see [Eyes that light up](#eyes-that-light-up)) |

The client reconnects on its own (backoff up to 30 s), so it self-heals across
server restarts and WiFi blips.

## A couple of servos straight off the GPIO (no PCA9685 yet)

Before the PCA9685 arrives you can drive one or two servos directly from the
Pi's GPIO with [pigpio](https://abyz.me.uk/rpi/pigpio/) (DMA-timed PWM — smooth,
unlike jittery software PWM). Only the channels you name are wired; the rest of
the animation still runs, it just doesn't move anything.

```bash
sudo apt install -y pigpio
sudo systemctl enable --now pigpiod     # the daemon pigpio talks to
.venv/bin/pip install pigpio

# Wiring (BCM numbering), per servo:
#   signal → the GPIO pin      (e.g. jaw → GPIO18 / pin 12, tail → GPIO17 / pin 11)
#   V+     → your 5 V supply    (NOT the Pi's 5V pin)
#   GND    → common ground shared by the servo supply AND a Pi GND pin
```

Bench-test the two servos on their own, no web app needed:

```bash
.venv/bin/python body_client.py --gpio jaw=18,tail=17 --demo
```

The eye LEDs work here too — name them like any other channel and they get
ordinary PWM at 500 Hz instead of servo pulses (LED to the GPIO pin through
its resistor, then to a Pi GND):

```bash
.venv/bin/python body_client.py --gpio jaw=18,eye_l=23,eye_r=24 --demo
```

It cycles asleep → idle → listening → thinking → speaking every few seconds:
the jaw servo flaps on *speaking*, the tail sways on *idle* and wags on
*thinking*. Ctrl-C stops it and lets the servos go slack. Once that looks right,
point it at the live brain and drive it by talking to Whiskers:

```bash
.venv/bin/python body_client.py --gpio jaw=18,tail=17     # paired: that's all
```

If a servo hums at the extremes or strains, narrow the pulse range in
`GpioServos` (`min_us`/`max_us`, default 500–2500 µs) to match your servo.

## Going real

1. Wire the PCA9685:

   ```
   Pi 3V3  → PCA9685 VCC      Pi GND → PCA9685 GND
   Pi SDA  → PCA9685 SDA      Pi SCL → PCA9685 SCL
   5–6 V servo supply → PCA9685 V+   (its OWN supply — NOT the Pi's 5 V pin)

   servos ch0–5 (jaw, ears_l, ears_r, eyes_l, eyes_r, tail)
   LEDs   ch6–7 (eye_l, eye_r) — see "Eyes that light up" below
   ```

   The servos can pull more current than the Pi can give; share the ground
   between the servo supply and the Pi, but power the servos from V+. See
   [Power](#power) for the supply itself.

2. Enable I2C and install the driver:

   ```bash
   sudo raspi-config nonint do_i2c 0            # enable I2C
   .venv/bin/pip install adafruit-circuitpython-servokit
   sudo apt install -y i2c-tools
   i2cdetect -y 1                               # PCA9685 should show at 0x40
   ```

3. Calibrate each servo (see [below](#where-0-is-and-how-far-a-servo-turns))
   so 0° and the travel land where you want, then run with `--real`:

   ```bash
   .venv/bin/python body_client.py --real
   ```

   The `Pca9685Servos` class already does the hardware writes — it maps each
   channel's 0–180° onto its own pulse range from `SERVO_PULSE_US`.

### Where 0 is, and how far a servo turns

A servo is positioned by the *width* of the pulse it's fed: a short pulse
drives it to one end, a long pulse to the other. `body_client.py` maps each
servo's `0–180°` command linearly onto its own `(min_us, max_us)` pulse range
in the **`SERVO_PULSE_US`** table at the top of the file:

```
angle   0° → min_us   ← where 0 is: one physical end-stop
angle 180° → max_us   ← the other end
```

- **To move where 0 sits:** power up and drive the servo to 0° *before* fitting
  the horn (`--calibrate jaw`), then press the horn on at the pose you want. The
  horn gives the coarse offset (splines are ~18° apart); nudge `min_us` to
  fine-tune.
- **To change how far it turns:** widen the `min_us…max_us` span for more
  travel, narrow it for less. Pull the ends in (e.g. `700–2300`) if a servo
  buzzes, strains, or slams a linkage at the extremes. `500–2500 µs` is a
  typical SG90 near its full ~180°.

`--calibrate <servo>` walks one servo through 0° → 90° → 180° → REST on a loop
so you can watch where each lands, adjust `SERVO_PULSE_US` (and `REST`), and
re-run:

```bash
.venv/bin/python body_client.py --real --calibrate jaw
```

### Fitting the eyelids

The eyes are a **pair** (`eyes_l` + `eyes_r`) that open and close together, so
`--calibrate` (one servo at a time) is awkward for them. `--eyes` is a dedicated
helper: it drives **both** eye servos at once and toggles them between OPEN and
CLOSED each time you press **Enter**, so you can seat the horns and linkages and
watch exactly where each pose lands. Ctrl-C stops and relaxes the servos.

```bash
.venv/bin/python body_client.py --gpio eyes_l=27,eyes_r=22 --eyes   # or --real
```

- **open** = `REST[EYES]` (the normal open-eyed pose); **closed** = `0°` (the
  shut/asleep pose).
- To adjust the poses: nudge `REST[EYES]` for *open*, or the `eyes_l`/`eyes_r`
  rows of `SERVO_PULSE_US` for where `0°`/`180°` physically sit, then re-run.
- If one eyelid swings the wrong way, add its channel to `INVERT` so the pair
  moves together.

Like every real mode, `--eyes` needs a backend flag (`--gpio …` or `--real`) —
without one it mocks and nothing moves.

### Eyes that light up

An LED behind each eye is the puppet's *is anyone home* tell — readable across
a table in a way that a 10° ear flick is not. **Dark asleep, lit awake**, easing
between the two so waking looks like the eyes catching rather than a switch
being thrown.

No extra board: the PCA9685 is a sixteen-channel PWM chip and only six channels
are servos, so the LEDs take **channels 6 and 7** (`eye_l`, `eye_r`).

**Wiring, per eye** — both pins are on that channel's own 3-pin header:

```
   PCA9685 ch6 PWM ──[ 180–330 Ω ]──▶|── GND        (long leg to the resistor)
```

The output *sources* the current at logic level, which is **3.3 V** (VCC comes
from the Pi's 3V3). Two consequences worth knowing before you buy LEDs:

- **Use red or amber.** At 3.3 V there is no headroom for white or blue
  (Vf ≈ 3.1 V) — they come out dim or not at all. Red/amber is the right colour
  for a familiar anyway.
- A channel can source **10 mA**, which is plenty for an indicator LED. 220 Ω
  gives ≈ 6 mA; drop to 180 Ω for a brighter eye, raise to 330 Ω for a subtler
  one. **Set brightness with the resistor, not in software** — see flicker
  below.

Want white eyes, or more punch? Wire the other way round instead — `V+ ──[R]──▶|──
PWM pin` — so the output *sinks* (25 mA allowed, and 5 V across the LED), and
add that channel to **`LED_INVERT`**, because then the LED lights when the
output is low. The trade-off: the chip comes out of reset with its outputs low,
so sink-wired eyes blaze for the moment before the client starts (it darkens
them as soon as it opens the board).

**Check the wiring** before trusting it to the animation:

```bash
.venv/bin/python body_client.py --real --leds     # or --gpio eye_l=23,eye_r=24
```

That loops through left eye only → right eye only → 25 % → 50 % → full. A dead
eye is nearly always in backwards (an LED conducts one way) or behind far too
big a resistor.

**About flicker.** The LEDs share the servos' 50 Hz frame — one chip, one
frequency — so any brightness *between* off and full is chopped at 50 Hz and
can shimmer as the head moves. At full brightness the driver uses the chip's
full-on bit instead: a steady output, no chopping. That is why every awake
state in **`LED_LEVEL`** sits at `1.0` by default, and why the eyes get their
character from *transitions* — the fade, the blink dip — rather than from a dim
steady glow. The `--leds` 25 % / 50 % steps are there so you can judge for
yourself whether it bothers you. If you want a dim steady eye, fit a bigger
resistor. (Driving the LEDs from GPIO instead — `--gpio eye_l=23,eye_r=24` —
gives them their own 500 Hz and no flicker at any level.)

The knobs, all at the top of `body_client.py`:

| | |
|---|---|
| `LED_NAMES` | which PCA9685 channel each eye is on |
| `LED_LEVEL` | brightness per state — `asleep` is `0.0`, the rest `1.0` (see flicker) |
| `LED_FADE_UP_S` / `LED_FADE_DOWN_S` | how long the kindle and the fade take (0.8 s / 1.6 s) |
| `LED_BLINK_DIP` | how far the eyes drop during a blink — instant, not faded |
| `LED_TRIM` | per-eye brightness balance, if one LED is hotter than the other |
| `LED_INVERT` | channels wired to sink rather than source (see above) |

The eyes go out on shutdown (`Ctrl-C`, or systemd stopping the service), so a
stopped puppet looks asleep rather than dead.

### Power

**Recommended: a 5 V 3 A (15 W) DC supply for the servos**, separate from the
Pi's own power.

Why those numbers, for 4× SG90:

| state | current per servo | 4 servos |
|-------|-------------------|----------|
| idle | ~5 mA | ~20 mA |
| moving (light load) | ~100–250 mA | ~0.4–1 A |
| stall (worst case) | ~650–750 mA | ~2.8 A |

The servos rarely stall and their moves are small and staggered, so 3 A gives
comfortable headroom over the realistic peak without brownout jitter or Pi
resets. 5 V sits safely inside the SG90's 4.8–6 V range and runs cool. (6 V is
also in spec and gives a little more torque, but hotter — only if a joint feels
weak. Adding more servos later → step up to 5 V 5 A.)

Connecting and de-gremlining it:

- The PCA9685's servo power is a **2-pin screw terminal (V+ / GND)**. Most
  adapters end in a barrel plug, so add a cheap **5.5×2.1 mm barrel-jack →
  screw-terminal pigtail** and wire it into V+/GND.
- **Fit a capacitor** across V+/GND — a 470–1000 µF electrolytic (the Adafruit
  board has a footprint for it). It absorbs the inrush when servos kick and
  stops the voltage dips that cause jitter.
- **Common ground, separate rail:** tie the servo supply's ground to the Pi's
  ground, but power the servos from V+ — never off the Pi's 5 V pin. The Zero
  can't source the current, and the spikes will crash it.

A generic "5 V 3 A barrel-jack adapter" is fine; for something that lasts, a
Mean Well **GST18A05** (5 V 3 A) or **RS-15-5** is a solid pick.

### Tuning the movements

All the geometry lives in one place at the top of `body_client.py`:

- `JAW / EARS / EYES / TAIL` — the logical parts and the channel(s) each drives
  (`ears` = `ears_l`+`ears_r`, `eyes` = `eyes_l`+`eyes_r`; see `PART_CHANNELS`).
- `SERVO_PULSE_US` — each servo's pulse range, i.e. where 0° sits and how far
  it turns (see [Where 0 is](#where-0-is-and-how-far-a-servo-turns)).
- `REST` — the relaxed neutral angle for each.
- `LED_*` — the eye LEDs: channels, per-state brightness, fade times, blink dip
  (see [Eyes that light up](#eyes-that-light-up)).
- The per-state sweep ranges inside `Animator._render` (how far the tail
  wags, how wide the jaw flaps, the blink cadence).

Re-measure these against your real linkages — servo horns and mechanical
travel vary, and the defaults are just sensible starting points. Use
`--calibrate <servo>` to find the endpoints and `--verbose` (mock) to see
every angle written.

## Start on boot (systemd)

There are no credentials to put in the unit file any more — pairing already
wrote the token into the service user's home, mode 0600. The unit just has to
run **as the user that paired it**, so that `~/.config/familiar/device.json` is
the one it finds.

Create `/etc/systemd/system/familiar-body.service` (adjust `User` and the paths
to where you put things):

```ini
[Unit]
Description=The familiar's animatronic body
After=network-online.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/familiar-body
ExecStart=/home/pi/familiar-body/.venv/bin/python body_client.py --real
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

(Drop `--real` until the hardware's wired.) Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now familiar-body
journalctl -u familiar-body -f          # watch it connect and react
```

## If it will not connect

The client retries forever with backoff, so the log is the place to look:
`journalctl -u familiar-body -f`.

- **"not paired yet"** — there is no `~/.config/familiar/device.json`. Either it
  was never paired, or systemd is running it as a different user from the one
  that paired it (`User=` in the unit).
- **It connects and immediately disconnects** — the token was revoked
  (**Forget** in the browser) or the table was deleted. Mint a new code and
  pair again.
- **"pairing failed (404)"** — a code is single-use and expires in about
  fifteen minutes. Mint another; it is free.
- **"pairing failed (401)"** — that is the *reverse proxy* refusing, not the
  familiar. A device authenticates with `Authorization: Bearer <token>`, and
  there is only one such header, so it cannot also satisfy an HTTP basic-auth
  gate in front of the site. Two paths have to skip that gate —
  `/api/pair` and `/t/*/ws/state` — and neither is left unguarded by doing so:
  one takes a single-use code, the other a device token. See
  `deploy/Caddyfile-snippet` for the matcher.
- **It connects but never moves** — nothing is driving the bus. Wake the
  familiar in a browser at that table; the puppet mirrors whatever the screen
  is doing. If the log *is* showing state changes and it still does not move,
  see the next one.
- **The log looks perfect and nothing moves** — check the servo supply is
  actually switched on. The PCA9685's logic runs off the Pi's 3V3, so with V+
  dead the board still enumerates, still prints `PCA9685 ready`, still accepts
  every write and still logs every state change. Nothing in the software path
  can see that rail: the only tell is a cat that does not move. (Sink-wired eye
  LEDs hang off V+ too, so they go dark with it — one reason to run them from
  the Pi's 5 V pin instead.)

The token is a plain file: `cat ~/.config/familiar/device.json` shows which
table this body thinks it belongs to.
