"""``python -m deedup`` starts the web UI and the background worker.

``deedup reset-password`` sets a new admin password from the server's console
(``docker exec -it deedup deedup reset-password``) and signs out every browser.
Only someone with a shell on the server can run it, which is the right bar.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from .config import load


def serve() -> None:
    import uvicorn

    from .web.app import create_app

    cfg = load()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info", access_log=False,
                server_header=False)


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
    p = argparse.ArgumentParser(prog="deedup", description="deedup: dedupe your hoard, safely.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="start the web UI (the default)")
    rp = sub.add_parser("reset-password", help="set a new admin password and sign out every browser")
    rp.add_argument("--stdin", action="store_true", help="read the password from standard input")
    args = p.parse_args(argv)
    if args.cmd == "reset-password":
        sys.exit(reset_password(args.stdin))
    serve()


if __name__ == "__main__":
    main()
