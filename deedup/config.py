"""Process-level configuration, read from environment variables.

Everything the user changes at runtime (shares, exclusions, disk mode,
schedule) lives in the database ``settings`` table instead; see ``db.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    config_dir: Path
    """Writable directory for the index database (``/config`` in Docker)."""
    mnt: Path
    """Where the host's ``/mnt`` is mounted, read-only (``/mnt`` in Docker)."""
    host: str = "0.0.0.0"
    port: int = 8080
    password: str | None = None
    """Optional HTTP basic-auth password for the web UI (user name ``deedup``)."""

    @property
    def db_path(self) -> Path:
        return self.config_dir / "deedup.db"


def load() -> AppConfig:
    return AppConfig(
        config_dir=Path(os.environ.get("DEEDUP_CONFIG", "/config")),
        mnt=Path(os.environ.get("DEEDUP_MNT", "/mnt")),
        host=os.environ.get("DEEDUP_HOST", "0.0.0.0"),
        port=int(os.environ.get("DEEDUP_PORT", "8080")),
        password=os.environ.get("DEEDUP_PASSWORD") or None,
    )
