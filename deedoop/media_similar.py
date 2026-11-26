"""Re-encoded songs and videos (milestone 8): fingerprints, pairs, review.

Milestone 7 finds the same recording with other tags. This finds copies
whose audio or video was *re-encoded*: a FLAC and the 128 kbps MP3 made from
it, the WhatsApp or Google Photos copy of a phone video, a resized export.
Their bytes and compressed streams differ, so only what you would hear or
see can match them:

* **Songs**: the Chromaprint fingerprint of the first two minutes (the one
  AcoustID uses, made by ffmpeg's Chromaprint muxer). Two songs whose lengths
  are within ``AUDIO_DURATION_SLACK`` seconds are compared at the best
  offset (up to about 5 s), and match when at least ``AUDIO_MIN_SCORE`` of
  the fingerprint bits agree. Candidates come from an index of fingerprint
  values, so most songs are never compared with each other.
* **Videos**: 12 frames at fixed points through the video ((i + 0.5) / 12
  of its length), each given the same 64-bit pHash as photos. Two videos
  whose lengths are within ``VIDEO_DURATION_RATIO`` match when, at the best
  shift of up to one frame (so a trimmed copy can still line up), at least
  ``VIDEO_MIN_SHARE`` of the frames both have are within ``FRAME_MAX_DIST``
  bits. Near-blank frames (black, white, a title card) are left out.

Like similar photos, these matches are suggestions: they go to the Review
screen, are not counted as reclaimable until you decide them, and a decision
is stored against the two files' fingerprints, so it survives renames and
moves, and "not the same" never comes back.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections import defaultdict

import numpy as np

from . import av, db, scanners
from .categories import group_category
from .similar import _DCT32

FLUSH = 25

AUDIO_MIN_DURATION = 10.0      # seconds: shorter sounds (jingles, notifications) are not fingerprinted
AUDIO_DURATION_SLACK = 3.0     # seconds between the two lengths
AUDIO_MAX_OFFSET = 40          # fingerprint words (about 8 per second): about 5 s
AUDIO_MIN_SCORE = 0.85         # share of fingerprint bits that agree; unrelated songs agree on about half
AUDIO_MIN_WORDS = 80           # overlap needed to compare (10 s)
_AUDIO_KEY_SHIFT = 12          # the index uses the top 20 bits of each word
_AUDIO_KEY_SAMPLE = 4          # ... and one in four values, chosen by value so both songs keep the same ones
_AUDIO_MIN_SHARED = 4          # index hits needed to compare two songs
_AUDIO_MAX_BUCKET = 64         # a value this common is silence or noise

VIDEO_FRAMES = 12
VIDEO_MIN_DURATION = 1.0
VIDEO_DURATION_RATIO = 0.8     # the shorter is at least this share of the longer (a trimmed copy)
FRAME_MAX_DIST = 10            # pHash bits that may differ between two frames of the same moment
VIDEO_MIN_FRAMES = 4           # frames both videos must have to be compared
VIDEO_MIN_SHARE = 0.6
_BLANK_STD = 4.0               # greyscale standard deviation under which a frame is near-blank

DECISIONS = {
    "keep_a": "Same, keep left",
    "keep_b": "Same, keep right",
    "both": "Same, keep both",
    "not_dup": "Not the same",
    "later": "Decide later",
}
_UNLINKED = ("both", "not_dup")
LOSSLESS = {"flac", "alac", "wavpack", "ape", "tta", "truehd", "mlp", "shorten", "tak", "ffv1", "huffyuv",
            "utvideo"}


# -- hooks (called from media_index, which the video and audio scanners share) ----------


def kinds_on(settings: dict) -> list[str]:
    """'audio' and/or 'video': fingerprints wanted for these, with their scanner on."""
    if not settings.get("similar_media"):
        return []
    out = [k for k in scanners.media_kinds(settings)]
    if "audio" in out and not av.chromaprint_available():
        out.remove("audio")
    return out


def plan(settings: dict) -> set[str]:
    """Phases the video and audio scanners run with these settings."""
    on = kinds_on(settings)
    return {"probe", "hash_stream"} | {f"fp_{k}" for k in on}


def fingerprint(scan) -> None:
    for kind in kinds_on(scan.settings):
        scan.run_phase(f"fp_{kind}", fp_candidates(scan.conn, kind), fp_disk)


def group(scan) -> dict:
    conn, settings = scan.conn, scan.settings
    on = kinds_on(settings)
    if not on:
        drop(conn)
        return {}
    if scan.pairs:
        scan.ctx.status.set(phase="media_pairs")
        rebuild_pairs(conn, on)
    res = rebuild_groups(conn)
    return {"media_similar_groups": res["groups"], "media_similar_wasted": res["wasted"]}


def drop(conn: sqlite3.Connection) -> None:
    with db.Tx(conn):
        conn.execute("DELETE FROM groups WHERE method = 'media_similar'")
        conn.execute("DELETE FROM media_pairs")
        conn.execute("DELETE FROM marks WHERE method = 'media_similar'")


# -- fingerprints ------------------------------------------------------------------------

_LOOSE = """
    FROM files f JOIN roots r ON r.id = f.root_id JOIN av ON av.file_id = f.id
    WHERE f.state = 'present' AND r.enabled = 1 AND f.container_id = 0 AND av.error IS NULL
