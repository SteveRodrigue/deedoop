"""Milestone 6.5: sign-in, read-only mode, CSRF and the rules in docs/secure-coding.md.

Each test names the rule (SC1-SC10) or the audit finding (A1-A12) it guards.
"""

import hashlib
import re
import time
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from deedup import auth, db
from deedup.web.app import create_app
from deedup.web.security import safe_next
from tests.conftest import TEST_PASSWORD, ensure_admin, sign_in
from tests.test_web import make_client

TEMPLATES = Path(__file__).parent.parent / "deedup" / "web" / "templates"
# Tables a GET may write: the session's "last seen", the audit trail, sign-in throttling.
VOLATILE = {"sessions", "security_log", "login_attempts", "lockouts"}


def _routes(app, methods):
    for r in app.routes:
        if isinstance(r, APIRoute) and r.methods & methods:
            yield r


def _url(route) -> str:
    return re.sub(r"\{[^}]+\}", "1", route.path).replace(".{fmt}", ".csv")


def _db_fingerprint(cfg) -> str:
    c = db.connect(cfg.db_path)
    try:
        h = hashlib.sha256()
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
            if name in VOLATILE:
                continue
            for row in c.execute(f"SELECT * FROM {name}"):  # noqa: S608 - table names from sqlite_master
                h.update(repr(tuple(row)).encode())
        return h.hexdigest()
    finally:
        c.close()


def _set(cfg, **values):
    c = db.connect(cfg.db_path)
    try:
        db.set_settings(c, values)
    finally:
        c.close()


# -- SC1: signed out means read-only -------------------------------------------------


def test_every_non_get_route_needs_sign_in(unraid):
    """A3 / SC1: walk every route; signed out, none of them changes anything."""
    _set(unraid, public_view=True)
    with make_client(unraid, signed_in=False) as client:
        before = _db_fingerprint(unraid)
        routes = list(_routes(client.app, {"POST", "PUT", "PATCH", "DELETE"}))
        assert len(routes) > 20
        for r in routes:
            if r.path in ("/login", "/first-run"):
                continue
            for method in r.methods - {"HEAD", "GET"}:
                resp = client.request(method, _url(r), data={"confirm": "delete", "mode": "delete"},
                                      headers={"accept": "text/html"}, follow_redirects=False)
                assert resp.status_code == 303 and resp.headers["location"].startswith("/login"), (method, r.path)
                resp = client.request(method, _url(r), headers={"accept": "application/json"}, follow_redirects=False)
                assert resp.status_code == 401, (method, r.path)
        assert _db_fingerprint(unraid) == before


def test_no_get_route_writes_to_the_index(unraid):
    """SC1: GET never changes the index (sessions and the audit trail aside)."""
    with make_client(unraid) as client:
        client.post("/setup/shares", data={"share": ["Photos"]})
        before = _db_fingerprint(unraid)
        for r in _routes(client.app, {"GET"}):
            client.get(_url(r), follow_redirects=False)
        assert _db_fingerprint(unraid) == before


