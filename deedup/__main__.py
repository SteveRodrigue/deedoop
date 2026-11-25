"""``python -m deedup`` starts the web UI and the background worker."""

from __future__ import annotations

import logging

import uvicorn

from .config import load
from .web.app import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
