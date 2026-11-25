"""Similar photos: perceptual hashes, candidate pairs and review decisions.

Two photos are *similar* when they look the same but their image data
differs: a recompressed copy (Google Photos "Storage saver"), a resized
export, a copy saved by an editor. Each photo gets two 64-bit perceptual
hashes, computed on a small greyscale version with its EXIF rotation
applied:

* pHash: the 8x8 lowest frequencies of a 32x32 DCT, each bit telling
  whether the coefficient is above the median.
* dHash: each bit telling whether a pixel of a 9x8 version is brighter than
  its right-hand neighbour.

Pairs whose pHash differ in at most ``MAX_DISTANCE`` bits are stored; the
threshold set in the app (default 8) decides which of them are shown. Pairs
are found with multi-index hashing: the 64 bits are split into four 16-bit
parts, and two hashes within distance 10 must agree on at least one part up
to 2 bits, so only those buckets are compared (roughly one to two minutes per
million photos).

Similar matches are suggestions. They go to the review queue, and nothing
is counted as reclaimable until a person has looked at them. A decision is
stored against the two photos' image-data hashes, so it survives rescans,
renames and moves, and a pair marked "not the same photo" never comes back.
"""

from __future__ import annotations

import io
import itertools
import sqlite3
import time
from collections import defaultdict

import numpy as np

from . import db

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    Image = None

# Largest distance stored; the threshold slider goes up to here.
MAX_DISTANCE = 10
DEFAULT_THRESHOLD = 8
_PARTS = 4
_PART_RADIUS = MAX_DISTANCE // _PARTS  # 2: by pigeonhole, one part is this close
# Near-uniform images (black frames, blank scans) all hash alike; skip them.
_MIN_BITS, _MAX_BITS = 4, 60
# A bucket this full is a degenerate hash shared by many unrelated images.
_MAX_BUCKET = 400

DECISIONS = {
    "keep_a": "Same photo, keep left",
    "keep_b": "Same photo, keep right",
    "both": "Same photo, keep both",
    "not_dup": "Not the same photo",
    "later": "Decide later",
}
# Decisions that take a pair out of the similar groups.
_UNLINKED = ("both", "not_dup")


# -- perceptual hashes ---------------------------------------------------------------


def _dct_matrix(n: int) -> np.ndarray:
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    m = np.cos(np.pi * (2 * i + 1) * k / (2 * n)) * np.sqrt(2 / n)
    m[0] /= np.sqrt(2)
    return m


_DCT32 = _dct_matrix(32)


def _pack(bits: np.ndarray) -> bytes:
    return np.packbits(bits.astype(np.uint8)).tobytes()


def hashes_from_image(im) -> dict[str, bytes]:
    """pHash and dHash of an opened Pillow image."""
    if getattr(im, "format", None) == "JPEG":
        im.draft("L", (256, 256))  # decode at a fraction of the size: much faster
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("L", "RGB"):
        im = im.convert("RGBA").convert("RGB") if "A" in im.mode or im.mode == "P" else im.convert("RGB")
    g = im.convert("L")
    g.thumbnail((256, 256), Image.Resampling.BILINEAR)
    a = np.asarray(g.resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float64)
    low = (_DCT32 @ a @ _DCT32.T)[:8, :8].flatten()
    p = low > np.median(low[1:])  # the DC term would skew the median
    s = np.asarray(g.resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
    d = (s[:, 1:] > s[:, :-1]).flatten()
    return {"phash": _pack(p), "dhash": _pack(d)}


def similar_hashes(data: bytes) -> dict[str, bytes]:
    """pHash and dHash of an encoded image, or ``sim_none`` with the reason."""
    if Image is None:  # pragma: no cover
        return {"sim_none": b"Pillow is not installed"}
    try:
        with Image.open(io.BytesIO(data)) as im:
            return hashes_from_image(im)
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as e:
        return {"sim_none": f"cannot decode: {e}".encode()[:300]}


def distance(a: bytes, b: bytes) -> int:
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).bit_count()


