"""Milestone 6.5: sign-in, read-only mode, CSRF and the rules in docs/secure-coding.md.

Each test names the rule (SC1-SC10) or the audit finding (A1-A12) it guards.
"""

import hashlib
import logging
import re
import ssl
import time
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from deedoop import auth, db, tls
from deedoop.control import Status
from deedoop.web.app import create_app
from deedoop.web.security import safe_next
from deedoop.worker import JobLogHandler
from tests.conftest import TEST_PASSWORD, ensure_admin, sign_in
from tests.test_web import make_client

TEMPLATES = Path(__file__).parent.parent / "deedoop" / "web" / "templates"
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
        assert r.status_code == 200 and "You are not signed in, so deedoop is read-only" in r.text
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
        scripts = re.findall(r"<script\b[^>]*>(?!</script)", text, flags=re.I)
        assert not scripts or f.name == "base.html", f"{f.name}: inline <script>"


def test_sc6_toast_text_from_the_address_is_escaped(unraid):
    # Save confirmations repeat text from the redirect's query string: it must stay text.
    bad = '<img src=x onerror="alert(1)">'
    with make_client(unraid) as client:
        for url in ("/changes?msg=", "/changes?error=", "/duplicates?msg=", "/setup?error=", "/review?msg="):
            r = client.get(url + bad)
            assert bad not in r.text, url
            assert "&lt;img src=x onerror=" in r.text, url


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
        assert "Change settings saved" in r.text  # the toast names what was saved
        c = db.connect(unraid.db_path)
        assert db.get_settings(c)["allow_changes"] is True
        c.close()


def test_reset_password_from_the_console(unraid, monkeypatch, capsys):
    import io

    from deedoop.__main__ import reset_password

    ensure_admin(unraid)
    monkeypatch.setenv("DEEDOOP_CONFIG", str(unraid.config_dir))
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
    ("/\x7fevil", False), ("javascript:alert(1)", False), ("///evil.example", False),
])
def test_redirects_stay_local(target, ok):
    """A7: only local paths; /\\ is read as // by browsers."""
    assert (safe_next(target, "DEFAULT") == target) is ok


# -- A2: ExifTool argument injection --------------------------------------------------------


def test_safe_for_exiftool():
    from deedoop.metadata import safe_for_exiftool

    assert safe_for_exiftool("/mnt/disk1/Photos/IMG 1 (copy).jpg")
    assert safe_for_exiftool("/mnt/disk1/Photos/-all=.jpg")  # absolute, so never an option
    for bad in ("relative.jpg", "-all=", "/mnt/a\n-all=\n.jpg", "/mnt/a\r.jpg", "/mnt/a\x00.jpg", "/mnt/\x7f.jpg"):
        assert not safe_for_exiftool(bad), repr(bad)


@pytest.mark.skipif(__import__("shutil").which("exiftool") is None, reason="exiftool not installed")
def test_a_file_name_cannot_add_exiftool_options(tmp_path):
    """A2: before M6.5 this name added -all=, which stripped the tags of the other photo."""
    import subprocess

    from deedoop.metadata import UNSAFE_NAME, Reader
    from tests.test_images import jpeg, photo

    good = tmp_path / "good.jpg"
    good.write_bytes(jpeg(photo(3)))
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-Make=Canon", str(good)], check=True)
    evil = tmp_path / "x\n-all=\n.jpg"
    evil.write_bytes(jpeg(photo(4)))
    with Reader() as r:
        out = r.read([str(evil), str(good)])
    assert out[str(evil)].error == UNSAFE_NAME
    assert out[str(good)].camera and "Canon" in out[str(good)].camera
    tags = subprocess.run(["exiftool", "-s3", "-Make", str(good)], capture_output=True, text=True, check=True)
    assert tags.stdout.strip() == "Canon"
    assert not list(tmp_path.glob("*_original"))


# -- A5: archive bombs ---------------------------------------------------------------------


def _bomb(path: Path, size: int) -> None:
    import zipfile

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        with z.open("zeros.bin", "w", force_zip64=True) as f:
            chunk = b"\0" * (1 << 20)
            for _ in range(size >> 20):
                f.write(chunk)


def test_zip_bomb_is_skipped_quickly(tmp_path):
    from deedoop.archives import read_archive
    from deedoop.hashing import Expected
    from deedoop.limits import ArchiveTooLarge

    p = tmp_path / "bomb.zip"
    _bomb(p, 256 << 20)  # 256 MiB of zeros, about 250 KB on disk
    st = p.stat()
    assert st.st_size < 1 << 20
    t = time.time()
    with pytest.raises(ArchiveTooLarge, match="expands more than"):
        read_archive(str(p), Expected(st.st_size, st.st_mtime_ns))
    assert time.time() - t < 10


def test_nested_bomb_counts_towards_its_parent(tmp_path):
    import zipfile

    from deedoop.archives import read_archive
    from deedoop.hashing import Expected
    from deedoop.limits import ArchiveTooLarge

    inner = tmp_path / "inner.zip"
    _bomb(inner, 128 << 20)
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w", zipfile.ZIP_STORED) as z:
        z.write(inner, "inner.zip")
    st = outer.stat()
    with pytest.raises(ArchiveTooLarge):
        read_archive(str(outer), Expected(st.st_size, st.st_mtime_ns))


