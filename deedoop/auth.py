"""Accounts, sessions and sign-in throttling (milestone 6.5).

Signed out, deedoop is read-only: every request that is not a GET needs a
signed-in admin (rule SC1 in docs/secure-coding.md, enforced in
``web/security.py``). This module holds the parts that do not depend on the
web framework, so the command line (``deedoop reset-password``) can use them.

- Passwords are hashed with Argon2id (RFC 9106 low-memory profile).
- A session is a random 32-byte token in a cookie. Only its SHA-256 is
  stored, so a copy of the database cannot be used to sign in.
- Failed sign-ins are throttled per address (lockout that doubles) and per
  account (one try a minute), so nobody can lock the admin out for good.
- The first admin is created with a one-time setup code printed in the
  container log, or from ``DEEDOOP_ADMIN_PASSWORD`` on first start.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.profiles import RFC_9106_LOW_MEMORY

from .db import Tx

log = logging.getLogger("deedoop.auth")

HASHER = PasswordHasher.from_parameters(RFC_9106_LOW_MEMORY)

COOKIE = "deedoop_session"
MIN_PASSWORD = 12
MAX_PASSWORD = 1024  # Argon2 hashes any length; this only bounds the work per request
SESSION_IDLE = 8 * 3600
SESSION_MAX = 7 * 24 * 3600
CONFIRM_WINDOW = 10 * 60  # dangerous actions ask for the password again after this long

IP_WINDOW = 15 * 60
IP_MAX_FAILURES = 5
IP_LOCK_FIRST = 15 * 60
IP_LOCK_MAX = 24 * 3600
ACCOUNT_MAX_FAILURES = 20  # from any address in IP_WINDOW; then one try a minute
ACCOUNT_SLOW = 60

SETUP_CODE_TTL = 3600
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I

# The most common passwords and keyboard walks of 12+ characters. Not a full
# breach list (that would be megabytes); enough to refuse the obvious ones.
COMMON_PASSWORDS = frozenset("""
123456789012 1234567890123 12345678901234 123456789123 111111111111 000000000000 123123123123
qwertyuiopas qwertyuiop12 qwertyuiop123 1q2w3e4r5t6y 1qaz2wsx3edc qazwsxedcrfv asdfghjkl123
passwordpassword password1234 password12345 password123456 passw0rd1234 p@ssw0rd1234 Password1234
password123! Password123! iloveyou1234 letmein12345 welcome12345 changeme1234 administrator
admin1234567 adminadmin12 adminpassword unraid123456 unraidunraid deedoopdeedoop deedoop123456 deedupdeedup deedup123456
abcdefghijkl abcd12345678 aaaaaaaaaaaa monkey123456 football1234 baseball1234 superman1234
trustno1trustno1 sunshine1234 princess1234 dragon123456 master123456 shadow123456
""".lower().split())


class AuthError(Exception):
    """Shown to the user as is."""


@dataclass
class Session:
    token_hash: bytes
    user_id: int
    username: str
    csrf: str
    created_at: float
    last_seen: float
    auth_at: float

    def recently_confirmed(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.auth_at <= CONFIRM_WINDOW


# -- passwords ----------------------------------------------------------------


def check_new_password(password: str, username: str = "") -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Use at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise AuthError(f"Use at most {MAX_PASSWORD} characters.")
    low = password.lower()
    if low in COMMON_PASSWORDS or len(set(password)) < 4:
        raise AuthError("That password is too easy to guess. Try a few unrelated words.")
    if username and username.lower() in low:
        raise AuthError("The password must not contain the user name.")


def hash_password(password: str) -> str:
    return HASHER.hash(password)


def verify_password(pw_hash: str, password: str) -> bool:
    if len(password) > MAX_PASSWORD:
        return False
    try:
        return HASHER.verify(pw_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# A hash to verify against when the user does not exist, so that a wrong user
# name takes as long as a wrong password.
_DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(16))


# -- accounts -----------------------------------------------------------------


def has_admin(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def create_admin(conn: sqlite3.Connection, username: str, password: str) -> int:
    username = username.strip()
    if not username or len(username) > 64 or not username.isprintable():
        raise AuthError("Pick a user name of 1 to 64 characters.")
    check_new_password(password, username)
    now = time.time()
    with Tx(conn):
        if has_admin(conn):
            raise AuthError("An admin account already exists.")
        cur = conn.execute("INSERT INTO users(username, pw_hash, created_at, pw_changed_at) VALUES (?, ?, ?, ?)",
                           (username, hash_password(password), now, now))
    return int(cur.lastrowid)


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    """Change the password and end every session of that user."""
    row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise AuthError("No such user.")
    check_new_password(password, row["username"])
    with Tx(conn):
        conn.execute("UPDATE users SET pw_hash = ?, pw_changed_at = ? WHERE id = ?",
                     (hash_password(password), time.time(), user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def the_admin(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()


def rename_admin(conn: sqlite3.Connection, user_id: int, username: str) -> None:
    username = username.strip()
    if not username or len(username) > 64 or not username.isprintable():
        raise AuthError("Pick a user name of 1 to 64 characters.")
    conn.execute("UPDATE users SET username = ? WHERE id = ?", (username, user_id))


# -- sign-in with throttling ------------------------------------------------------


def lockout_remaining(conn: sqlite3.Connection, ip: str, now: float | None = None) -> float:
    now = now or time.time()
    row = conn.execute("SELECT until FROM lockouts WHERE ip = ?", (ip,)).fetchone()
    return max(0.0, row["until"] - now) if row else 0.0


def _account_wait(conn: sqlite3.Connection, username: str, now: float) -> float:
    fails = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE username = ? COLLATE NOCASE AND ok != 1 AND at > ?",
                         (username, now - IP_WINDOW)).fetchone()[0]
    if fails < ACCOUNT_MAX_FAILURES:
        return 0.0
    last = conn.execute("SELECT MAX(at) FROM login_attempts WHERE username = ? COLLATE NOCASE",
                        (username,)).fetchone()[0] or 0
    return max(0.0, last + ACCOUNT_SLOW - now)


def authenticate(conn: sqlite3.Connection, username: str, password: str, ip: str) -> int:
    """Return the user id, or raise :class:`AuthError` with a message that does
    not reveal whether the user name exists."""
    now = time.time()
    username = username.strip()[:64]
    wait = lockout_remaining(conn, ip, now)
    if wait:
        raise AuthError(f"Too many failed sign-ins from this address. Try again in {_fmt_wait(wait)}.")
    wait = _account_wait(conn, username, now)
    if wait:
        raise AuthError(f"Too many failed sign-ins for this account. Try again in {_fmt_wait(wait)}.")
    row = conn.execute("SELECT id, pw_hash FROM users WHERE username = ?", (username,)).fetchone()
    ok = verify_password(row["pw_hash"] if row else _DUMMY_HASH, password) and row is not None
    with Tx(conn):
        conn.execute("INSERT INTO login_attempts(at, ip, username, ok) VALUES (?, ?, ?, ?)",
                     (now, ip, username, int(ok)))
        conn.execute("DELETE FROM login_attempts WHERE at < ?", (now - 30 * 86400,))
        if ok:
            conn.execute("UPDATE lockouts SET until = 0 WHERE ip = ?", (ip,))
        else:
            fails = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE ip = ? AND ok = 0 AND at > ? "
                                 "AND at > COALESCE((SELECT MAX(at) FROM login_attempts WHERE ip = ? AND ok = 1), 0)",
                                 (ip, now - IP_WINDOW, ip)).fetchone()[0]
            if fails >= IP_MAX_FAILURES:
                prev = conn.execute("SELECT strikes FROM lockouts WHERE ip = ?", (ip,)).fetchone()
                strikes = (prev["strikes"] if prev else 0) + 1
                lock = min(IP_LOCK_FIRST * 2 ** (strikes - 1), IP_LOCK_MAX)
                conn.execute("INSERT INTO lockouts(ip, until, strikes) VALUES (?, ?, ?) "
                             "ON CONFLICT(ip) DO UPDATE SET until = excluded.until, strikes = excluded.strikes",
                             (ip, now + lock, strikes))
                # Start counting again after the lockout.
                conn.execute("UPDATE login_attempts SET ok = -1 WHERE ip = ? AND ok = 0", (ip,))
                event(conn, "locked", username, ip, f"{fails} failed sign-ins; locked for {_fmt_wait(lock)}",
                      in_tx=True)
    if not ok:
        event(conn, "signin_failed", username, ip)
        raise AuthError("Wrong user name or password.")
    if HASHER.check_needs_rehash(row["pw_hash"]):
        conn.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_password(password), row["id"]))
    return int(row["id"])


def confirm_password(conn: sqlite3.Connection, sess: Session, password: str, ip: str) -> None:
    """Re-check the password for a dangerous action; throttled like a sign-in."""
    authenticate(conn, sess.username, password, ip)
    conn.execute("UPDATE sessions SET auth_at = ? WHERE token_hash = ?", (time.time(), sess.token_hash))


def _fmt_wait(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    return f"{m} minute{'s' if m != 1 else ''}" if m < 120 else f"{round(m / 60)} hours"


# -- sessions -----------------------------------------------------------------


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def new_session(conn: sqlite3.Connection, user_id: int, ip: str, user_agent: str) -> str:
    """Create a session; return the cookie value (shown once, never stored)."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with Tx(conn):
        conn.execute("DELETE FROM sessions WHERE last_seen < ? OR created_at < ?",
                     (now - SESSION_IDLE, now - SESSION_MAX))
        conn.execute("INSERT INTO sessions(token_hash, user_id, csrf, created_at, last_seen, auth_at, ip, user_agent) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (_digest(token), user_id, secrets.token_urlsafe(32), now, now, now, ip, user_agent[:200]))
    return token


def load_session(conn: sqlite3.Connection, token: str | None) -> Session | None:
    if not token or len(token) > 100:
        return None
    now = time.time()
    row = conn.execute("SELECT s.*, u.username FROM sessions s JOIN users u ON u.id = s.user_id "
                       "WHERE s.token_hash = ?", (_digest(token),)).fetchone()
    if row is None:
        return None
    if row["last_seen"] < now - SESSION_IDLE or row["created_at"] < now - SESSION_MAX:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
        return None
    if now - row["last_seen"] > 60:  # one write a minute is plenty
        conn.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, row["token_hash"]))
    return Session(row["token_hash"], row["user_id"], row["username"], row["csrf"], row["created_at"],
                   row["last_seen"], row["auth_at"])