# -- candidate pairs -----------------------------------------------------------------


def _popcount64(x: np.ndarray) -> np.ndarray:
    x = x - ((x >> np.uint64(1)) & np.uint64(0x5555555555555555))
    x = (x & np.uint64(0x3333333333333333)) + ((x >> np.uint64(2)) & np.uint64(0x3333333333333333))
    x = (x + (x >> np.uint64(4))) & np.uint64(0x0F0F0F0F0F0F0F0F)
    return ((x * np.uint64(0x0101010101010101)) >> np.uint64(56)).astype(np.int64)


def _masks16(radius: int) -> list[int]:
    out = [0]
    for r in range(1, radius + 1):
        for bits in itertools.combinations(range(16), r):
            out.append(sum(1 << b for b in bits))
    return out


def near_pairs(hashes: np.ndarray, max_distance: int = MAX_DISTANCE) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """All index pairs ``i < j`` with Hamming distance at most ``max_distance``.

    ``hashes`` is a uint64 array. Returns (i, j, distance).
    """
    n = len(hashes)
    if n < 2:
        empty = np.empty(0, dtype=np.int64)
        return empty, empty, empty
    radius = max_distance // _PARTS
    masks = _masks16(radius)
    found_i, found_j = [], []
    for part in range(_PARTS):
        seg = ((hashes >> np.uint64(16 * part)) & np.uint64(0xFFFF)).astype(np.int64)
        order = np.argsort(seg, kind="stable")
        # 16-bit buckets: where each value starts in ``order`` and how many share it
        bucket_n = np.bincount(seg, minlength=1 << 16)
        bucket_start = np.cumsum(bucket_n) - bucket_n
        bucket_n[bucket_n > _MAX_BUCKET] = 0
        for m in masks:
            keys = seg ^ m
            left = bucket_start[keys]
            counts = bucket_n[keys]
            total = int(counts.sum())
            if total == 0:
                continue
            qi = np.repeat(np.arange(n), counts)
            # position inside each run: 0..count-1
            starts = np.repeat(left, counts)
            offs = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)
            qj = order[starts + offs]
            keep = qi < qj
            qi, qj = qi[keep], qj[keep]
            if len(qi) == 0:
                continue
            d = _popcount64(hashes[qi] ^ hashes[qj])
            ok = d <= max_distance
            found_i.append(qi[ok])
            found_j.append(qj[ok])
    if not found_i:
        empty = np.empty(0, dtype=np.int64)
        return empty, empty, empty
    pi = np.concatenate(found_i)
    pj = np.concatenate(found_j)
    uniq = np.unique(pi * n + pj)
    pi, pj = uniq // n, uniq % n
    return pi, pj, _popcount64(hashes[pi] ^ hashes[pj])