def test_too_many_members(tmp_path, monkeypatch):
    import zipfile

    from deedoop import limits
    from deedoop.archives import read_archive
    from deedoop.hashing import Expected

    monkeypatch.setattr(limits, "MAX_MEMBERS", 10)
    p = tmp_path / "many.zip"
    with zipfile.ZipFile(p, "w") as z:
        for i in range(11):
            z.writestr(f"f{i}.txt", str(i))
    st = p.stat()
    with pytest.raises(limits.ArchiveTooLarge, match="files inside"):
        read_archive(str(p), Expected(st.st_size, st.st_mtime_ns))


def test_tar_names_keep_their_leading_dots():
    from deedoop.archives import _strip_dot_slash

    assert _strip_dot_slash("./a/b.jpg") == "a/b.jpg"
    assert _strip_dot_slash(".hidden/x") == ".hidden/x"
    assert _strip_dot_slash("/abs/x") == "abs/x"


# -- A8, A9: symbolic links ------------------------------------------------------------------


def test_custom_folder_cannot_be_a_symlink(unraid, conn):
    from deedoop.discovery import RootError, add_custom_root

    outside = unraid.config_dir.parent / "outside"
    outside.mkdir()
    (unraid.mnt / "disks/usb/link").symlink_to(outside)
    with pytest.raises(RootError, match="symbolic link"):
        add_custom_root(conn, unraid.mnt, str(unraid.mnt / "disks/usb/link"))
    (unraid.mnt / "disks/usb/link2").symlink_to(unraid.mnt / "disks/usb/Backup")
    with pytest.raises(RootError, match="symbolic link"):
        add_custom_root(conn, unraid.mnt, str(unraid.mnt / "disks/usb/link2"))
    assert add_custom_root(conn, unraid.mnt, str(unraid.mnt / "disks/usb/Backup"))


def test_safepath_never_follows_a_swapped_folder(tmp_path):
    from deedoop import safepath

    share = tmp_path / "share"
    (share / "dir").mkdir(parents=True)
    (share / "dir/a.jpg").write_bytes(b"a")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "a.jpg").write_bytes(b"precious")
    q = tmp_path / "q"
    q.mkdir()
    # Someone swaps the folder for a symlink after the scan.
    (share / "dir").rename(share / "dir-old")
    (share / "dir").symlink_to(victim)
    with pytest.raises(safepath.UnsafePath):
        safepath.unlink(str(share / "dir/a.jpg"))
    with pytest.raises(safepath.UnsafePath):
        safepath.rename_noclobber(str(share / "dir/a.jpg"), str(q / "a.jpg"))
    assert (victim / "a.jpg").read_bytes() == b"precious"
    # Never overwrites.
    (q / "b.jpg").write_bytes(b"b")
    with pytest.raises(safepath.Exists):
        safepath.rename_noclobber(str(share / "dir-old/a.jpg"), str(q / "b.jpg"))
    safepath.rename_noclobber(str(share / "dir-old/a.jpg"), str(q / "a.jpg"))
    assert (q / "a.jpg").read_bytes() == b"a"
    for bad in ("relative", "/a/../b", "/a//b", "/a/./b"):
        with pytest.raises(safepath.UnsafePath):
            safepath.check(bad)


# -- A10: CSV formulas -------------------------------------------------------------------------


def test_csv_cells_cannot_be_formulas():
    from deedoop.queries import csv_cell

    assert csv_cell("=HYPERLINK(\"http://x\")") == "'=HYPERLINK(\"http://x\")"
    for v in ("+1", "-1", "@SUM(A1)", "\tx", "\rx"):
        assert csv_cell(v).startswith("'")
    assert csv_cell("IMG_1.jpg") == "IMG_1.jpg" and csv_cell(5) == 5


def test_no_extractall():
    """SC4: nothing in deedoop extracts an archive to disk."""
    src = Path(__file__).parent.parent / "deedoop"
    for f in src.rglob("*.py"):
        assert "extractall" not in f.read_text(), f


# -- SC3: ffprobe and ffmpeg (milestone 7) ---------------------------------------------------


def test_safe_for_ffmpeg():
    from deedoop.av import safe_for_ffmpeg

    assert safe_for_ffmpeg("/mnt/disk1/Music/AC:DC - Back in Black.mp3")
    assert safe_for_ffmpeg("/mnt/disk1/Music/-i.mp3")
    for bad in ("song.mp3", "concat:/a.mp3|/b.mp3", "-i", "/mnt/a\n.mp3", "/mnt/a\r.mp3", "/mnt/\x00.mp3"):
        assert not safe_for_ffmpeg(bad), repr(bad)


