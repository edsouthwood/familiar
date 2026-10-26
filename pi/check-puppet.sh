#!/usr/bin/env bash
# Why is the puppet not moving?
#
# Checks both ends in one go: what the brain knows about the device (paired?
# ever connected?) and what the Pi is actually doing. Everything here is
# READ-ONLY -- it starts nothing, stops nothing and changes no files.
#
#   ./pi/check-puppet.sh                  # asks for the Pi login
#   ./pi/check-puppet.sh ed@192.168.1.64  # or tell it up front
#
# Run it from the repo root on the machine running the brain.

set -u
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$BASE" || exit 1

PI="${1:-}"
if [ -z "$PI" ]; then
    read -rp "Pi login [pi@192.168.1.64]: " PI
    PI="${PI:-pi@192.168.1.64}"
fi

HOST="$(grep -oP '^[a-z0-9.-]+(?=\s*\{)' /etc/caddy/Caddyfile 2>/dev/null | grep familiar | head -1)"
HOST="${HOST:-familiar.example.com}"

echo "════ 1. what the brain knows ═══════════════════════════════════════"
.venv/bin/python - <<'PY'
import sqlite3, os
from pathlib import Path
db = Path(os.environ.get("DATA_DIR") or "data") / "app.db"
try:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
except sqlite3.Error as exc:
    raise SystemExit(f"  cannot open {db}: {exc}")
rows = c.execute("select name, token_hash is not null, pair_expires, paired_at,"
                 " last_seen_at from devices").fetchall()
if not rows:
    print("  no devices at all — nothing has been paired. Mint a code in the"
          "\n  browser: the table's page -> Running the table -> Bodies.")
for name, paired, expires, paired_at, seen in rows:
    if not paired:
        print(f"  {name!r}: code minted, NOT yet claimed (expires {expires})")
        print("     -> the Pi has never successfully run --pair")
    elif not seen:
        print(f"  {name!r}: paired {paired_at}, but has NEVER connected")
        print("     -> pairing worked; the problem is on the Pi (sections 3-4)")
    else:
        print(f"  {name!r}: paired {paired_at}, last seen {seen}")
        print("     -> the bus is working. If it is not moving, nothing is")
        print("        driving it: wake the familiar in a browser.")
PY

echo
echo "════ 2. can a device reach the brain from outside? ═════════════════"
pair=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST \
    "https://$HOST/api/pair" -H 'Content-Type: application/json' -d '{"code":"x"}')
front=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$HOST/")
echo "  POST https://$HOST/api/pair -> $pair"
case "$pair" in
  404) echo "     OK: the app answered (404 = 'that is not a real code')." ;;
  401) echo "     BLOCKED: the reverse proxy refused before the app saw it."
       echo "     A device sends Authorization: Bearer and cannot also do Basic."
       echo "     /etc/caddy/Caddyfile needs the @human matcher — see"
       echo "     deploy/Caddyfile-snippet — then: sudo systemctl reload caddy" ;;
  000) echo "     UNREACHABLE: DNS, TLS or the box is down." ;;
  *)   echo "     unexpected — expected 404" ;;
esac
echo "  GET  https://$HOST/           -> $front  (401 = humans still gated, as intended)"

echo
echo "════ 3. the Pi: is the service even running? ═══════════════════════"
ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new "$PI" 'bash -s' <<'REMOTE'
echo "  logged in as $(whoami) on $(hostname)"
echo
echo "  -- systemd --"
if systemctl list-unit-files familiar-body.service >/dev/null 2>&1; then
    systemctl is-active familiar-body || true
    systemctl status familiar-body --no-pager -n 0 2>&1 | sed -n '1,6p' | sed 's/^/  /'
else
    echo "  no familiar-body.service installed (are you running it by hand?)"
fi
echo
echo "  -- the unit's own settings --"
grep -nE 'User=|EnvironmentFile=|ExecStart=|WorkingDirectory=' \
     /etc/systemd/system/familiar-body.service 2>/dev/null | sed 's/^/  /' \
     || echo "  (no unit file)"
echo
echo "  -- traps --"
if [ -f /etc/familiar-body.env ]; then
    echo "  /etc/familiar-body.env EXISTS:"
    sed 's/^/    /' /etc/familiar-body.env
    grep -q FAMILIAR_WS_URL /etc/familiar-body.env && \
      echo "    !! FAMILIAR_WS_URL OVERRIDES the paired URL. Delete this file and" && \
      echo "       the EnvironmentFile= line, then daemon-reload and restart."
else
    echo "  no /etc/familiar-body.env  (good — unless the unit still names it,"
    echo "  in which case systemd refuses to start the service at all)"
fi
echo
echo "  -- where is the token, and who can read it? --"
for home in /root /home/*; do
    f="$home/.config/familiar/device.json"
    [ -e "$f" ] && ls -l "$f" | sed 's/^/  /'
done
echo "  (it must be in the home of the user in User= above)"
echo
echo "  -- last 25 log lines --"
journalctl -u familiar-body -n 25 --no-pager 2>/dev/null | sed 's/^/  /' \
    || echo "  (no journal for familiar-body)"
REMOTE

echo
echo "════ 4. if all else is fine, run it in the foreground ══════════════"
echo "  ssh $PI"
echo "  cd ~/familiar-body && .venv/bin/python body_client.py"
echo
echo "  Good:  connecting to wss://$HOST/t/<slug>/ws/state ..."
echo "         connected — Whiskers' body is live"
echo "  Bad:   'not paired yet'          -> wrong user, token is elsewhere"
echo "         a URL without /t/<slug>/  -> stale FAMILIAR_WS_URL"