def test_signed_out_view_switch(unraid):
    with make_client(unraid, signed_in=False) as client:
        # Off (the default): only the status page and sign-in.
        r = client.get("/duplicates", headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login?next=/duplicates"
        assert client.get("/api/status", headers={"accept": "application/json"}).status_code == 401
        r = client.get("/login")
        assert r.status_code == 200 and "Indexed files" in r.text and "Sign in" in r.text
        assert 'href="/duplicates"' not in r.text  # no navigation to pages it cannot see
        assert client.get("/healthz").status_code == 200
        assert client.get("/static/style.css").status_code == 200

        _set(unraid, public_view=True)
        r = client.get("/duplicates")
        assert r.status_code == 200 and "You are not signed in, so deedup is read-only" in r.text
        assert "data-readonly" in r.text and "csrf_token" not in r.text


def test_signed_in_admin_can_change_settings(unraid):
    with make_client(unraid) as client:
        r = client.post("/setup/access", data={"public_view": "1"})
        assert r.status_code == 200
        c = db.connect(unraid.db_path)
        assert db.get_settings(c)["public_view"] is True
        events = [r["event"] for r in auth.recent_events(c)]
        c.close()
        assert "signin" in events and "change" in events


# -- A1: CSRF -------------------------------------------------------------------------


def test_csrf_token_is_required(unraid):
    with make_client(unraid) as client:
        before = _db_fingerprint(unraid)
        token = client.headers.pop("X-CSRF-Token")
        r = client.post("/setup/changes", data={"allow_changes": "1"})
        assert r.status_code == 403
        r = client.post("/setup/changes", data={"allow_changes": "1", "csrf_token": "x" * 43})
        assert r.status_code == 403
        assert _db_fingerprint(unraid) == before
        r = client.post("/setup/access", data={"public_view": "1", "csrf_token": token})
        assert r.status_code == 200  # the hidden form field works too


def test_forms_from_other_sites_are_refused(unraid):
    with make_client(unraid) as client:
        before = _db_fingerprint(unraid)
        for origin in ("http://evil.example", "null"):
            r = client.post("/setup/access", data={"public_view": "1"}, headers={"origin": origin})
            assert r.status_code == 403
        r = client.post("/setup/access", data={"public_view": "1"},
                        headers={"referer": "http://evil.example/page"})
        assert r.status_code == 403
        assert _db_fingerprint(unraid) == before
        r = client.post("/setup/access", data={"public_view": "1"}, headers={"origin": "http://testserver"})
        assert r.status_code == 200


def test_every_post_form_carries_the_csrf_token():
    for f in TEMPLATES.glob("*.html"):
        text = f.read_text()
        for m in re.finditer(r"<form\b[^>]*>", text):
            if 'method="post"' in m.group(0):
                assert text[m.end():m.end() + 40].startswith('{% include "_csrf.html" %}'), (f.name, m.group(0))


# -- SC6: templates ---------------------------------------------------------------------


def test_templates_escape_and_have_no_inline_script():
    for f in TEMPLATES.glob("*.html"):
        text = f.read_text()
        assert "|safe" not in text.replace(" ", "") and "Markup" not in text, f.name
        assert "autoescape false" not in text, f.name
        assert not re.search(r"\son[a-z]+\s*=", text), f"{f.name}: inline event handler"
        assert "javascript:" not in text, f.name
        scripts = re.findall(r"<script>", text)
        assert not scripts or f.name == "base.html", f"{f.name}: inline <script>"


def test_security_headers(unraid):
    with make_client(unraid, signed_in=False) as client:
        r = client.get("/login")
        csp = r.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "'unsafe-inline'" not in \
            csp.split("script-src")[1].split(";")[0]
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "same-origin"
        assert r.headers["cache-control"] == "no-store"
        big = client.post("/login", content=b"x" * (2 << 20),
                          headers={"content-type": "application/x-www-form-urlencoded"})
        assert big.status_code == 413


def test_allowed_host_names(unraid):
    _set(unraid, allowed_hosts=["tower.local"])
    with make_client(unraid, signed_in=False) as client:
        assert client.get("/login").status_code == 400  # Host: testserver
        assert client.get("/login", headers={"host": "tower.local:8080"}).status_code == 200
        assert client.get("/healthz").status_code == 200


# -- sign-in, sessions, lockout -----------------------------------------------------------


def test_session_cookie_flags_and_sign_out(unraid):
    with make_client(unraid, signed_in=False) as client:
        r = client.post("/login", data={"username": "admin", "password": TEST_PASSWORD}, follow_redirects=False)
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
        token = client.cookies.get(auth.COOKIE)
        c = db.connect(unraid.db_path)
        stored = c.execute("SELECT token_hash FROM sessions").fetchone()[0]
        c.close()
        assert token.encode() not in stored and stored == hashlib.sha256(token.encode()).digest()
        sign_in(client, unraid)
        assert client.post("/logout", follow_redirects=False).status_code == 303
        r = client.get("/setup", headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303


def test_wrong_password_is_throttled_per_address(conn):
    auth.create_admin(conn, "admin", TEST_PASSWORD)
    for _ in range(auth.IP_MAX_FAILURES):
        with pytest.raises(auth.AuthError, match="Wrong user name or password"):
            auth.authenticate(conn, "admin", "wrong password!", "10.0.0.5")
    with pytest.raises(auth.AuthError, match="from this address"):
        auth.authenticate(conn, "admin", TEST_PASSWORD, "10.0.0.5")
    # The admin can still sign in from another device.
    assert auth.authenticate(conn, "admin", TEST_PASSWORD, "10.0.0.6")
    # Unknown user names get the same answer as wrong passwords.
    with pytest.raises(auth.AuthError, match="Wrong user name or password"):
        auth.authenticate(conn, "nobody", TEST_PASSWORD, "10.0.0.7")
    assert any(r["event"] == "locked" for r in auth.recent_events(conn))
    # The lockout doubles the second time.
    conn.execute("UPDATE lockouts SET until = 0")
    for _ in range(auth.IP_MAX_FAILURES):
        with pytest.raises(auth.AuthError):
            auth.authenticate(conn, "admin", "wrong password!", "10.0.0.5")
    wait = auth.lockout_remaining(conn, "10.0.0.5")
    assert auth.IP_LOCK_FIRST * 2 - 5 < wait <= auth.IP_LOCK_FIRST * 2


def test_account_slows_down_after_many_failures_from_many_addresses(conn):
    auth.create_admin(conn, "admin", TEST_PASSWORD)
    for i in range(auth.ACCOUNT_MAX_FAILURES):
        with pytest.raises(auth.AuthError):
            auth.authenticate(conn, "admin", "wrong password!", f"10.1.{i}.1")
    with pytest.raises(auth.AuthError, match="for this account"):
        auth.authenticate(conn, "admin", TEST_PASSWORD, "10.2.0.1")
    conn.execute("UPDATE login_attempts SET at = at - ?", (auth.ACCOUNT_SLOW + 1,))
    assert auth.authenticate(conn, "admin", TEST_PASSWORD, "10.2.0.1")  # one try a minute still works


def test_password_rules(conn):
    for bad in ("short", "aaaaaaaaaaaaaaaa", "password1234", "my admin password", "x" * 2000):
        with pytest.raises(auth.AuthError):
            auth.check_new_password(bad, "admin")
    auth.check_new_password("correct horse battery staple", "admin")
    uid = auth.create_admin(conn, "admin", "correct horse battery staple")
    row = conn.execute("SELECT pw_hash FROM users").fetchone()
    assert row["pw_hash"].startswith("$argon2id$") and "staple" not in row["pw_hash"]
    with pytest.raises(auth.AuthError, match="already exists"):
        auth.create_admin(conn, "second", "a different long passphrase")
    token = auth.new_session(conn, uid, "1.2.3.4", "test")
    auth.set_password(conn, uid, "a brand new passphrase")
    assert auth.load_session(conn, token) is None  # changing the password ends every session


def test_sessions_expire(conn):
    uid = auth.create_admin(conn, "admin", TEST_PASSWORD)
    token = auth.new_session(conn, uid, "1.2.3.4", "test")
    assert auth.load_session(conn, token).username == "admin"
    conn.execute("UPDATE sessions SET last_seen = ?", (time.time() - auth.SESSION_IDLE - 1,))
    assert auth.load_session(conn, token) is None
    token = auth.new_session(conn, uid, "1.2.3.4", "test")
    conn.execute("UPDATE sessions SET created_at = ?", (time.time() - auth.SESSION_MAX - 1,))
    assert auth.load_session(conn, token) is None
    assert auth.load_session(conn, "not a token") is None


def test_first_run_needs_the_setup_code_from_the_log(unraid, caplog):
    with TestClient(create_app(unraid, start_worker=False)) as client:
        r = client.get("/login")
        assert "Create the admin account" in r.text
        code = client.app.state.setup_code.current()
        assert code in caplog.text
        r = client.post("/first-run", data={"code": "AAAA-BBBB-CCCC-DDDD", "username": "admin",
                                            "password": TEST_PASSWORD, "password2": TEST_PASSWORD})
        assert "setup code is wrong" in r.text
        r = client.post("/first-run", data={"code": code.lower().replace("-", " "), "username": "steve",
                                            "password": TEST_PASSWORD, "password2": TEST_PASSWORD})
        assert r.status_code == 200 and "steve" in r.text  # signed in, on Setup
        c = db.connect(unraid.db_path)
        assert c.execute("SELECT username FROM users").fetchall()[0][0] == "steve"
        c.close()
        # A second admin cannot be created, even with a valid code.
        r = client.post("/first-run", data={"code": code, "username": "x", "password": TEST_PASSWORD,
                                            "password2": TEST_PASSWORD}, follow_redirects=False)
        assert r.headers["location"] == "/login"


def test_dangerous_settings_ask_for_the_password_again(unraid):
    with make_client(unraid) as client:
        c = db.connect(unraid.db_path)
        c.execute("UPDATE sessions SET auth_at = auth_at - ?", (auth.CONFIRM_WINDOW + 5,))
        c.close()
        r = client.post("/setup/changes", data={"allow_changes": "1"})
        assert "Enter your password to confirm" in r.text
        c = db.connect(unraid.db_path)
        assert db.get_settings(c)["allow_changes"] is False
        c.close()
        r = client.post("/setup/changes", data={"allow_changes": "1", "password": TEST_PASSWORD})
        assert "Saved" in r.text
        c = db.connect(unraid.db_path)
        assert db.get_settings(c)["allow_changes"] is True
        c.close()


def test_reset_password_from_the_console(unraid, monkeypatch, capsys):
    import io

    from deedup.__main__ import reset_password

    ensure_admin(unraid)
    monkeypatch.setenv("DEEDUP_CONFIG", str(unraid.config_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO("a new console passphrase\n"))
    assert reset_password(stdin=True) == 0
    c = db.connect(unraid.db_path)
    assert auth.authenticate(c, "admin", "a new console passphrase", "1.1.1.1")
    c.close()


# -- SC7: redirects ---------------------------------------------------------------------


@pytest.mark.parametrize("target,ok", [
    ("/duplicates?method=image#g1", True), ("/", True), ("/?x=1", True), ("/changes/3", True),
    ("//evil.example", False), ("/\\evil.example", False), ("https://evil.example", False),
    ("/\tevil", False), ("evil", False), ("", False), ("/a\\b", False),
])
def test_redirects_stay_local(target, ok):
    """A7: only local paths; /\\ is read as // by browsers."""
    assert (safe_next(target, "DEFAULT") == target) is ok