def test_a_file_name_cannot_add_ffmpeg_options(monkeypatch):
    """SC3: a name with a line break is never handed to ffprobe or ffmpeg."""
    import subprocess

    from deedoop import av

    def boom(*a, **kw):
        raise AssertionError("a subprocess was started for an unsafe name")

    monkeypatch.setattr(subprocess, "run", boom)
    assert av.probe("/mnt/disk1/x\n-y\n.mp3").error == av.UNSAFE_NAME
    with pytest.raises(ValueError):
        av.stream_hash("/mnt/disk1/x\n.mp3", 10)


@pytest.mark.skipif(not __import__("deedoop.av").av.tools_available(), reason="ffmpeg not installed")
def test_a_crafted_media_file_cannot_open_other_files(tmp_path):
    """SC3: a playlist in the backups must not make ffmpeg read another file
    (or a URL). Without -format_whitelist, ffmpeg opens the HLS playlist and
    hashes the file it points to."""
    import subprocess

    from deedoop import av

    secret = tmp_path / "secret.mp3"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=d=1", str(secret)], check=True)
    playlist = tmp_path / "evil.m3u8"
    playlist.write_text(f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nfile://{secret}\n#EXT-X-ENDLIST\n")
    with pytest.raises(RuntimeError):
        av.stream_hash(str(playlist), playlist.stat().st_size)
    assert av.probe(str(playlist)).error
    concat = tmp_path / "list.mp4"
    concat.write_text(f"ffconcat version 1.0\nfile '{secret}'\n")
    with pytest.raises(RuntimeError):
        av.stream_hash(str(concat), concat.stat().st_size)



@pytest.mark.skipif(not __import__("deedoop.av").av.tools_available(), reason="ffmpeg not installed")
def test_m8_fingerprints_and_posters_cannot_open_other_files(tmp_path):
    """SC3 (M8): the new ffmpeg commands (Chromaprint, video frames, posters)
    use the same protocol and format whitelists, so a playlist or concat list
    in the backups cannot make them read another file."""
    import subprocess

    from deedoop import av

    secret = tmp_path / "secret.mkv"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=15:s=64x64",
                    "-f", "lavfi", "-i", "sine=d=15", "-shortest", str(secret)], check=True)
    assert av.video_frame(str(secret), 1.0)  # the commands do work on a real file
    lists = [tmp_path / "evil.m3u8", tmp_path / "list.mp4"]
    lists[0].write_text(f"#EXTM3U\n#EXT-X-TARGETDURATION:15\n#EXTINF:15,\nfile://{secret}\n#EXT-X-ENDLIST\n")
    lists[1].write_text(f"ffconcat version 1.0\nfile '{secret}'\n")
    for crafted in lists:
        with pytest.raises(RuntimeError):
            av.video_frame(str(crafted), 1.0)
        with pytest.raises(RuntimeError):
            av.poster(str(crafted), 1.0)
        if av.chromaprint_available():
            with pytest.raises(RuntimeError):
                av.audio_fingerprint(str(crafted))
    with pytest.raises(ValueError):
        av.poster("concat:/mnt/a.mp4|/mnt/b.mp4", 1.0)


# -- milestone 7.5: built-in HTTPS and the scan log (SC8) ------------------------


def test_scan_log_never_takes_sign_in_messages():
    """SC8: the sign-in code logs the setup code to the container log; it must
    never reach the scan log, which signed-out visitors may be able to read."""
    st = Status()
    st.reset(job=1)
    handler = JobLogHandler(st)
    root = logging.getLogger("deedoop")
    root.addHandler(handler)
    try:
        logging.getLogger("deedoop.auth").warning("setup code: 123-456")
        logging.getLogger("deedoop.web").warning("something")
        logging.getLogger("deedoop.engine").warning("exiftool not found; skipping metadata")
    finally:
        root.removeHandler(handler)
    msgs = [m for _, _, m in st.drain_log()]
    assert msgs == ["exiftool not found; skipping metadata"]


def test_self_signed_key_is_owner_only(tmp_path):
    """SC8: the private key is written 0600 and the folder 0700."""
    info = tls.prepare(_tls_cfg(tmp_path))
    assert info.key_path.stat().st_mode & 0o777 == 0o600
    assert info.key_path.parent.stat().st_mode & 0o777 == 0o700


def test_https_refuses_old_tls(tmp_path):
    """SC8: the server context accepts TLS 1.2 and 1.3 only, no weak ciphers."""
    info = tls.prepare(_tls_cfg(tmp_path))
    ctx = tls.server_context(info.cert_path, info.key_path)
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    names = {c["name"] for c in ctx.get_ciphers()}
    assert names and not any(w in n for n in names for w in ("RC4", "3DES", "NULL", "CBC", "MD5"))
    tls12 = [c for c in ctx.get_ciphers() if c["protocol"] == "TLSv1.2"]
    assert tls12 and all(c["kea"] in ("kx-ecdhe", "kx-dhe") for c in tls12)


def _tls_cfg(tmp_path):
    from deedoop.config import AppConfig

    return AppConfig(config_dir=tmp_path / "config", mnt=tmp_path / "mnt", https=True,
                     tls_cert=tmp_path / "config/tls/cert.pem", tls_key=tmp_path / "config/tls/key.pem")