def rebuild_pairs(conn: sqlite3.Connection) -> int:
    """Recompute ``similar_pairs`` from the stored perceptual hashes.

    Pairs between two loose photos go to the review queue; pairs between a
    loose photo and an archive member feed the archive coverage report.
    Copies with the same image data are already exact or same-photo
    duplicates and are left out.
    """
    rows = conn.execute(
        """SELECT f.id, f.container_id, f.dev, f.inode, p.value AS ph, d.value AS dh,
                  COALESCE(ie.value, b3.value) AS ident
           FROM files f JOIN roots r ON r.id = f.root_id
           JOIN hashes p ON p.file_id = f.id AND p.kind = 'phash'
           JOIN hashes d ON d.file_id = f.id AND d.kind = 'dhash'
           LEFT JOIN hashes ie ON ie.file_id = f.id AND ie.kind = 'img_exact'
           LEFT JOIN hashes b3 ON b3.file_id = f.id AND b3.kind = 'blake3'
           WHERE f.state = 'present' AND r.enabled = 1"""
    ).fetchall()
    ids, loose, phys, ph, dh, ident = [], [], [], [], [], []
    for r in rows:
        v = int.from_bytes(r["ph"], "big")
        if not _MIN_BITS <= v.bit_count() <= _MAX_BITS:
            continue
        ids.append(r["id"])
        loose.append(r["container_id"] == 0)
        phys.append((r["dev"], r["inode"]) if r["container_id"] == 0 else ("m", r["id"]))
        ph.append(v)
        dh.append(int.from_bytes(r["dh"], "big"))
        ident.append(r["ident"])
    hashes = np.array(ph, dtype=np.uint64)
    pi, pj, pd = near_pairs(hashes)
    out = []
    for i, j, d in zip(pi.tolist(), pj.tolist(), pd.tolist(), strict=True):
        if not (loose[i] or loose[j]):
            continue  # two archive members: not actionable, and Takeouts repeat a lot
        if phys[i] == phys[j] or (ident[i] is not None and ident[i] == ident[j]):
            continue
        a, b = (ids[i], ids[j]) if ids[i] < ids[j] else (ids[j], ids[i])
        out.append((a, b, d, (dh[i] ^ dh[j]).bit_count()))
    with db.Tx(conn):
        conn.execute("DELETE FROM similar_pairs")
        conn.executemany("INSERT OR IGNORE INTO similar_pairs(a, b, pdist, ddist) VALUES (?, ?, ?, ?)", out)
    return len(out)


# -- decisions and groups --------------------------------------------------------------

# The identity a decision is stored against: the image data, so a renamed or
# moved photo keeps its verdict.
IDENT = "(SELECT COALESCE(MAX(CASE WHEN kind = 'img_exact' THEN value END), MAX(CASE WHEN kind = 'blake3' THEN value END)) FROM hashes WHERE file_id = {f} AND kind IN ('img_exact', 'blake3'))"


def pair_key(conn: sqlite3.Connection, a: int, b: int) -> tuple[bytes, bytes] | None:
    ka = conn.execute("SELECT " + IDENT.format(f="?"), (a,)).fetchone()[0]
    kb = conn.execute("SELECT " + IDENT.format(f="?"), (b,)).fetchone()[0]
    if ka is None or kb is None:
        return None
    return (ka, kb) if ka <= kb else (kb, ka)


def decide(conn: sqlite3.Connection, a: int, b: int, decision: str) -> bool:
    """Record a decision on the pair (a, b) as shown: a on the left."""
    if decision not in DECISIONS:
        return False
    key = pair_key(conn, a, b)
    if key is None:
        return False
    keep = None
    if decision in ("keep_a", "keep_b"):
        keeper = a if decision == "keep_a" else b
        keep = conn.execute("SELECT " + IDENT.format(f="?"), (keeper,)).fetchone()[0]
    with db.Tx(conn):
        conn.execute(
            "INSERT OR REPLACE INTO reviews(key_a, key_b, decision, keep, decided_at) VALUES (?, ?, ?, ?, ?)",
            (key[0], key[1], decision, keep, time.time()),
        )
    return True


def undo(conn: sqlite3.Connection, a: int, b: int) -> None:
    key = pair_key(conn, a, b)
    if key:
        with db.Tx(conn):
            conn.execute("DELETE FROM reviews WHERE key_a = ? AND key_b = ?", key)


PAIRS_WITH_DECISION = f"""
    SELECT sp.a, sp.b, sp.pdist, sp.ddist, rv.decision, rv.decided_at, rv.keep,
           {IDENT.format(f="sp.a")} AS ka, {IDENT.format(f="sp.b")} AS kb
    FROM similar_pairs sp
    JOIN files fa ON fa.id = sp.a JOIN files fb ON fb.id = sp.b
    LEFT JOIN reviews rv
      ON rv.key_a = MIN({IDENT.format(f="sp.a")}, {IDENT.format(f="sp.b")})
     AND rv.key_b = MAX({IDENT.format(f="sp.a")}, {IDENT.format(f="sp.b")})
    WHERE sp.pdist <= :t AND fa.container_id = 0 AND fb.container_id = 0
"""


