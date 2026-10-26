"""Guards for constants that are duplicated across the Python/JS boundary.

AMBIENT_WORD_THRESHOLD exists in two places: the server decides when a buffer
is big enough to fold in, and the browser decides when to POST.

/api/config now serves this value, so the server is the source of truth at
runtime and the copies below are fallbacks used only when that fetch fails.
They should still agree with the server, or a familiar running on a degraded
connection buffers to a different size than the server expects.
"""

import re
from pathlib import Path

import server

REPO = Path(__file__).resolve().parent.parent


def _grep_int(path: Path, pattern: str) -> int:
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"{pattern!r} no longer matches anything in {path.name} -- update this test"
    return int(match.group(1))


def test_browser_ambient_threshold_matches_server():
    browser = _grep_int(REPO / "static" / "familiar.js", r"ambientWordThreshold:\s*(\d+)")
    assert browser == server.AMBIENT_WORD_THRESHOLD