def end_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_digest(token),))


def end_all_sessions(conn: sqlite3.Connection, user_id: int | None = None) -> int:
    if user_id is None:
        return conn.execute("DELETE FROM sessions").rowcount
    return conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,)).rowcount


# -- audit trail ----------------------------------------------------------------


def event(conn: sqlite3.Connection, kind: str, username: str = "", ip: str = "", detail: str = "",
          in_tx: bool = False) -> None:
    """Add a line to the security log (shown in Setup). Never pass secrets."""
    conn.execute("INSERT INTO security_log(at, event, username, ip, detail) VALUES (?, ?, ?, ?, ?)",
                 (time.time(), kind, username[:64], ip[:64], detail[:500]))
    if not in_tx:
        conn.execute("DELETE FROM security_log WHERE id <= (SELECT MAX(id) FROM security_log) - 5000")


def recent_events(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM security_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


# -- first run ------------------------------------------------------------------


class SetupCode:
    """The one-time code that proves the person creating the admin can read the
    container log. A new code is made on every start and every hour until an
    admin exists, and each one is printed to the log."""

    def __init__(self, ttl: float = SETUP_CODE_TTL):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._code = ""
        self._made = 0.0

    def current(self) -> str:
        with self._lock:
            if not self._code or time.time() - self._made > self.ttl:
                raw = "".join(secrets.choice(_ALPHABET) for _ in range(16))
                self._code = "-".join(raw[i:i + 4] for i in range(0, 16, 4))
                self._made = time.time()
                log.warning("deedoop has no admin account yet. To create it, open the web UI and enter this "
                            "setup code: %s (valid for %d minutes; restart deedoop for a new one)",
                            self._code, self.ttl // 60)
            return self._code

    def check(self, code: str) -> bool:
        want = self.current()
        got = code.strip().upper().replace(" ", "")
        return secrets.compare_digest(got.replace("-", ""), want.replace("-", ""))

    def burn(self) -> None:
        with self._lock:
            self._code = ""


def bootstrap(conn: sqlite3.Connection, admin_password: str | None, setup: SetupCode) -> None:
    """At start: create the admin from the environment, or announce the setup code."""
    if has_admin(conn):
        return
    if admin_password:
        try:
            create_admin(conn, "admin", admin_password)
            event(conn, "admin_created", "admin", "", "from DEEDOOP_ADMIN_PASSWORD")
            log.warning("Created the admin account (user name: admin) from the environment. You can remove "
                        "DEEDOOP_ADMIN_PASSWORD / DEEDOOP_PASSWORD from the container settings now.")
            return
        except AuthError as e:
            log.error("DEEDOOP_ADMIN_PASSWORD was not used: %s Create the admin in the web UI instead.", e)
    setup.current()
