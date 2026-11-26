"""``python -m deedoop`` starts the web UI and the background worker.

``deedoop reset-password`` sets a new admin password from the server's console
(``docker exec -it deedoop deedoop reset-password``) and signs out every browser.
Only someone with a shell on the server can run it, which is the right bar.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from .config import load


def serve() -> None:
    import ssl

    import uvicorn

    from . import tls
    from .web.app import create_app

    cfg = load()
    info = tls.prepare(cfg)
    extra = {}
    if info.enabled:
        # SC8: TLS 1.2+ with forward-secret AEAD ciphers (tls.CIPHERS).
        extra = {"ssl_certfile": str(info.cert_path), "ssl_keyfile": str(info.key_path),
                 "ssl_version": ssl.PROTOCOL_TLS_SERVER, "ssl_ciphers": tls.CIPHERS}
        logging.getLogger("deedoop.tls").info(
            "HTTPS on with %s certificate, SHA-256 fingerprint %s",
            "your" if info.source == "yours" else "a self-signed", info.fingerprint)
        for p in info.problems:
            logging.getLogger("deedoop.tls").warning("%s", p)
    uvicorn.run(create_app(cfg, tls_info=info), host=cfg.host, port=cfg.port, log_level="info",
                access_log=False, server_header=False, **extra)


def healthcheck() -> int:
    """For Docker's HEALTHCHECK: 0 when the web UI answers, over HTTP or HTTPS."""
    import ssl
    import urllib.request

    cfg = load()
    scheme = "https" if cfg.https else "http"
    ctx = None
    if cfg.https:
        # Our own certificate on 127.0.0.1, possibly self-signed: only that the server answers matters here.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(f"{scheme}://127.0.0.1:{cfg.port}/healthz", timeout=4, context=ctx) as r:  # noqa: S310 - fixed local URL
            return 0 if r.status == 200 else 1
    except OSError:
        return 1


def reset_password(stdin: bool = False) -> int:
    from . import auth, db

    cfg = load()
    db.init(cfg.db_path)
    conn = db.connect(cfg.db_path)
    try:
        if stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = getpass.getpass("New admin password: ")
            if getpass.getpass("Again: ") != password:
                print("The two passwords are different.", file=sys.stderr)
                return 1
        user = auth.the_admin(conn)
        try:
            if user is None:
                uid = auth.create_admin(conn, "admin", password)
                name = "admin"
            else:
                uid, name = user["id"], user["username"]
                auth.set_password(conn, uid, password)
        except auth.AuthError as e:
            print(e, file=sys.stderr)
            return 1
        conn.execute("DELETE FROM lockouts")
        auth.event(conn, "password_reset", name, "console", "from the command line; all sessions ended")
        print(f"Password set for {name}. Every browser was signed out.")
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="deedoop", description="deedoop: dedupe your hoard, safely.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="start the web UI (the default)")
    sub.add_parser("healthcheck", help="exit 0 if the web UI answers (for Docker)")
    rp = sub.add_parser("reset-password", help="set a new admin password and sign out every browser")
    rp.add_argument("--stdin", action="store_true", help="read the password from standard input")
    args = p.parse_args(argv)
    if args.cmd == "reset-password":
        sys.exit(reset_password(args.stdin))
    if args.cmd == "healthcheck":
        sys.exit(healthcheck())
    serve()


if __name__ == "__main__":
    main()
