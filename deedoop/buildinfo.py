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


REPO_URL = "https://github.com/SteveRodrigue/deedoop"


def build_info(env: dict[str, str] | None = None) -> dict[str, str]:
    """The version, plus the short commit and build date when the image has valid ones."""
    env = os.environ if env is None else env
    commit = env.get("DEEDOOP_COMMIT", "").strip().lower()
    date = env.get("DEEDOOP_BUILD_DATE", "").strip()
    return {"version": __version__,
            "commit": commit[:7] if _COMMIT.fullmatch(commit) else "",
            "date": date if _DATE.fullmatch(date) else ""}


def build_label(env: dict[str, str] | None = None) -> str:
    """``0.10.0`` alone, or ``0.10.0 (1ae420b, 2026-11-28)`` in a built image."""
    info = build_info(env)
    parts = [p for p in (info["commit"], info["date"]) if p]
    return f"{__version__} ({', '.join(parts)})" if parts else __version__
