import io
import random
import zipfile

import numpy as np
import pytest
from PIL import Image, ImageDraw

from deedoop import db, similar
from deedoop.similar import distance, near_pairs, similar_hashes
from tests.conftest import scan, write


def scene(seed: int, size=(1600, 1200)) -> Image.Image:
    """A photo-like picture: smooth shapes, which perceptual hashes are made for."""
    r = random.Random(seed)
    im = Image.new("RGB", size, tuple(r.randrange(256) for _ in range(3)))
    d = ImageDraw.Draw(im)
    w, h = size
    for _ in range(25):
        x, y, s = r.randrange(w), r.randrange(h), r.randrange(w // 30, w // 4)
        d.ellipse((x, y, x + s, y + s), fill=tuple(r.randrange(256) for _ in range(3)))
    return im


def jpg(im: Image.Image, quality=92) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def saver(im: Image.Image) -> bytes:
    """What Google "Storage saver" does: smaller and harder compressed."""
    return jpg(im.resize((im.width // 2, im.height // 2)), quality=55)


# -- hashes and pairs ------------------------------------------------------------


def test_perceptual_hashes_survive_resizing_and_recompression():
    a = similar_hashes(jpg(scene(1)))
    b = similar_hashes(saver(scene(1)))
    c = similar_hashes(jpg(scene(2)))
    assert set(a) == {"phash", "dhash"} and len(a["phash"]) == 8
    assert distance(a["phash"], b["phash"]) <= 4
    assert distance(a["dhash"], b["dhash"]) <= 6
    assert distance(a["phash"], c["phash"]) > 12


def test_png_and_rotation_tag():
    im = scene(3)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    assert distance(similar_hashes(buf.getvalue())["phash"], similar_hashes(jpg(im))["phash"]) <= 4
    # a copy stored sideways with an EXIF rotation tag is the same photo once rotated
    ex = Image.Exif()
    ex[0x0112] = 6  # rotate 90 CW to display
    side = io.BytesIO()
    im.transpose(Image.Transpose.ROTATE_90).save(side, "JPEG", quality=90, exif=ex.tobytes())
    assert distance(similar_hashes(side.getvalue())["phash"], similar_hashes(jpg(im))["phash"]) <= 4


def test_garbage_gets_a_reason():
    assert "sim_none" in similar_hashes(b"not an image")


def test_near_pairs_matches_brute_force():
    rng = np.random.default_rng(7)
    n = 1500
    h = rng.integers(0, 2**63, n, dtype=np.uint64) * np.uint64(2)
    for k in range(0, 600, 2):  # plant pairs at every distance 0..14
        flips = rng.choice(64, size=k % 15, replace=False)
        h[k + 1] = h[k] ^ np.uint64(sum(1 << int(f) for f in flips))
    pi, pj, pd = near_pairs(h, 10)
    got = {(int(a), int(b), int(d)) for a, b, d in zip(pi, pj, pd, strict=True)}
    vals = [int(x) for x in h]
    want = {(i, j, (vals[i] ^ vals[j]).bit_count()) for i in range(n) for j in range(i + 1, n)
            if (vals[i] ^ vals[j]).bit_count() <= 10}
    assert got == want and len(want) >= 200


# -- scan, groups, review --------------------------------------------------------------


@pytest.fixture
def photos(unraid, conn):
    m = unraid.mnt
    write(m / "disk1/Photos/2019/IMG_0001.JPG", jpg(scene(1)))
    write(m / "disk1/Photos/2019/IMG_0002.JPG", jpg(scene(2)))
    write(m / "disk1/Photos/2019/IMG_0003.JPG", jpg(scene(3)))
    write(m / "disk2/Photos/google/IMG_0001.jpg", saver(scene(1)))  # Storage saver copy
    write(m / "disk2/Photos/google/edit.jpg", saver(scene(2)))
    write(m / "disk2/Photos/google/other.jpg", jpg(scene(9)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Takeout/Google Photos/2019/IMG_0003.JPG", saver(scene(3)))
        z.writestr("Takeout/Google Photos/2019/new.jpg", jpg(scene(11)))
    write(m / "disk2/Photos/Takeout/takeout-20240101T000000Z-001.zip", buf.getvalue())
    db.set_settings(conn, {"shares": ["Photos"], "takeout_paths": [str(m / "user/Photos/Takeout")]})
    return m


def names(conn, a, b):
    q = "SELECT name FROM files WHERE id = ?"
    return {conn.execute(q, (a,)).fetchone()[0], conn.execute(q, (b,)).fetchone()[0]}


def test_scan_finds_similar_pairs_groups_and_archive_coverage(unraid, conn, photos):
    s = scan(unraid)
    pairs = conn.execute("SELECT * FROM similar_pairs").fetchall()
    loose = [p for p in pairs if names(conn, p["a"], p["b"]) <= {"IMG_0001.JPG", "IMG_0001.jpg", "IMG_0002.JPG",
                                                                    "edit.jpg"}]
    assert len(loose) == 2 and all(p["pdist"] <= 4 for p in loose)
    assert s["similar_groups"] == 2
    g = conn.execute("SELECT * FROM groups WHERE method = 'similar' ORDER BY wasted DESC").fetchall()
    assert all(x["pending"] == 1 and x["copies"] == 2 for x in g)
    # the original is the suggested keeper, so the small copy is what could go
    small = conn.execute("SELECT size FROM files WHERE name = 'IMG_0001.jpg'").fetchone()[0]
    assert small in {x["wasted"] for x in g}
    # suggestions are not counted in the dashboard total
    from deedoop.queries import index_stats

    st = index_stats(conn, unraid.db_path)
    assert st["total_wasted"] == 0 and st["similar_pending"] == 2
    # the Takeout's resized IMG_0003 counts as "similar photo on disk"
    a = conn.execute("SELECT * FROM archives").fetchone()
    assert (a["cov_similar"], a["cov_new"], a["version"]) == (1, 1, 2)


def test_review_decisions_survive_rescans_and_renames(unraid, conn, photos):
    scan(unraid)
    t = 8
    first = similar.queue(conn, t)[0]
    assert similar.decide(conn, first["a"], first["b"], "not_dup")
    similar.rebuild_similar_groups(conn, t)
    assert similar.counts(conn, t)["open"] == 1
    assert conn.execute("SELECT COUNT(*) FROM groups WHERE method = 'similar'").fetchone()[0] == 1
    # rename one photo of the rejected pair: the verdict follows the image data
    gone = names(conn, first["a"], first["b"])
    src = next(p for p in (photos / "disk1/Photos/2019").iterdir() if p.name in gone)
    src.rename(src.with_name("renamed.JPG"))
    scan(unraid)
    assert similar.counts(conn, t) == {"pairs": 2, "open": 1, "later": 0, "decided": 1}
    nxt = similar.queue(conn, t)[0]
    similar.decide(conn, nxt["a"], nxt["b"], "keep_a")
    similar.rebuild_similar_groups(conn, t)
    assert conn.execute("SELECT pending FROM groups WHERE method = 'similar'").fetchone()[0] == 0
    similar.undo(conn, nxt["a"], nxt["b"])
    assert similar.counts(conn, t)["open"] == 1


def test_threshold_and_backfill(unraid, conn, photos):
    db.set_settings(conn, {"similar_matching": False})
    scan(unraid)
    loose_phash = ("SELECT COUNT(*) FROM hashes h JOIN files f ON f.id = h.file_id "
                   "WHERE h.kind = 'phash' AND f.container_id = 0")
    assert conn.execute(loose_phash).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM groups WHERE method = 'similar'").fetchone()[0] == 0
    # an archive indexed before milestone 4 is read once more
    conn.execute("UPDATE archives SET version = 1, indexed_at = 0")
    db.set_settings(conn, {"similar_matching": True, "similar_threshold": 0})
    scan(unraid)  # photos already hashed by the image pass get their perceptual hashes now
    assert conn.execute(loose_phash).fetchone()[0] == 6
    a = conn.execute("SELECT version, indexed_at FROM archives").fetchone()
    assert a["version"] == 2 and a["indexed_at"] > 0
    strict = similar.counts(conn, 0)["pairs"]
    assert strict < similar.counts(conn, 10)["pairs"]


def test_review_pages(unraid, conn, photos):
    from tests.test_web import make_client

    scan(unraid)
    with make_client(unraid) as client:
        r = client.get("/review")
        assert r.status_code == 200 and "suggested keeper" in r.text and "Keep left" in r.text
        assert "pHash distance" in r.text
        pair = similar.queue(conn, 8)[0]
        r = client.post("/review/decide", data={"a": pair["a"], "b": pair["b"], "decision": "keep_a"})
        assert r.status_code == 200  # redirected to the next pair
        r = client.get("/review?view=decided")
        assert "Same photo, keep" in r.text and "Undo" in r.text
        r = client.post("/review/threshold", data={"threshold": "3"})
        assert db.get_settings(conn)["similar_threshold"] == 3
        r = client.get("/duplicates?method=similar")
        assert r.status_code == 200 and "Similar photos" in r.text
        fid = conn.execute("SELECT id FROM files WHERE name = 'IMG_0002.JPG'").fetchone()[0]
        r = client.get(f"/file/{fid}")
        assert "Similar photos" in r.text and "edit.jpg" in r.text
        aid = conn.execute("SELECT file_id FROM archives").fetchone()[0]
        r = client.get(f"/archives/{aid}?status=similar")
        assert "IMG_0003.JPG" in r.text
        assert client.get("/").status_code == 200