def rebuild_similar_groups(conn: sqlite3.Connection, threshold: int) -> dict:
    """Group loose photos joined by similar pairs within the threshold, leaving
    out pairs marked "not the same photo" or "keep both"."""
    from .categories import group_category

    edges = [r for r in conn.execute(PAIRS_WITH_DECISION, {"t": threshold})
             if r["decision"] not in _UNLINKED]
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
    open_by_comp: dict[int, int] = defaultdict(int)
    best_by_comp: dict[int, int] = {}
    for e in edges:
        c = find(e["a"])
        if e["decision"] in (None, "later"):
            open_by_comp[c] += 1
        best_by_comp[c] = min(best_by_comp.get(c, 99), e["pdist"])
    now = time.time()
    groups = wasted = files = 0
    with db.Tx(conn):
        conn.execute("DELETE FROM groups WHERE method = 'similar'")
        for root, members in comps.items():
            if len(members) < 2:
                continue
            marks = ",".join("?" * len(members))
            info = conn.execute(
                f"""SELECT f.id, f.size, f.ext, f.dev, f.inode, md.width, md.height, md.taken_at
                    FROM files f LEFT JOIN media md ON md.file_id = f.id WHERE f.id IN ({marks})""",
                members,
            ).fetchall()
            phys = {(r["dev"], r["inode"]): r for r in info}
            keeper = max(info, key=keeper_rank)
            w = sum(r["size"] for r in phys.values()) - keeper["size"]
            cur = conn.execute(
                "INSERT INTO groups(method, key, size, file_count, copies, wasted, category, built_at, score, "
                "pending) VALUES ('similar', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"s{root}", keeper["size"], len(info), len(phys), max(w, 0),
                 group_category([r["ext"] for r in info]), now, best_by_comp.get(root, 0),
                 open_by_comp.get(root, 0)),
            )
            conn.executemany("INSERT INTO group_members(group_id, file_id) VALUES (?, ?)",
                             [(cur.lastrowid, r["id"]) for r in info])
            groups += 1
            wasted += max(w, 0)
            files += len(info)
    return {"groups": groups, "wasted": wasted, "files": files}


def keeper_rank(r) -> tuple:
    """The copy to keep: highest resolution, then largest file, then the
    earliest date taken."""
    pixels = (r["width"] or 0) * (r["height"] or 0)
    taken = r["taken_at"] or "9999"
    return (pixels, r["size"], [-ord(c) for c in taken])


def queue(conn: sqlite3.Connection, threshold: int, decided: bool = False, limit: int = 200) -> list[sqlite3.Row]:
    """Pairs to review (closest first), or the decided ones (latest first)."""
    if decided:
        sql = PAIRS_WITH_DECISION + " AND rv.decision IS NOT NULL AND rv.decision != 'later' ORDER BY rv.decided_at DESC LIMIT :n"
    else:
        sql = (PAIRS_WITH_DECISION + " AND (rv.decision IS NULL OR rv.decision = 'later') "
               "ORDER BY rv.decision IS NOT NULL, sp.pdist, sp.ddist, sp.a, sp.b LIMIT :n")
    return conn.execute(sql, {"t": threshold, "n": limit}).fetchall()


def counts(conn: sqlite3.Connection, threshold: int) -> dict:
    r = conn.execute(
        f"""SELECT COUNT(*) AS pairs,
                   SUM(decision IS NULL OR decision = 'later') AS open,
                   SUM(decision = 'later') AS later
            FROM ({PAIRS_WITH_DECISION})""",
        {"t": threshold},
    ).fetchone()
    return {"pairs": r["pairs"] or 0, "open": r["open"] or 0, "later": r["later"] or 0,
            "decided": (r["pairs"] or 0) - (r["open"] or 0)}
