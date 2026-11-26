"""Process-level configuration, read from environment variables.

Everything the user changes at runtime (shares, exclusions, disk mode,
schedule, who may see what) lives in the database ``settings`` table
instead; see ``db.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Inside the container deedup must listen on every interface so that Docker's
# port mapping reaches it; the Unraid template decides what is exposed.
ALL_INTERFACES = "0.0.0.0"  # noqa: S104


@dataclass(frozen=True)
class AppConfig:
    config_dir: Path
    """Writable directory for the index database (``/config`` in Docker)."""
    mnt: Path
    """Where the host's ``/mnt`` is mounted, read-only (``/mnt`` in Docker)."""
    host: str = ALL_INTERFACES
    port: int = 8080
    admin_password: str | None = field(default=None, repr=False)
    """First run only: creates the admin account with this password. Ignored once an admin exists.
    ``DEEDUP_ADMIN_PASSWORD``, or the old ``DEEDUP_PASSWORD`` from before M6.5."""
    legacy_password_set: bool = False
    """``DEEDUP_PASSWORD`` (basic auth before M6.5) is still set; the UI asks to remove it."""
    trusted_proxies: tuple[str, ...] = ()
    """Addresses or networks of reverse proxies whose X-Forwarded-For/-Proto headers are believed."""
    https: bool = False
    """Serve HTTPS on the web UI port (``DEEDUP_HTTPS=on``); see ``tls.py``."""
    tls_cert: Path = Path("/config/tls/cert.pem")
    tls_key: Path = Path("/config/tls/key.pem")
    tls_names: tuple[str, ...] = ()
    """Extra host names and addresses for the self-signed certificate (``DEEDUP_TLS_NAMES``)."""

    @property
    def db_path(self) -> Path:
        return self.config_dir / "deedup.db"


ON = {"1", "on", "true", "yes"}


def load() -> AppConfig:
    config_dir = Path(os.environ.get("DEEDUP_CONFIG", "/config"))
    legacy = os.environ.get("DEEDUP_PASSWORD") or None
    proxies = tuple(p.strip() for p in os.environ.get("DEEDUP_TRUSTED_PROXIES", "").split(",") if p.strip())
    return AppConfig(
        config_dir=config_dir,
        mnt=Path(os.environ.get("DEEDUP_MNT", "/mnt")),
        host=os.environ.get("DEEDUP_HOST", ALL_INTERFACES),
        port=int(os.environ.get("DEEDUP_PORT", "8080")),
        admin_password=os.environ.get("DEEDUP_ADMIN_PASSWORD") or legacy,
        legacy_password_set=legacy is not None,
        trusted_proxies=proxies,
        https=os.environ.get("DEEDUP_HTTPS", "").strip().lower() in ON,
        tls_cert=Path(os.environ.get("DEEDUP_TLS_CERT") or config_dir / "tls" / "cert.pem"),
        tls_key=Path(os.environ.get("DEEDUP_TLS_KEY") or config_dir / "tls" / "key.pem"),
        tls_names=tuple(n.strip() for n in os.environ.get("DEEDUP_TLS_NAMES", "").split(",") if n.strip()),
    )