"""


def fp_candidates(conn: sqlite3.Connection, kind: str) -> list[sqlite3.Row]:
    hk = "afp" if kind == "audio" else "vfp"
    least = AUDIO_MIN_DURATION if kind == "audio" else VIDEO_MIN_DURATION
    return conn.execute(
        f"""SELECT f.id, f.size, f.mtime_ns, f.dev, f.inode, f.dir, f.name, r.disk, r.path AS root_path, av.duration
            {_LOOSE} AND av.kind = ? AND av.duration >= ?
              AND NOT EXISTS (SELECT 1 FROM hashes h WHERE h.file_id = f.id AND h.kind IN (?, ?))
            ORDER BY r.disk, f.dev, f.inode""",  # SC5: placeholders only
        (kind, least, hk, hk + "_none"),
    ).fetchall()


def phash_gray(a: np.ndarray) -> int:
    """64-bit pHash of a 32x32 greyscale frame, as for photos; 0 for a near-blank one."""
    a = a.astype(np.float64)
    if a.std() < _BLANK_STD:
        return 0
    low = (_DCT32 @ a @ _DCT32.T)[:8, :8].flatten()
    bits = low > np.median(low[1:])
    return int.from_bytes(np.packbits(bits.astype(np.uint8)).tobytes(), "big")


def video_fingerprint(path: str, duration: float) -> bytes:
    """12 frame pHashes, 8 bytes each, big-endian; 0 for a missing or blank frame."""
    out = []
    for i in range(VIDEO_FRAMES):
        raw = av.video_frame(path, duration * (i + 0.5) / VIDEO_FRAMES)
        v = phash_gray(np.frombuffer(raw, dtype=np.uint8).reshape(av.FRAME, av.FRAME)) if raw else 0
        out.append(v.to_bytes(8, "big"))
    fp = b"".join(out)
    if sum(1 for i in range(VIDEO_FRAMES) if fp[8 * i:8 * i + 8] != bytes(8)) < VIDEO_MIN_FRAMES:
        raise RuntimeError("too few frames with a picture to fingerprint")
    return fp


def fingerprint_one(path: str, kind: str, size: int, mtime_ns: int, duration: float) -> list[tuple[str, bytes]]:
    """The hashes rows for one file: the fingerprint and its key, or ``*_none``
    with the reason."""
    hk = "afp" if kind == "audio" else "vfp"
    try:
        st = os.stat(path)
        if (st.st_size, st.st_mtime_ns) != (size, mtime_ns):
            return [(hk + "_none", b"changed since the scan; will retry next scan")]
        fp = av.audio_fingerprint(path) if kind == "audio" else video_fingerprint(path, duration)
        st = os.stat(path)
        if (st.st_size, st.st_mtime_ns) != (size, mtime_ns):
            return [(hk + "_none", b"changed while reading; will retry next scan")]
    except (RuntimeError, ValueError) as e:
        return [(hk + "_none", str(e).encode()[:300])]
    except OSError as e:
        return [(hk + "_none", f"{type(e).__name__}: {e.strerror or e}".encode()[:300])]
    # The key decisions are stored against: the fingerprint and the size, so a
    # renamed or moved file keeps its verdict and a changed file loses it.
    key = hashlib.blake2b(fp + size.to_bytes(8, "big"), digest_size=16).digest()
    return [(hk, fp), ("mkey", key)]


def fp_disk(ctx, disk: str, phase: str, rows: list[sqlite3.Row], settings: dict) -> None:
    st, control = ctx.status, ctx.control
    kind = "audio" if phase == "fp_audio" else "video"
    conn = db.connect(ctx.db_path)
    pending: list[tuple] = []

    def flush() -> None:
        nonlocal pending
        if pending:
            with db.Tx(conn):
                conn.executemany("INSERT OR REPLACE INTO hashes(file_id, kind, value) VALUES (?, ?, ?)", pending)
        pending = []

    st.disk(disk, state="listening" if kind == "audio" else "watching")
    last: tuple | None = None
    last_vals: list[tuple[str, bytes]] = []
    try:
        for row in rows:
            control.checkpoint()
            phys = (row["dev"], row["inode"], row["size"], row["mtime_ns"])
            if phys != last:
                path = os.path.join(row["root_path"], row["dir"], row["name"])
                st.disk(disk, current=path)
                last_vals = fingerprint_one(path, kind, row["size"], row["mtime_ns"], row["duration"])
                last = phys
                if last_vals[0][0].endswith("_none"):
                    st.log("warn", f"{path}: no fingerprint ({last_vals[0][1].decode(errors='replace')})")
            pending.extend((row["id"], k, v) for k, v in last_vals)
            st.add(disk, "done_files", 1)
            if len(pending) >= FLUSH:
                flush()
        flush()
        st.disk(disk, state="done", current="")
    finally:
        try:
            flush()
        finally:
            conn.close()


# -- pairs -------------------------------------------------------------------------------


def _items(conn: sqlite3.Connection, kind: str) -> list[sqlite3.Row]:
    hk = "afp" if kind == "audio" else "vfp"
    return conn.execute(
        """SELECT f.id, f.dev, f.inode, av.duration, fp.value AS fp,
                  (SELECT value FROM hashes WHERE file_id = f.id AND kind = 'blake3') AS b3,
                  (SELECT value FROM hashes WHERE file_id = f.id AND kind = 'stream') AS sh
           FROM files f JOIN roots r ON r.id = f.root_id JOIN av ON av.file_id = f.id
           JOIN hashes fp ON fp.file_id = f.id AND fp.kind = ?
           WHERE f.state = 'present' AND r.enabled = 1 AND f.container_id = 0 AND av.error IS NULL
             AND av.kind = ?""",
        (hk, kind),
    ).fetchall()


def _same(r1, r2) -> bool:
    """The same file or the same recording: already found by another method."""
    return ((r1["dev"], r1["inode"]) == (r2["dev"], r2["inode"])
            or (r1["b3"] is not None and r1["b3"] == r2["b3"])
            or (r1["sh"] is not None and r1["sh"] == r2["sh"]))


def audio_score(a: np.ndarray, b: np.ndarray, max_offset: int = AUDIO_MAX_OFFSET) -> tuple[float, int]:
    """Best share of agreeing bits over offsets of ``b`` against ``a``, and that offset (in words)."""
    best, best_off = 0.0, 0
    for off in range(-max_offset, max_offset + 1):
        x, y = (a[off:], b) if off >= 0 else (a, b[-off:])
        n = min(len(x), len(y))
        if n < AUDIO_MIN_WORDS:
            continue
        s = 1.0 - float(np.bitwise_count(x[:n] ^ y[:n]).sum()) / (32 * n)
        if s > best:
            best, best_off = s, off
    return best, best_off


def _pairs_within_buckets(keys: np.ndarray, owners: np.ndarray, max_bucket: int, n: int) -> np.ndarray:
    """Codes ``i * n + j`` (i < j) of owners sharing a key, once per shared key.
    ``n`` is the number of items, not the highest owner seen: the caller decodes
    with it, and the last items may have no keys at all."""
    order = np.lexsort((owners, keys))
    k, o = keys[order], owners[order]
    if len(k) < 2:
        return np.empty(0, dtype=np.int64)
    starts = np.flatnonzero(np.r_[True, k[1:] != k[:-1]])
    sizes = np.diff(np.r_[starts, len(k)])
    keep = (sizes >= 2) & (sizes <= max_bucket)
    out = []
    for s, z in zip(starts[keep].tolist(), sizes[keep].tolist(), strict=True):
        grp = o[s:s + z]
        ii, jj = np.triu_indices(z, 1)
        a, b = grp[ii], grp[jj]
        ok = a != b
        out.append(np.minimum(a, b)[ok] * n + np.maximum(a, b)[ok])
    return np.concatenate(out) if out else np.empty(0, dtype=np.int64)


def audio_pairs(items: list) -> list[tuple[int, int, float, float]]:
    """(id_a, id_b, score, offset in seconds) for matching songs."""
    fps = [np.frombuffer(r["fp"], dtype="<u4") for r in items]
    durs = np.array([r["duration"] or 0.0 for r in items])
    keys, owners = [], []
    for i, fp in enumerate(fps):
        v = np.unique(fp >> np.uint32(_AUDIO_KEY_SHIFT))
        v = v[v % _AUDIO_KEY_SAMPLE == 0]
        keys.append(v.astype(np.int64))
        owners.append(np.full(len(v), i, dtype=np.int64))
    if len(items) < 2:
        return []
    n = len(items)
    codes = _pairs_within_buckets(np.concatenate(keys), np.concatenate(owners), _AUDIO_MAX_BUCKET, n)
    uniq, counts = np.unique(codes, return_counts=True)
    cand = uniq[counts >= _AUDIO_MIN_SHARED]
    out = []
    for code in cand.tolist():
        i, j = divmod(code, n)
        if abs(durs[i] - durs[j]) > AUDIO_DURATION_SLACK or _same(items[i], items[j]):
            continue
        score, off = audio_score(fps[i], fps[j])
        if score >= AUDIO_MIN_SCORE:
            out.append((items[i]["id"], items[j]["id"], round(score, 4), round(off / 8.0, 1)))
    return out


def _frames(fp: bytes) -> np.ndarray:
    return np.frombuffer(fp, dtype=">u8").astype(np.uint64)


def video_pairs(items: list) -> list[tuple[int, int, float, float]]:
    """(id_a, id_b, share of agreeing frames, offset in seconds) for matching videos.
    Videos are sorted by length; each is compared, all at once, with the
    longer ones it could be a trimmed or re-encoded copy of."""
    order = sorted(range(len(items)), key=lambda i: items[i]["duration"] or 0.0)
    f = np.array([_frames(items[i]["fp"]) for i in order], dtype=np.uint64).reshape(len(order), VIDEO_FRAMES)
    durs = np.array([items[i]["duration"] or 0.0 for i in order])
    out = []
    for x in range(len(order) - 1):
        hi = int(np.searchsorted(durs, durs[x] / VIDEO_DURATION_RATIO, side="right"))
        if hi <= x + 1:
            continue
        block = f[x + 1:hi]
        best_share = np.zeros(len(block))
        best_shift = np.zeros(len(block), dtype=np.int64)
        for shift in (0, -1, 1):
            ia = np.arange(max(0, -shift), min(VIDEO_FRAMES, VIDEO_FRAMES - shift))
            a, b = f[x, ia], block[:, ia + shift]
            valid = (a != 0)[None, :] & (b != 0)
            ok = (np.bitwise_count(a[None, :] ^ b) <= FRAME_MAX_DIST) & valid
            nvalid = valid.sum(1)
            share = np.where(nvalid >= VIDEO_MIN_FRAMES, ok.sum(1) / np.maximum(nvalid, 1), 0.0)
            better = share > best_share
            best_share[better], best_shift[better] = share[better], shift
        for k in np.flatnonzero(best_share >= VIDEO_MIN_SHARE).tolist():
            ix, iy = order[x], order[x + 1 + k]
            if _same(items[ix], items[iy]):
                continue
            out.append((items[ix]["id"], items[iy]["id"], round(float(best_share[k]), 4),
                        round(float(best_shift[k]) * durs[x + 1 + k] / VIDEO_FRAMES, 1)))
    return out


def rebuild_pairs(conn: sqlite3.Connection, kinds: list[str]) -> int:
    rows: list[tuple] = []
    for kind in kinds:
        items = _items(conn, kind)
        found = audio_pairs(items) if kind == "audio" else video_pairs(items)
        rows += [(min(a, b), max(a, b), kind, s, o) for a, b, s, o in found]
    with db.Tx(conn):
        conn.execute("DELETE FROM media_pairs")
        conn.executemany("INSERT OR IGNORE INTO media_pairs(a, b, kind, score, offset) VALUES (?, ?, ?, ?, ?)", rows)
    return len(rows)


# -- decisions and groups ------------------------------------------------------------------

MKEY = "(SELECT value FROM hashes WHERE file_id = {f} AND kind = 'mkey')"

PAIRS_WITH_DECISION = f"""
    SELECT mp.a, mp.b, mp.kind, mp.score, mp.offset, rv.decision, rv.decided_at, rv.keep,
           {MKEY.format(f="mp.a")} AS ka, {MKEY.format(f="mp.b")} AS kb
    FROM media_pairs mp
    JOIN files fa ON fa.id = mp.a JOIN files fb ON fb.id = mp.b
    LEFT JOIN media_reviews rv
      ON rv.key_a = MIN({MKEY.format(f="mp.a")}, {MKEY.format(f="mp.b")})
     AND rv.key_b = MAX({MKEY.format(f="mp.a")}, {MKEY.format(f="mp.b")})
    WHERE fa.state = 'present' AND fb.state = 'present'
