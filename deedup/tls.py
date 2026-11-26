"""Built-in HTTPS (milestone 7.5).

Off unless ``DEEDUP_HTTPS=on``. When on, deedup serves HTTPS on its usual
port with, in order of preference:

1. **Your certificate**: ``cert.pem`` (the certificate, followed by any
   intermediates) and ``key.pem`` in ``/config/tls``, or the files named by
   ``DEEDUP_TLS_CERT`` and ``DEEDUP_TLS_KEY``.
2. **A self-signed certificate** that deedup makes on first start and keeps in
   ``/config/tls/self-signed.*``. Browsers warn once per device; the Setup
   screen shows its SHA-256 fingerprint so you can check it is this server.

A certificate that cannot be loaded never stops deedup from starting: it falls
back to the self-signed one and Setup says why. For access from outside your
home, a reverse proxy with a real certificate (SWAG, Nginx Proxy Manager) is
still the better choice; see SECURITY.md.

The TLS settings are Python's server defaults (TLS 1.2 and 1.3 only) with
forward-secret AEAD ciphers for TLS 1.2 (SC8).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import os
import re
import secrets
import socket
import ssl
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import AppConfig

log = logging.getLogger("deedup.tls")

SELF_SIGNED_DAYS = 397      # about 13 months
RENEW_DAYS = 30             # make a new self-signed certificate this close to expiry
# TLS 1.2 suites: ECDHE or DHE key exchange, AES-GCM or ChaCha20 only. TLS 1.3 uses its own (all strong).
CIPHERS = "ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20:!aNULL:!eNULL:!MD5:!DSS:!SHA1"
_DNS = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))*$")


@dataclass
class TlsInfo:
    enabled: bool = False
    source: str = ""                    # "yours" or "self-signed"
    cert_path: Path | None = None
    key_path: Path | None = None
    fingerprint: str = ""               # SHA-256, colon-separated hex
    not_after: dt.datetime | None = None
    names: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def days_left(self) -> int | None:
        if self.not_after is None:
            return None
        return (self.not_after - dt.datetime.now(dt.UTC)).days


def server_context(cert: Path, key: Path) -> ssl.SSLContext:
    """The context uvicorn will use: loading it is the test that the pair works."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers(CIPHERS)
    ctx.load_cert_chain(cert, key)
    return ctx


def wanted_names(extra: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """Names for the self-signed certificate, and the ones that were refused."""
    names: list[str] = []
    bad: list[str] = []
    for n in ["localhost", "127.0.0.1", "::1", socket.gethostname(), *extra]:
        n = n.strip().lower().rstrip(".")
        if not n:
            continue
        try:
            n = str(ipaddress.ip_address(n))
        except ValueError:
            if not _DNS.match(n.removeprefix("*.")):
                bad.append(n[:80])
                continue
        if n not in names:
            names.append(n)
    return names, bad


def _load(cert: Path):
    from cryptography import x509

    return x509.load_pem_x509_certificate(cert.read_bytes())


def describe(cert: Path) -> tuple[str, dt.datetime, list[str]]:
    """Fingerprint, expiry and names of a PEM certificate (the first in the file)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    c = _load(cert)
    fp = c.fingerprint(hashes.SHA256()).hex().upper()
    try:
        san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [str(v) for v in san.get_values_for_type(x509.DNSName)] + \
                [str(v) for v in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = [a.value for a in c.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)]
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)), c.not_valid_after_utc, [str(n) for n in names]


def _write_private(path: Path, data: bytes) -> None:
    """Write a file only its owner can read, atomically (SC8: the key never
    exists with looser permissions, not even for a moment)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # noqa: TID251 - SC2 covers /mnt; this is deedup's own /config/tls
    except BaseException:
        try:
            os.unlink(tmp)  # noqa: TID251 - our own temp file in /config/tls, never /mnt
        except OSError:
            pass
        raise


def make_self_signed(cert: Path, key: Path, names: list[str], days: int = SELF_SIGNED_DAYS) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    pk = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "deedup self-signed"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "deedup")])
    sans: list[x509.GeneralName] = []
    for n in names:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            sans.append(x509.DNSName(n))
    now = dt.datetime.now(dt.UTC)
    c = (x509.CertificateBuilder()
         .subject_name(name).issuer_name(name)
         .public_key(pk.public_key())
         .serial_number(int.from_bytes(secrets.token_bytes(16), "big") >> 1)
         .not_valid_before(now - dt.timedelta(days=1))
         .not_valid_after(now + dt.timedelta(days=days))
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         .add_extension(x509.SubjectAlternativeName(sans), critical=False)
         .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, content_commitment=False,
                                      data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                      crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
         .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
         .add_extension(x509.SubjectKeyIdentifier.from_public_key(pk.public_key()), critical=False)
         .sign(pk, hashes.SHA256()))
    cert.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_private(key, pk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    _write_private(cert, c.public_bytes(serialization.Encoding.PEM))


def _usable(cert: Path, key: Path) -> str | None:
    """None when the pair loads, otherwise why not."""
    if not cert.is_file() and not key.is_file():
        return "missing"
    if not cert.is_file():
        return f"{cert.name} is missing (found {key.name})"
    if not key.is_file():
        return f"{key.name} is missing (found {cert.name})"
    try:
        server_context(cert, key)
    except (ssl.SSLError, OSError, ValueError) as e:
        return f"cannot be used: {getattr(e, 'reason', None) or e}"[:200]
    return None


def prepare(cfg: AppConfig) -> TlsInfo:
    """Pick (and if needed make) the certificate to serve. Never raises for a
    bad certificate of yours: it falls back to the self-signed one."""
    if not cfg.https:
        return TlsInfo()
    info = TlsInfo(enabled=True)
    why = _usable(cfg.tls_cert, cfg.tls_key)
    if why is None:
        info.source, info.cert_path, info.key_path = "yours", cfg.tls_cert, cfg.tls_key
        try:
            if cfg.tls_key.stat().st_mode & 0o077:
                info.problems.append(f"{cfg.tls_key.name} can be read by other users of the server. "
                                     f"Run chmod 600 on it.")
        except OSError:
            pass
    else:
        if why != "missing":
            info.problems.append(f"Your certificate {why}, so deedup uses its self-signed one.")
        tls_dir = cfg.config_dir / "tls"
        cert, key = tls_dir / "self-signed.crt", tls_dir / "self-signed.key"
        names, bad = wanted_names(cfg.tls_names)
        if bad:
            info.problems.append("Not valid host names or addresses, left out: " + ", ".join(bad))
        if not _self_signed_ok(cert, key, names):
            log.info("making a self-signed certificate for %s", ", ".join(names))
            make_self_signed(cert, key, names)
        info.source, info.cert_path, info.key_path = "self-signed", cert, key
    info.fingerprint, info.not_after, info.names = describe(info.cert_path)
    if info.days_left is not None and info.days_left < 0:
        info.problems.append("Your certificate has expired. Browsers will refuse it until you replace it.")
    elif info.source == "yours" and info.days_left is not None and info.days_left < RENEW_DAYS:
        info.problems.append(f"Your certificate expires in {info.days_left} days.")
    return info


def _self_signed_ok(cert: Path, key: Path, names: list[str]) -> bool:
    if _usable(cert, key) is not None:
        return False
    try:
        _, not_after, have = describe(cert)
    except (ValueError, OSError):
        return False
    left = not_after - dt.datetime.now(dt.UTC)
    return left > dt.timedelta(days=RENEW_DAYS) and set(names) <= set(have)
