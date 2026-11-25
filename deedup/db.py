"""SQLite index: schema, connections and settings.

The index is one SQLite file in WAL mode. Each thread opens its own
connection with :func:`connect`. The schema follows the design doc; tables
that later milestones fill (``media``, ``sidecars``, ``reviews``,
``actions``) are created when those milestones land, through
``MIGRATIONS``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

MIGRATIONS: list[str] = [
    # v1: milestone 1
    """
    CREATE TABLE roots (
        id          INTEGER PRIMARY KEY,
        path        TEXT NOT NULL UNIQUE,   -- physical path, e.g. /mnt/disk3/Photos
        disk        TEXT NOT NULL,          -- disk or pool name, e.g. disk3, cache
        share       TEXT,                   -- Unraid share name, NULL for custom paths
        user_path   TEXT NOT NULL,          -- what the user knows, e.g. /mnt/user/Photos
        source      TEXT NOT NULL DEFAULT 'share',  -- 'share' or 'custom'
        enabled     INTEGER NOT NULL DEFAULT 1,
        last_scan_job INTEGER,
        last_scan_at  REAL,
        created_at  REAL NOT NULL
    );

    CREATE TABLE files (
        id            INTEGER PRIMARY KEY,
        root_id       INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
        dir           TEXT NOT NULL,        -- directory relative to the root, '' for top
        name          TEXT NOT NULL,
        container_id  INTEGER NOT NULL DEFAULT 0,  -- archive file id for members (M3); 0 = on disk
        size          INTEGER NOT NULL,
        mtime_ns      INTEGER NOT NULL,
        dev           INTEGER NOT NULL,
        inode         INTEGER NOT NULL,
        nlink         INTEGER NOT NULL,
        ext           TEXT NOT NULL,        -- lower-case extension without the dot
        state         TEXT NOT NULL DEFAULT 'present',  -- 'present' or 'gone'
        last_seen_run INTEGER NOT NULL,
        error         TEXT,
        UNIQUE (root_id, dir, name, container_id)
    );
    CREATE INDEX files_size ON files(size) WHERE state = 'present';
    CREATE INDEX files_phys ON files(dev, inode);

    CREATE TABLE hashes (
        file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        kind     TEXT NOT NULL,            -- partial, blake3 (later: img_exact, phash, dhash)
        value    BLOB NOT NULL,
        PRIMARY KEY (file_id, kind)
    ) WITHOUT ROWID;
    CREATE INDEX hashes_kind_value ON hashes(kind, value);

    CREATE TABLE groups (
        id          INTEGER PRIMARY KEY,
        method      TEXT NOT NULL,          -- 'exact' (later: 'image', 'fuzzy')
        key         TEXT NOT NULL,
        size        INTEGER NOT NULL,       -- size of one copy
        file_count  INTEGER NOT NULL,       -- paths in the group
        copies      INTEGER NOT NULL,       -- distinct physical copies (hardlinks count once)
        wasted      INTEGER NOT NULL,       -- size * (copies - 1)
        category    TEXT NOT NULL,
        built_at    REAL NOT NULL,
        UNIQUE (method, key)
    );
    CREATE INDEX groups_wasted ON groups(method, wasted DESC);

    CREATE TABLE group_members (
        group_id  INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
        file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        role      TEXT NOT NULL DEFAULT 'undecided',  -- keep, remove, undecided
        PRIMARY KEY (group_id, file_id)
    ) WITHOUT ROWID;
    CREATE INDEX group_members_file ON group_members(file_id);

    CREATE TABLE jobs (
        id          INTEGER PRIMARY KEY,
        type        TEXT NOT NULL,          -- scan, benchmark
        params      TEXT NOT NULL DEFAULT '{}',
        state       TEXT NOT NULL DEFAULT 'queued',  -- queued, running, done, failed, stopped
        cursor      TEXT NOT NULL DEFAULT '{}',
        progress    TEXT NOT NULL DEFAULT '{}',
        error       TEXT,
        created_at  REAL NOT NULL,
        started_at  REAL,
        finished_at REAL
    );

    CREATE TABLE settings (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    );

    CREATE TABLE benchmarks (
        id          INTEGER PRIMARY KEY,
        created_at  REAL NOT NULL,
        result      TEXT NOT NULL
    );
    """,
]

DEFAULT_SETTINGS: dict[str, Any] = {
    # Shares ticked on the setup screen; their per-disk roots are derived.
    "shares": [],
    # fnmatch patterns, matched against each file or folder name and against
    # its path relative to the share.
    "exclusions": [
        ".Recycle.Bin",
        ".Trash-*",
        "#recycle",
        "@eaDir",
        ".deedup-quarantine",
    ],
    # Folders holding Google Takeout or other archives. Recorded now, used
    # from milestone 3.
    "takeout_paths": [],
    # Files smaller than this are ignored by the duplicate finder.
    "min_size": 1,
    # 'single': one disk at a time. 'parallel': up to parallel_disks at once.
    "disk_mode": "single",
    "parallel_disks": 2,
    # BLAKE3 threads per large file. 1 is right for spinning disks.
    "hash_threads": 1,
    "schedule_enabled": False,
    "schedule_start": "01:00",
    "schedule_end": "07:00",
}


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=60, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-65536")
    return conn


def init(path: Path | str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
            conn.execute("BEGIN")
            for stmt in _split(script):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version={i}")
            conn.execute("COMMIT")
    finally:
        conn.close()


def _split(script: str) -> list[str]:
    out, buf = [], []
    for line in script.splitlines():
        buf.append(line)
        if sqlite3.complete_statement("\n".join(buf)):
            stmt = "\n".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
    return out


class Tx:
    """``with Tx(conn):`` runs the block in one IMMEDIATE transaction."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")


# -- settings -----------------------------------------------------------------


def get_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    out = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        out[row["key"]] = json.loads(row["value"])
    return out


def set_settings(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    with Tx(conn):
        for k, v in values.items():
            if k not in DEFAULT_SETTINGS:
                raise KeyError(k)
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, json.dumps(v)),
            )


# -- jobs ---------------------------------------------------------------------


def enqueue_job(conn: sqlite3.Connection, type_: str, params: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO jobs(type, params, created_at) VALUES (?, ?, ?)",
        (type_, json.dumps(params or {}), time.time()),
    )
    return int(cur.lastrowid)


def active_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM jobs WHERE state IN ('queued', 'running') ORDER BY id LIMIT 1"
    ).fetchone()