"""


def pair_key(conn: sqlite3.Connection, a: int, b: int) -> tuple[bytes, bytes] | None:
    ka = conn.execute("SELECT " + MKEY.format(f="?"), (a,)).fetchone()[0]
    kb = conn.execute("SELECT " + MKEY.format(f="?"), (b,)).fetchone()[0]
    if ka is None or kb is None or ka == kb:
        return None
    return (ka, kb) if ka <= kb else (kb, ka)


def decide(conn: sqlite3.Connection, a: int, b: int, decision: str) -> bool:
    """Record a decision on the pair (a, b) as shown: a on the left."""
    if decision not in DECISIONS:
        return False
    if not conn.execute("SELECT 1 FROM media_pairs WHERE a = ? AND b = ?", (min(a, b), max(a, b))).fetchone():
        return False
    key = pair_key(conn, a, b)
    if key is None:
        return False
    keep = None
    if decision in ("keep_a", "keep_b"):
        keep = conn.execute("SELECT " + MKEY.format(f="?"), (a if decision == "keep_a" else b,)).fetchone()[0]
    with db.Tx(conn):
        conn.execute("INSERT OR REPLACE INTO media_reviews(key_a, key_b, decision, keep, decided_at) "
                     "VALUES (?, ?, ?, ?, ?)", (key[0], key[1], decision, keep, time.time()))
    return True


def undo(conn: sqlite3.Connection, a: int, b: int) -> None:
    key = pair_key(conn, a, b)
    if key:
        with db.Tx(conn):
            conn.execute("DELETE FROM media_reviews WHERE key_a = ? AND key_b = ?", key)


def keeper_rank(r) -> tuple:
    """The copy to keep: lossless before lossy, then the higher resolution
    (videos) or bitrate, then the longer, then the more complete tags."""
    codec = (r["vcodec"] if r["kind"] == "video" else r["acodec"]) or ""
    lossless = codec in LOSSLESS or codec.startswith("pcm_")
    pixels = (r["width"] or 0) * (r["height"] or 0)
    return (lossless, pixels, r["bitrate"] or 0, round(r["seconds"] or 0), r["tag_count"] or 0, r["size"])


def rebuild_groups(conn: sqlite3.Connection) -> dict:
    """Join files linked by undecided or "keep one" pairs into groups."""
    edges = [r for r in conn.execute(PAIRS_WITH_DECISION) if r["decision"] not in _UNLINKED]
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in edges:
        ra, rb = find(e["a"]), find(e["b"])
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    comps: dict[int, list[int]] = defaultdict(list)
    for x in list(parent):
        comps[find(x)].append(x)
    pending: dict[int, int] = defaultdict(int)
    best: dict[int, float] = {}
    for e in edges:
        c = find(e["a"])
        if e["decision"] in (None, "later"):
            pending[c] += 1
        best[c] = max(best.get(c, 0.0), e["score"])
    now = time.time()
    groups = wasted = 0
    with db.Tx(conn):
        conn.execute("DELETE FROM groups WHERE method = 'media_similar'")
        for root, members in comps.items():
            if len(members) < 2:
                continue
            marks = ",".join("?" * len(members))
            info = conn.execute(
                f"""SELECT f.id, f.size, f.ext, f.dev, f.inode, av.kind, av.vcodec, av.acodec, av.width, av.height,
                           av.bitrate, av.duration AS seconds, av.tags AS tag_count
                    FROM files f JOIN av ON av.file_id = f.id WHERE f.id IN ({marks})""",
                members,
            ).fetchall()
            if len(info) < 2:
                continue
            phys = {(r["dev"], r["inode"]): r for r in info}
            keeper = max(info, key=keeper_rank)
            w = max(sum(r["size"] for r in phys.values()) - keeper["size"], 0)
            cur = conn.execute(
                "INSERT INTO groups(method, key, size, file_count, copies, wasted, category, built_at, score, pending) "
                "VALUES ('media_similar', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"m{root}", keeper["size"], len(info), len(phys), w, group_category([r["ext"] for r in info]), now,
                 int(round(100 * best.get(root, 0.0))), pending.get(root, 0)))
            conn.executemany("INSERT INTO group_members(group_id, file_id) VALUES (?, ?)",
                             [(cur.lastrowid, r["id"]) for r in info])
            groups += 1
            wasted += w
    return {"groups": groups, "wasted": wasted}


def queue(conn: sqlite3.Connection, decided: bool = False, limit: int = 200) -> list[sqlite3.Row]:
    """Pairs to review (closest first), or the decided ones (latest first)."""
    if decided:
        sql = PAIRS_WITH_DECISION + " AND rv.decision IS NOT NULL AND rv.decision != 'later' ORDER BY rv.decided_at DESC LIMIT ?"
    else:
        sql = (PAIRS_WITH_DECISION + " AND (rv.decision IS NULL OR rv.decision = 'later') "
               "ORDER BY rv.decision IS NOT NULL, mp.score DESC, mp.a, mp.b LIMIT ?")
    return conn.execute(sql, (limit,)).fetchall()


def counts(conn: sqlite3.Connection) -> dict:
    r = conn.execute(f"""SELECT COUNT(*) AS pairs, SUM(decision IS NULL OR decision = 'later') AS open
                         FROM ({PAIRS_WITH_DECISION})""").fetchone()
    pairs, open_ = r["pairs"] or 0, r["open"] or 0
    return {"pairs": pairs, "open": open_, "decided": pairs - open_}


def removals(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """(remove, keep) pairs from "Keep left" / "Keep right" decisions."""
    out = []
    for r in conn.execute(PAIRS_WITH_DECISION + " AND rv.decision IN ('keep_a', 'keep_b')"):
        if r["keep"] == r["ka"] and r["keep"] != r["kb"]:
            out.append((r["b"], r["a"]))
        elif r["keep"] == r["kb"] and r["keep"] != r["ka"]:
            out.append((r["a"], r["b"]))
    return out
