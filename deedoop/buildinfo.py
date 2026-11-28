"""Which build is running: the version plus the commit and date baked into the image.

The image build passes DEEDOOP_COMMIT and DEEDOOP_BUILD_DATE (see Dockerfile and
scripts/ci.sh). Values that don't look like a short commit id or an ISO date are
ignored, so the footer never shows arbitrary environment text.
"""

import os
import re

from . import __version__

_COMMIT = re.compile(r"[0-9a-f]{7,40}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def build_label(env: dict[str, str] | None = None) -> str:
    """``0.10.0`` alone, or ``0.10.0 (1ae420b, 2026-11-28)`` in a built image."""
    env = os.environ if env is None else env
    commit = env.get("DEEDOOP_COMMIT", "").strip().lower()
    date = env.get("DEEDOOP_BUILD_DATE", "").strip()
    parts = []
    if _COMMIT.fullmatch(commit):
        parts.append(commit[:7])
    if _DATE.fullmatch(date):
        parts.append(date)
    return f"{__version__} ({', '.join(parts)})" if parts else __version__
