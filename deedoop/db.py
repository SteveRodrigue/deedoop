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
    # v5: reorganisation plans (milestone 5). Preview only: milestone 6 applies them.
    """
    CREATE TABLE plans (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL,
        kind        TEXT NOT NULL,        -- move
        rule        TEXT NOT NULL,        -- JSON: template, fallback, conflicts
        source      TEXT NOT NULL,        -- JSON: the Browse location and filters
        counts      TEXT NOT NULL,        -- JSON: items per status
        created_at  REAL NOT NULL,
        checked_at  REAL
    );
    CREATE TABLE plan_items (
        plan_id   INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
        file_id   INTEGER NOT NULL,       -- no foreign key: a plan outlives a rescan
        src_path  TEXT NOT NULL,
        src_user  TEXT NOT NULL,
        dst_path  TEXT NOT NULL,
        dst_user  TEXT NOT NULL,
        size      INTEGER NOT NULL,
        mtime_ns  INTEGER NOT NULL,
        status    TEXT NOT NULL,
        note      TEXT NOT NULL DEFAULT '',
        stale     INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (plan_id, file_id)
    ) WITHOUT ROWID;
    CREATE INDEX plan_items_status ON plan_items(plan_id, status);
    """,
    # v6: changes to files (milestone 6): marks, actions and their journal
    """
    -- Files you marked for removal, each with the copy that is kept.
    CREATE TABLE marks (
        file_id    INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
        keep_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        method     TEXT NOT NULL,         -- exact, image, similar
        marked_at  REAL NOT NULL
    );
    CREATE INDEX marks_keep ON marks(keep_id);

    -- One confirmed change: a batch of removals or the moves of a plan.
    CREATE TABLE actions (
        id          INTEGER PRIMARY KEY,
        kind        TEXT NOT NULL,        -- remove, move
        mode        TEXT NOT NULL,        -- quarantine, delete, hardlink, move
        label       TEXT NOT NULL,
        plan_id     INTEGER,
        state       TEXT NOT NULL DEFAULT 'queued',  -- queued, running, done, stopped, failed, undone
        counts      TEXT NOT NULL DEFAULT '{}',
        job_id      INTEGER,
        created_at  REAL NOT NULL,
        finished_at REAL,
        undone_at   REAL,
        purged_at   REAL
    );

    -- The journal: what was done to each file, so it can be checked and undone.
    CREATE TABLE action_items (
        action_id   INTEGER NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
        seq         INTEGER NOT NULL,
        file_id     INTEGER NOT NULL,
        keep_id     INTEGER,
        method      TEXT,                 -- exact, image, similar (removals)
        src_path    TEXT NOT NULL,        -- where the file was
        src_user    TEXT NOT NULL,
        dst_path    TEXT NOT NULL DEFAULT '',  -- quarantine path, or the plan's destination
        dst_user    TEXT NOT NULL DEFAULT '',
        keep_path   TEXT NOT NULL DEFAULT '',
        quarantine  TEXT NOT NULL DEFAULT '',  -- where the original went (moves across devices, hardlinks)
        size        INTEGER NOT NULL,
        mtime_ns    INTEGER NOT NULL,
        keep_size   INTEGER,
        keep_mtime  INTEGER,
        made_dirs   TEXT NOT NULL DEFAULT '[]',
        status      TEXT NOT NULL DEFAULT 'pending',  -- pending, done, skipped, failed, undone, purged
        note        TEXT NOT NULL DEFAULT '',
        done_at     REAL,
        PRIMARY KEY (action_id, seq)
    ) WITHOUT ROWID;
    CREATE INDEX action_items_status ON action_items(action_id, status);
    """,
    # v7: milestone 6.5, sign-in and audit trail
    """
    CREATE TABLE users (
        id          INTEGER PRIMARY KEY,
        username    TEXT NOT NULL UNIQUE COLLATE NOCASE,
        pw_hash     TEXT NOT NULL,            -- Argon2id, never the password
        created_at  REAL NOT NULL,
        pw_changed_at REAL NOT NULL
    );
    CREATE TABLE sessions (
        token_hash  BLOB PRIMARY KEY,         -- SHA-256 of the cookie value, never the value
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        csrf        TEXT NOT NULL,
        created_at  REAL NOT NULL,
        last_seen   REAL NOT NULL,
        auth_at     REAL NOT NULL,            -- last time the password was typed
        ip          TEXT NOT NULL DEFAULT '',
        user_agent  TEXT NOT NULL DEFAULT ''
    ) WITHOUT ROWID;
    CREATE TABLE login_attempts (
        id          INTEGER PRIMARY KEY,
        at          REAL NOT NULL,
        ip          TEXT NOT NULL,
        username    TEXT NOT NULL,
        ok          INTEGER NOT NULL
    );
    CREATE INDEX login_attempts_ip ON login_attempts(ip, at);
    CREATE INDEX login_attempts_at ON login_attempts(at);
    CREATE TABLE lockouts (
        ip          TEXT PRIMARY KEY,
        until       REAL NOT NULL,
        strikes     INTEGER NOT NULL
    ) WITHOUT ROWID;
    CREATE TABLE security_log (
        id          INTEGER PRIMARY KEY,
        at          REAL NOT NULL,
        event       TEXT NOT NULL,            -- signin, signin_failed, locked, signout, change, csrf, ...
        username    TEXT NOT NULL DEFAULT '',
        ip          TEXT NOT NULL DEFAULT '',
        detail      TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX security_log_at ON security_log(at);
    ALTER TABLE actions ADD COLUMN username TEXT NOT NULL DEFAULT '';
    ALTER TABLE actions ADD COLUMN ip TEXT NOT NULL DEFAULT '';
    """,
    # v8: milestone 7, video and audio (ffprobe headers; stream hashes go in hashes)
    """
    CREATE TABLE av (
        file_id      INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
        kind         TEXT NOT NULL,       -- audio or video, from the streams
        container    TEXT,                -- ffprobe format name, e.g. 'mov,mp4,m4a,3gp,3g2,mj2'
        duration     REAL,                -- seconds
        bitrate      INTEGER,             -- bits per second, whole file
        vcodec       TEXT,
        width        INTEGER,
        height       INTEGER,
        fps          REAL,
        acodec       TEXT,
        channels     INTEGER,
        sample_rate  INTEGER,
        layout       TEXT NOT NULL DEFAULT '',  -- stream signature: stream-hash candidates share it
        artist       TEXT,
        album        TEXT,
        album_artist TEXT,
        title        TEXT,
        track        INTEGER,
        disc         INTEGER,
        year         TEXT,
        genre        TEXT,
        has_cover    INTEGER NOT NULL DEFAULT 0,
        tags         INTEGER NOT NULL DEFAULT 0,  -- filled tag fields (+1 for cover art): the keeper rule
        probed_at    REAL NOT NULL,
        error        TEXT
    );
    CREATE INDEX av_music ON av(kind, artist, album);
    CREATE INDEX av_layout ON av(layout, duration);

    CREATE TRIGGER files_changed_av AFTER UPDATE OF size, mtime_ns, inode, dev ON files
    BEGIN
        DELETE FROM av WHERE file_id = NEW.id;
    END;
    """,
    # v9: milestone 7.5, the scan log
    """
    CREATE TABLE scan_log (
        id      INTEGER PRIMARY KEY,
        job_id  INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        at      REAL NOT NULL,
        level   TEXT NOT NULL,            -- info, warn, error
        message TEXT NOT NULL
    );
    CREATE INDEX scan_log_job ON scan_log(job_id, id);
    """,
    # v10: milestone 8, re-encoded songs and videos (fingerprints go in hashes:
    # afp, vfp, and mkey, the key decisions are stored against)
    """
    CREATE TABLE media_pairs (
        a       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        b       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        kind    TEXT NOT NULL,            -- audio or video
        score   REAL NOT NULL,            -- share of fingerprint bits (audio) or frames (video) that agree
        offset  REAL NOT NULL DEFAULT 0,  -- seconds b is shifted against a
        PRIMARY KEY (a, b)
    ) WITHOUT ROWID;
    CREATE INDEX media_pairs_b ON media_pairs(b);

    CREATE TABLE media_reviews (
        key_a       BLOB NOT NULL,
        key_b       BLOB NOT NULL,
        decision    TEXT NOT NULL,        -- keep_a, keep_b, both, not_dup, later
        keep        BLOB,                 -- key of the copy to keep
        decided_at  REAL NOT NULL,
        PRIMARY KEY (key_a, key_b)
    ) WITHOUT ROWID;
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
        ".deedoop-quarantine",
        ".deedup-quarantine",
    ],
    # Folders holding Google Takeout or other archives. Archives in them are
    # opened and their contents indexed.
    "takeout_paths": [],
    # Also open archives found anywhere else in the selected shares.
    "archives_everywhere": False,
    # Milestone 7: scanners an admin can turn off (see scanners.py). With all
    # of them off, deedoop only compares raw file contents.
    "scanners": {"photo": True, "video": True, "audio": True, "archive": True},
    # Match methods. Exact content is always on; the next three belong to the
    # photo scanner and only run while it is on.
    "image_matching": True,   # same photo, different metadata
    "metadata": True,         # read date taken, camera, size, GPS with ExifTool
    "similar_matching": True,  # similar photos: recompressed or resized copies, reviewed by you
    "similar_threshold": 8,
    "similar_media": True,     # re-encoded songs and videos (fingerprints), reviewed by you    # largest pHash distance (0-10) shown as similar
    # Files smaller than this are ignored by the duplicate finder.
    "min_size": 1,
    # 'single': one disk at a time. 'parallel': up to parallel_disks at once.
    "disk_mode": "single",
    "parallel_disks": 2,
    # BLAKE3 threads per large file. 1 is right for spinning disks.
    "hash_threads": 1,
    # Milestone 6: changes to files. Off until you turn them on in Setup.
    "allow_changes": False,
    "allow_direct_delete": False,  # removals skip the quarantine (cannot be undone)
    "quarantine_days": 30,         # quarantined files are deleted after this many days; 0 keeps them
    # Milestone 6.5: what people who are not signed in can see. False: a status
    # page and the sign-in form only. True: every page, read-only.
    "public_view": False,
    # Host names the web UI answers to (DNS-rebinding guard). Empty: any.
    "allowed_hosts": [],
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


LOG_KEEP_JOBS = 30


def add_log(conn: sqlite3.Connection, job_id: int, lines: list[tuple[float, str, str]]) -> None:
    if lines:
        with Tx(conn):
            conn.executemany("INSERT INTO scan_log(job_id, at, level, message) VALUES (?, ?, ?, ?)",
                             [(job_id, at, level, msg) for at, level, msg in lines])


def prune_logs(conn: sqlite3.Connection, keep: int = LOG_KEEP_JOBS) -> None:
    """Keep the logs of the last ``keep`` jobs."""
    conn.execute("DELETE FROM scan_log WHERE job_id NOT IN (SELECT id FROM jobs ORDER BY id DESC LIMIT ?)", (keep,))


def job_log(conn: sqlite3.Connection, job_id: int, warnings_only: bool = False, limit: int = 2000) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT at, level, message FROM scan_log WHERE job_id = ? AND (? = 0 OR level != 'info') ORDER BY id LIMIT ?",
        (job_id, int(warnings_only), limit)).fetchall()


def active_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM jobs WHERE state IN ('queued', 'running') ORDER BY id LIMIT 1"
    ).fetchone()
