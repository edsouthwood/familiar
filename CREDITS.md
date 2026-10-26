# Credits

Everything in this repository is covered by the MIT licence in `LICENSE`,
including:

- **The creature drawings** (`static/forms/*.svg`) and **the familiar shells**
  (`static/shells/*/`): original work by the author.
- **The cat sound pack** (`static/sounds/cat/*.wav`): synthesised by
  `tools/synth_meows.py`; no recordings. Run it again to regenerate them, or
  drop your own `.mp3`/`.wav`/`.ogg` clips into `static/sounds/<pack>/`. If you
  add recordings, check their licence allows redistribution before you share
  your fork (freesound.org lets you filter by CC0).

## Not included: voices

No voice models ship with this repository. `setup.sh` downloads one Piper
voice from the [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices)
catalogue onto your machine, and you can fetch others the same way. Each voice
has its own model card and licence; read it before using a voice for anything
beyond your own table. For reference:

| Voice | Dataset licence (from its model card) |
|---|---|
| `en_GB-alan-medium` (setup.sh default) | Mycroft AI mimic3 `apope` voice — see that repository |
| `en_GB-cori-high` | LibriVox recordings, public domain; trained from scratch |
| `en_GB-aru-medium` | CC BY 4.0; fine-tuned from `en_US-lessac` |
| `en_GB-northern_english_male-medium` | CC BY-SA 4.0; fine-tuned from `en_US-lessac` |

Voices fine-tuned from `en_US-lessac` also inherit the Blizzard 2013 Lessac
dataset licence, which restricts commercial use.

## Software

- [Flask](https://flask.palletsprojects.com/), [flask-sock](https://github.com/miguelgrinberg/flask-sock),
  [gunicorn](https://gunicorn.org/), [Requests](https://requests.readthedocs.io/)
- [Piper](https://github.com/OHF-Voice/piper1-gpl) text-to-speech (installed as a dependency, GPL-3.0)
- Optional: [pocket-tts](https://github.com/kyutai-labs/pocket-tts) by Kyutai
