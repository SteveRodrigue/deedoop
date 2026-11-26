"""Process-level configuration, read from environment variables.

Everything the user changes at runtime (shares, exclusions, disk mode,
schedule, who may see what) lives in the database ``settings`` table
instead; see ``db.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Inside the container deedoop must listen on every interface so that Docker's
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
    ``DEEDOOP_ADMIN_PASSWORD``, or the old ``DEEDOOP_PASSWORD`` from before M6.5."""
    legacy_password_set: bool = False
    """``DEEDOOP_PASSWORD`` (basic auth before M6.5) is still set; the UI asks to remove it."""
    trusted_proxies: tuple[str, ...] = ()
    """Addresses or networks of reverse proxies whose X-Forwarded-For/-Proto headers are believed."""
    https: bool = False
    """Serve HTTPS on the web UI port (``DEEDOOP_HTTPS=on``); see ``tls.py``."""
    tls_cert: Path = Path("/config/tls/cert.pem")
    tls_key: Path = Path("/config/tls/key.pem")
    tls_names: tuple[str, ...] = ()
    """Extra host names and addresses for the self-signed certificate (``DEEDOOP_TLS_NAMES``)."""

    @property
    def db_path(self) -> Path:
        return self.config_dir / "deedoop.db"


ON = {"1", "on", "true", "yes"}


def env(name: str, default: str | None = None) -> str | None:
    """``DEEDOOP_<name>`` from the environment."""
    return os.environ.get("DEEDOOP_" + name, default)


def load() -> AppConfig:
    config_dir = Path(env("CONFIG", "/config"))
    legacy = env("PASSWORD") or None
    proxies = tuple(p.strip() for p in (env("TRUSTED_PROXIES") or "").split(",") if p.strip())
    return AppConfig(
        config_dir=config_dir,
        mnt=Path(env("MNT", "/mnt")),
        host=env("HOST", ALL_INTERFACES),
        port=int(env("PORT", "8080")),
        admin_password=env("ADMIN_PASSWORD") or legacy,
        legacy_password_set=legacy is not None,
        trusted_proxies=proxies,
        https=(env("HTTPS") or "").strip().lower() in ON,
        tls_cert=Path(env("TLS_CERT") or config_dir / "tls" / "cert.pem"),
        tls_key=Path(env("TLS_KEY") or config_dir / "tls" / "key.pem"),
        tls_names=tuple(n.strip() for n in (env("TLS_NAMES") or "").split(",") if n.strip()),
    )
