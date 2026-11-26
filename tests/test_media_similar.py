"""Milestone 8: re-encoded songs and videos (Chromaprint and 12-frame
fingerprints), the media review queue and its decisions."""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from deedup import av, changes, db, media_similar, queries, scanners
from deedup.engine import regroup
from tests.conftest import scan

needs_ffmpeg = pytest.mark.skipif(not av.tools_available(), reason="ffmpeg/ffprobe not installed")
needs_chromaprint = pytest.mark.skipif(not (av.tools_available() and av.chromaprint_available()),
                                       reason="ffmpeg without Chromaprint")


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", *args], check=True)


def tune(path: Path, seed: int, color: str = "pink", base: int = 220, step: int = 3, seconds: int = 30) -> Path:
    """Something with enough going on to fingerprint like music: coloured noise and a melody."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ff("-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c={color}:seed={seed}:a=0.3",
       "-f", "lavfi", "-i", f"aevalsrc='0.5*sin(2*PI*t*({base}*pow(2,floor(mod(t*{step},12))/12)))':d={seconds}",
       "-filter_complex", "[0][1]amix=inputs=2", "-ar", "44100", "-ac", "2", str(path))
    return path


def encode(src: Path, dst: Path, *args: str) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    ff("-i", str(src), *args, str(dst))
    return dst


def film(path: Path, seconds: int = 8, pattern: str = "life=s=320x240:mold=10:r=25:ratio=0.1:seed=7") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ff("-f", "lavfi", "-i", pattern, "-f", "lavfi", "-i", "anoisesrc=c=pink:a=0.1", "-t", str(seconds),
       "-vf", "scale=640:480", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-shortest",
       str(path))
    return path


@pytest.fixture
def library(unraid, conn):
    m = unraid.mnt
    (m / "disk1/Docs").mkdir(parents=True, exist_ok=True)
    (m / "disk2/Docs").mkdir(parents=True, exist_ok=True)
    if av.chromaprint_available():
        a = tune(m / "disk1/Docs/Music/Lanterns/Low Tide.flac", seed=3)
        encode(a, m / "disk2/Docs/iPod/Track 01.mp3", "-c:a", "libmp3lame", "-b:a", "128k")
        tune(m / "disk1/Docs/Music/Other/Paper Boats.flac", seed=9, color="brown", base=330, step=5)
    v = film(m / "disk1/Docs/Videos/VID_0001.mp4")
    encode(v, m / "disk2/Docs/WhatsApp/VID-WA0001.mp4", "-vf", "scale=320:240", "-c:v", "libx264", "-crf", "32",
           "-c:a", "aac", "-b:a", "48k")
    film(m / "disk1/Docs/Videos/VID_0002.mp4", pattern="testsrc2=s=320x240:r=25")
    db.set_settings(conn, {"shares": ["Docs"]})
    return m


def pairs(conn):
    return {(r["kind"], frozenset((r["na"], r["nb"]))) for r in conn.execute(
        "SELECT mp.kind, fa.name AS na, fb.name AS nb FROM media_pairs mp "
        "JOIN files fa ON fa.id = mp.a JOIN files fb ON fb.id = mp.b")}


def fid(conn, name):
    return conn.execute("SELECT id FROM files WHERE name = ?", (name,)).fetchone()[0]


# -- no ffmpeg needed ---------------------------------------------------------------------


def test_audio_score_finds_the_offset():
    rng = np.random.default_rng(1)
    a = rng.integers(0, 2**32, 900, dtype=np.uint64).astype(np.uint32)
    b = a[16:].copy()  # 2 s cut from the start
    b[::7] ^= np.uint32(0x00F0)  # a few bits flipped by the re-encode
    score, off = media_similar.audio_score(a, b)
    assert off == 16 and score > 0.97
    other = rng.integers(0, 2**32, 900, dtype=np.uint64).astype(np.uint32)
    assert media_similar.audio_score(a, other)[0] < 0.6


def test_blank_frames_are_left_out():
    assert media_similar.phash_gray(np.full((32, 32), 12, dtype=np.uint8)) == 0
    rng = np.random.default_rng(2)
    assert media_similar.phash_gray(rng.integers(0, 255, (32, 32), dtype=np.uint8)) != 0


def test_pairs_within_buckets():
    keys = np.array([5, 5, 5, 9, 9, 7], dtype=np.int64)
    owners = np.array([0, 1, 2, 1, 3, 0], dtype=np.int64)
    codes = sorted(media_similar._pairs_within_buckets(keys, owners, 64, 4).tolist())
    n = 4
    assert codes == sorted([0 * n + 1, 0 * n + 2, 1 * n + 2, 1 * n + 3])
    assert media_similar._pairs_within_buckets(keys, owners, 2, 4).tolist() == [1 * n + 3]  # the full bucket is skipped


def test_audio_pairs_when_the_last_songs_have_no_keys():
    """Regression: pair codes were built with the highest owner seen, but read
    back with the number of songs, so a match was lost whenever the last songs
    in the list (short clips) had no index keys."""
    rng = np.random.default_rng(5)
    song = rng.integers(0, 2**32, 900, dtype=np.uint64).astype(np.uint32)
    copy = song.copy()
    copy[::9] ^= np.uint32(0x0101)
    def row(i, fp, d):
        return {"id": i, "dev": 1, "inode": i, "duration": d, "fp": fp.astype("<u4").tobytes(), "b3": None, "sh": None}
    blank = np.zeros(80, dtype=np.uint32) + np.uint32(1 << 12)  # one key, never a multiple of the sample
    items = [row(1, blank, 10.0), row(2, song, 112.0), row(3, copy, 112.0), row(4, blank + 1, 10.0)]
    assert [(a, b) for a, b, _, _ in media_similar.audio_pairs(items)] == [(2, 3)]


def test_keeper_prefers_lossless_then_quality():
    def r(**kw):
        return {"kind": "audio", "vcodec": None, "acodec": "mp3", "width": None, "height": None, "bitrate": 128000,
                "seconds": 200, "tag_count": 5, "size": 3, **kw}
    assert media_similar.keeper_rank(r(acodec="flac", bitrate=900000, tag_count=0)) > media_similar.keeper_rank(r())
    assert media_similar.keeper_rank(r(bitrate=320000)) > media_similar.keeper_rank(r(tag_count=9))
    v = {"kind": "video", "acodec": "aac", "bitrate": 1, "seconds": 8, "tag_count": 0, "size": 1}
    big = {**v, "vcodec": "h264", "width": 1920, "height": 1080}
    small = {**v, "vcodec": "h264", "width": 640, "height": 360, "bitrate": 9}
    assert media_similar.keeper_rank(big) > media_similar.keeper_rank(small)


def test_plan_and_methods_follow_the_switch():
    s = dict(db.DEFAULT_SETTINGS)
    s["scanners"] = {"photo": False, "video": True, "audio": False, "archive": False}
    plan = scanners.plan(s)
    assert "fp_video" in plan and "fp_audio" not in plan
    assert "media_similar" in scanners.methods(s) or not av.tools_available()
    s["similar_media"] = False
    assert "fp_video" not in scanners.plan(s) and "media_similar" not in scanners.methods(s)


# -- with ffmpeg ----------------------------------------------------------------------------


@needs_ffmpeg
def test_re_encoded_copies_are_paired_and_grouped(unraid, conn, library):
    res = scan(unraid)
    found = pairs(conn)
    assert ("video", frozenset({"VID_0001.mp4", "VID-WA0001.mp4"})) in found
    assert not any("VID_0002.mp4" in p for _, p in found)
    if av.chromaprint_available():
        assert ("audio", frozenset({"Low Tide.flac", "Track 01.mp3"})) in found
        assert not any("Paper Boats.flac" in p for _, p in found)
    assert res["media_similar_groups"] == len(found)
    # Suggestions only: not counted in the reclaimable total.
    st = queries.index_stats(conn, unraid.db_path)
    assert st["media_similar_groups"] == len(found) and st["media_similar_pending"] == len(found)
    assert st["total_wasted"] == st["wasted"] + st["image_wasted"] + st["media_wasted"]
    # The suggested keeper is the higher resolution video (and the lossless song).
    gid = conn.execute("SELECT g.id FROM groups g JOIN group_members m ON m.group_id = g.id "
                       "WHERE g.method = 'media_similar' AND m.file_id = ?", (fid(conn, "VID_0001.mp4"),)).fetchone()[0]
    members = queries.group_members(conn, [gid])[gid]
    assert next(m for m in members if m["keeper"])["user_path"].endswith("VID_0001.mp4")
    assert {"dims", "kbps"} <= members[0]["differs"]
    # Fingerprints are made once: a second scan reads nothing again.
    before = conn.execute("SELECT COUNT(*) FROM hashes WHERE kind IN ('afp', 'vfp')").fetchone()[0]
    scan(unraid)
    assert conn.execute("SELECT COUNT(*) FROM hashes WHERE kind IN ('afp', 'vfp')").fetchone()[0] == before


@needs_ffmpeg
def test_decisions_survive_a_rescan_and_feed_changes(unraid, conn, library):
    scan(unraid)
    a, b = sorted((fid(conn, "VID_0001.mp4"), fid(conn, "VID-WA0001.mp4")))
    keep_left = "keep_a" if a == fid(conn, "VID_0001.mp4") else "keep_b"
    assert media_similar.decide(conn, a, b, keep_left)
    media_similar.rebuild_groups(conn)
    assert media_similar.counts(conn)["decided"] == 1
    s = db.get_settings(conn)
    assert changes.mark_reviewed(conn, 8, s) == 1
    mk = conn.execute("SELECT file_id, keep_id, method FROM marks").fetchone()
    assert (mk["file_id"], mk["keep_id"], mk["method"]) == (fid(conn, "VID-WA0001.mp4"), fid(conn, "VID_0001.mp4"),
                                                             "media_similar")
    # "Not the same" takes the pair out of the groups for good, even after a rescan.
    assert media_similar.decide(conn, a, b, "not_dup")
    scan(unraid)
    assert not conn.execute("SELECT 1 FROM groups g JOIN group_members m ON m.group_id = g.id "
                            "WHERE g.method = 'media_similar' AND m.file_id = ?", (a,)).fetchone()
    assert media_similar.queue(conn) == [] or all(r["a"] != a for r in media_similar.queue(conn))
    # A pair that is not in the index cannot be decided.
    assert not media_similar.decide(conn, a, fid(conn, "VID_0002.mp4"), "keep_a")
    assert not media_similar.decide(conn, a, b, "delete_everything")


@needs_ffmpeg
def test_turning_it_off_drops_pairs_groups_and_marks(unraid, conn, library):
    scan(unraid)
    s = db.get_settings(conn)
    changes.mark_reviewed(conn, 8, s)
    a, b = sorted((fid(conn, "VID_0001.mp4"), fid(conn, "VID-WA0001.mp4")))
    media_similar.decide(conn, a, b, "keep_a")
    changes.mark_reviewed(conn, 8, s)
    assert conn.execute("SELECT COUNT(*) FROM marks WHERE method = 'media_similar'").fetchone()[0] == 1
    db.set_settings(conn, {"similar_media": False})
    regroup(conn, db.get_settings(conn))
    assert conn.execute("SELECT COUNT(*) FROM groups WHERE method = 'media_similar'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM media_pairs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM marks WHERE method = 'media_similar'").fetchone()[0] == 0
    # The decision is kept for when it is turned back on.
    assert conn.execute("SELECT COUNT(*) FROM media_reviews").fetchone()[0] == 1


@needs_ffmpeg
def test_short_and_silent_files_get_a_reason(unraid, conn):
    m = unraid.mnt
    (m / "disk1/Docs").mkdir(parents=True, exist_ok=True)
    ff("-f", "lavfi", "-i", "color=c=black:s=160x120:d=3", "-f", "lavfi", "-i", "anullsrc", "-t", "3",
       "-c:v", "libx264", "-c:a", "aac", "-shortest", str(m / "disk1/Docs/black.mp4"))
    db.set_settings(conn, {"shares": ["Docs"]})
    scan(unraid)
    why = conn.execute("SELECT h.value FROM hashes h JOIN files f ON f.id = h.file_id "
                       "WHERE f.name = 'black.mp4' AND h.kind = 'vfp_none'").fetchone()
    assert why is not None and b"too few frames" in why[0]


@needs_ffmpeg
def test_review_pages_and_poster(unraid, conn, library):
    from tests.test_web import make_client

    scan(unraid)
    with make_client(unraid) as client:
        r = client.get("/review?kind=media")
        assert r.status_code == 200 and "Songs and videos" in r.text and 'action="/review/media/decide"' in r.text
        assert "suggested keeper" in r.text and "/poster/" in r.text
        v = fid(conn, "VID_0001.mp4")
        p = client.get(f"/poster/{v}")
        assert p.status_code == 200 and p.headers["content-type"] == "image/jpeg" and p.content[:2] == b"\xff\xd8"
        assert client.get("/poster/999999").status_code == 404
        if av.chromaprint_available():  # a song has no poster
            assert client.get(f"/poster/{fid(conn, 'Low Tide.flac')}").status_code == 404
        a, b = sorted((v, fid(conn, "VID-WA0001.mp4")))
        r = client.post("/review/media/decide", data={"a": a, "b": b, "decision": "both"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/review?kind=media&done=both"
        bad = client.post("/review/media/decide", data={"a": a, "b": b, "decision": "x\r\nLocation: //evil"},
                          follow_redirects=False)
        assert bad.headers["location"] == "/review?kind=media&done="  # SC7: never echoes the posted text
        assert "Same, keep both" in client.get("/review?kind=media&view=decided").text
        assert client.post("/review/media/undo", data={"a": a, "b": b}, follow_redirects=False).status_code == 303
        assert client.get("/duplicates?method=media_similar").status_code == 200
        assert "Re-encoded songs and videos" in client.get("/").text
        assert 'name="similar_media"' in client.get("/setup").text


@needs_ffmpeg
def test_review_decisions_need_sign_in(unraid, conn, library):
    from tests.test_web import make_client

    scan(unraid)
    a, b = sorted((fid(conn, "VID_0001.mp4"), fid(conn, "VID-WA0001.mp4")))
    with make_client(unraid, signed_in=False) as client:
        r = client.post("/review/media/decide", data={"a": a, "b": b, "decision": "keep_a"},
                        headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert conn.execute("SELECT COUNT(*) FROM media_reviews").fetchone()[0] == 0
