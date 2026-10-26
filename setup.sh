#!/usr/bin/env bash
# One-time setup for running a familiar on this machine (Linux or macOS).
#
#   ./setup.sh
#
# Makes .venv, installs the dependencies, downloads one Piper voice, and writes
# .env with a fresh SECRET_KEY and your Gemini key. Safe to run again: it keeps
# an existing .env and skips a voice that is already downloaded.
#
# Windows: install WSL (https://learn.microsoft.com/windows/wsl/install), open
# an Ubuntu terminal, clone the repo there and run this script. Then open the
# printed address in Chrome on Windows -- WSL forwards localhost.
set -euo pipefail
cd "$(dirname "$0")"

VOICE="${PIPER_VOICE:-en_GB-alan-medium}"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Python 3 is needed (3.10 or newer). Install it and run this again." >&2
    exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    echo "Python 3.10 or newer is needed; $("$PYTHON" --version) found." >&2
    exit 1
fi

echo "==> Creating .venv and installing dependencies"
if [ ! -x .venv/bin/python ]; then
    "$PYTHON" -m venv .venv || {
        echo "Could not create a venv. On Debian/Ubuntu: sudo apt install python3-venv" >&2
        exit 1
    }
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "==> Downloading the Piper voice $VOICE"
mkdir -p voices
if [ -f "voices/$VOICE.onnx" ]; then
    echo "    already there"
else
    .venv/bin/python -m piper.download_voices --data-dir voices "$VOICE"
fi

if [ -f .env ]; then
    echo "==> Keeping your existing .env"
else
    echo "==> Writing .env"
    secret="$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')"
    echo
    echo "    The familiar's brain is Google Gemini, which has a free tier."
    echo "    Get a key at https://aistudio.google.com/apikey and paste it here"
    echo "    (or press Enter to add it to .env later)."
    read -r -p "    GEMINI_API_KEY: " gemini_key || gemini_key=""
    sed -e "s|^SECRET_KEY=.*|SECRET_KEY=$secret|" \
        -e "s|^GEMINI_API_KEY=.*|GEMINI_API_KEY=$gemini_key|" \
        -e "s|^PIPER_VOICE=.*|PIPER_VOICE=$VOICE|" \
        .env.example > .env
    chmod 600 .env
fi

cat <<EOF

All set. Start the familiar with:

    .venv/bin/python server.py

then open http://localhost:8321 in Chrome. The first account you create owns
this install.
EOF
