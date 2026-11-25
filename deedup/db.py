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
        kind     TEXT NOT NULL,            -- partial, blake3, img_exact (later: phash, dhash)
        value    BLOB NOT NULL,
        PRIMARY KEY (file_id, kind)
    ) WITHOUT ROWID;
    CREATE INDEX hashes_kind_value ON hashes(kind, value);

    CREATE TABLE groups (
        id          INTEGER PRIMARY KEY,
        method      TEXT NOT NULL,          -- 'exact', 'image', 'similar'
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
    # v2: milestone 2, photos and metadata
    """
    CREATE TABLE media (
        file_id      INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
        mime         TEXT,
        taken_at     TEXT,     -- 'YYYY-MM-DD HH:MM:SS' as recorded by the camera
        camera       TEXT,
        width        INTEGER,
        height       INTEGER,
        duration     REAL,     -- seconds, for video and audio
        gps_lat      REAL,
        gps_lon      REAL,
        orientation  INTEGER,
        extracted_at REAL NOT NULL,
        error        TEXT
    );
    CREATE INDEX media_taken ON media(taken_at);

    -- A file whose content changed loses its metadata, like its hashes.
    CREATE TRIGGER files_changed_media AFTER UPDATE OF size, mtime_ns, inode, dev ON files
    BEGIN
        DELETE FROM media WHERE file_id = NEW.id;
    END;
    """,
    # v3: milestone 3, archives and Google Takeout
    """
    CREATE INDEX files_container ON files(container_id) WHERE container_id != 0;

    CREATE TABLE archives (
        file_id       INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
        kind          TEXT NOT NULL,      -- zip, tar, 7z
        takeout_set   TEXT,               -- e.g. 'disk3:Backups/takeout-20240301t101010z'; NULL if not a Takeout
        part          INTEGER,
        members       INTEGER NOT NULL DEFAULT 0,
        unpacked      INTEGER NOT NULL DEFAULT 0,   -- total member bytes
        indexed_at    REAL NOT NULL,
        error         TEXT,
        -- coverage, refreshed after every scan
        media_members INTEGER NOT NULL DEFAULT 0,  -- members that are not Takeout JSON
        cov_exact     INTEGER NOT NULL DEFAULT 0,
        cov_image     INTEGER NOT NULL DEFAULT 0,
        cov_archive   INTEGER NOT NULL DEFAULT 0,  -- only in another archive
        cov_new       INTEGER NOT NULL DEFAULT 0,
        new_bytes     INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX archives_set ON archives(takeout_set);

    -- Parsed Google Photos JSON sidecars (members or loose files).
    CREATE TABLE sidecar_json (
        file_id      INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
        title        TEXT,
        taken_at     TEXT,   -- UTC, 'YYYY-MM-DD HH:MM:SS'
        lat          REAL,
        lon          REAL,
        description  TEXT
    );

    -- Which sidecar belongs to which photo; rebuilt after every scan.
    CREATE TABLE sidecars (
        file_id       INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
        json_file_id  INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE
    );

    -- An archive that changed is indexed again from scratch.
    CREATE TRIGGER files_changed_archive AFTER UPDATE OF size, mtime_ns, inode, dev ON files
    BEGIN
        DELETE FROM files WHERE container_id = NEW.id;
        DELETE FROM archives WHERE file_id = NEW.id;
        DELETE FROM sidecar_json WHERE file_id = NEW.id;
    END;

    -- Members follow their archive: gone when it is gone, back when it is back.
    CREATE TRIGGER files_state_members AFTER UPDATE OF state ON files
    WHEN NEW.container_id = 0 AND NEW.state != OLD.state
    BEGIN
        UPDATE files SET state = NEW.state WHERE container_id = NEW.id;
    END;

    -- Deleting an archive row deletes its members (and theirs, one level down).
    CREATE TRIGGER files_deleted_members AFTER DELETE ON files
    BEGIN
        DELETE FROM files WHERE container_id = OLD.id;
    END;
    """,
    # v4: similar photos (milestone 4)
    """
    CREATE TABLE similar_pairs (
        a      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        b      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        pdist  INTEGER NOT NULL,          -- pHash Hamming distance
        ddist  INTEGER NOT NULL,          -- dHash Hamming distance
        PRIMARY KEY (a, b)
    ) WITHOUT ROWID;
    CREATE INDEX similar_pairs_b ON similar_pairs(b);

    -- Review decisions, keyed by the two photos' image-data hashes so they
    -- survive rescans, renames and moves.
    CREATE TABLE reviews (
        key_a       BLOB NOT NULL,
        key_b       BLOB NOT NULL,
        decision    TEXT NOT NULL,        -- keep_a, keep_b, both, not_dup, later
        keep        BLOB,                 -- image-data hash of the copy to keep
        decided_at  REAL NOT NULL,
        PRIMARY KEY (key_a, key_b)
    ) WITHOUT ROWID;

    ALTER TABLE groups ADD COLUMN score INTEGER;       -- similar: closest pHash distance
    ALTER TABLE groups ADD COLUMN pending INTEGER;     -- similar: pairs still to review
    ALTER TABLE archives ADD COLUMN cov_similar INTEGER;
    -- Archives indexed before perceptual hashes existed are read once more.
    ALTER TABLE archives ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
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
    # Folders holding Google Takeout or other archives. Archives in them are
    # opened and their contents indexed.
    "takeout_paths": [],
    # Also open archives found anywhere else in the selected shares.
    "archives_everywhere": False,
    # Match methods. Exact content is always on.
    "image_matching": True,   # same photo, different metadata
    "metadata": True,         # read date taken, camera, size, GPS with ExifTool
    "similar_matching": True,  # similar photos: recompressed or resized copies, reviewed by you
    "similar_threshold": 8,    # largest pHash distance (0-10) shown as similar
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
