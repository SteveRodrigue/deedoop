"""Milestone 7: video and audio (ffprobe details, stream hash, "Same recording,
other tags") and the scanner registry that turns each kind of analysis on or off."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from deedoop import av, changes, db, queries, scanners
from deedoop.control import Control, Status
from deedoop.engine import regroup
from tests.conftest import scan

needs_ffmpeg = pytest.mark.skipif(not av.tools_available(), reason="ffmpeg/ffprobe not installed")


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", *args], check=True)


def song(path: Path, freq: int = 440, seconds: float = 3, **tags: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = [x for k, v in tags.items() for x in ("-metadata", f"{k}={v}")]
    ff("-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}", "-c:a", "libmp3lame", "-b:a", "96k",
       *meta, str(path))
    return path


def retag(src: Path, dst: Path, **tags: str) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    meta = [x for k, v in tags.items() for x in ("-metadata", f"{k}={v}")]
    ff("-i", str(src), "-map", "0", "-map_metadata", "-1", "-c", "copy", *meta, str(dst))
    return dst


def video(path: Path, seconds: int = 2, pattern: str = "testsrc") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ff("-f", "lavfi", "-i", f"{pattern}=d={seconds}:s=160x120", "-f", "lavfi", "-i", f"sine=d={seconds}",
       "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(path))
    return path


# -- parsing, no ffmpeg needed ----------------------------------------------------------


def test_parse_probe_reads_tags_cover_and_layout():
    data = {
        "format": {"format_name": "mp3", "duration": "201.5", "bit_rate": "320000",
                   "tags": {"ARTIST": "Nina Simone", "album": "Pastel Blues", "track": "3/9", "date": "1965-10-01"}},
        "streams": [
            {"codec_type": "audio", "codec_name": "mp3", "channels": 2, "sample_rate": "44100"},
            {"codec_type": "video", "codec_name": "mjpeg", "width": 500, "height": 500,
             "disposition": {"attached_pic": 1}},
        ],
    }
    p = av.parse_probe(data)
    assert p.kind == "audio" and p.has_cover and p.acodec == "mp3" and p.vcodec is None
    assert p.duration == 201.5 and p.bitrate == 320000
    assert p.tags["artist"] == "Nina Simone" and p.number("track") == 3 and p.year == "1965"
    assert p.layout == "a:mp3:2:44100"  # the cover is not part of the recording
    assert p.tag_count == 4 + 1  # artist, album, track, date + cover


def test_parse_probe_video_and_vorbis_tags():
    data = {"format": {"format_name": "matroska,webm", "duration": "12.0"},
            "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
                         "avg_frame_rate": "30000/1001"},
                        {"codec_type": "audio", "codec_name": "opus", "channels": 2, "sample_rate": "48000",
                         "tags": {"TRACKNUMBER": "7", "ALBUMARTIST": "Various"}}]}
    p = av.parse_probe(data)
    assert p.kind == "video" and p.width == 1920 and p.fps == 29.97
    assert p.layout == "v:h264:1920x1080|a:opus:2:48000"
    assert p.number("track") == 7 and p.tags["album_artist"] == "Various"
    assert av.parse_probe({"format": {}, "streams": []}).error == "no audio or video stream"


def test_registry_settings_helpers():
    s = dict(db.DEFAULT_SETTINGS)
    assert scanners.enabled(s, "photo") and scanners.enabled({"scanners": {}}, "video")
    s["scanners"] = {"photo": False, "video": False, "audio": False, "archive": False}
    assert scanners.raw_only(s)
    assert scanners.methods(s) == ["exact"]
    assert scanners.exiftool_exts(s) == set()
    s["scanners"] = {"photo": True, "video": False, "audio": False, "archive": False}
    assert scanners.methods(s) == ["exact", "image", "similar"]
    assert "mp4" not in scanners.exiftool_exts(s) and "jpg" in scanners.exiftool_exts(s)


# -- scanning ------------------------------------------------------------------------------


@pytest.fixture
def music(unraid, conn):
    m = unraid.mnt
    for d in ("disk1/Docs", "disk2/Docs/Videos"):
        (m / d).mkdir(parents=True, exist_ok=True)
    a = song(m / "disk1/Docs/Music/Artist/song.mp3", artist="Artist", title="Song", album="Album", track="1",
             date="1999", genre="Jazz")
    retag(a, m / "disk2/Docs/Old/song (1).mp3", title="Song")                        # fewer tags, same audio
    shutil.copy(a, m / "disk2/Docs/Old/song copy.mp3")                                # byte-identical: exact
    song(m / "disk1/Docs/Music/Other/other.mp3", freq=880, artist="Other")            # same length, other audio
    v = video(m / "disk1/Docs/Videos/clip.mp4")
    ff("-i", str(v), "-c", "copy", "-metadata", "title=Holiday", str(m / "disk2/Docs/Videos/clip.mkv"))
    db.set_settings(conn, {"shares": ["Docs"]})
    return m


def media_groups(conn):
    out = []
    for g in conn.execute("SELECT id FROM groups WHERE method = 'media' ORDER BY id"):
        out.append(sorted(r[0] for r in conn.execute(
            "SELECT f.name FROM group_members m JOIN files f ON f.id = m.file_id WHERE m.group_id = ?", (g[0],))))
    return out


@needs_ffmpeg
def test_stream_hash_ignores_tags_cover_and_container(tmp_path):
    a = song(tmp_path / "a.mp3", artist="A")
    b = retag(a, tmp_path / "b.mp3", artist="B", album="X")
    png = tmp_path / "cover.png"
    ff("-f", "lavfi", "-i", "color=c=blue:s=32x32", "-frames:v", "1", str(png))
    c = tmp_path / "c.mp3"
    ff("-i", str(a), "-i", str(png), "-map", "0:a", "-map", "1:v", "-c", "copy", "-id3v2_version", "3",
       "-disposition:v", "attached_pic", str(c))
    other = song(tmp_path / "o.mp3", freq=660)
    h = {p.name: av.stream_hash(str(p), p.stat().st_size) for p in (a, b, c, other)}
    assert a.read_bytes() != b.read_bytes()
    assert h["a.mp3"] == h["b.mp3"] == h["c.mp3"] != h["o.mp3"]
    assert av.probe(str(c)).has_cover
    v = video(tmp_path / "v.mp4")
    ff("-i", str(v), "-c", "copy", str(tmp_path / "v.mkv"))
    assert av.stream_hash(str(v), 1) == av.stream_hash(str(tmp_path / "v.mkv"), 1)


@needs_ffmpeg
def test_same_recording_other_tags(unraid, conn, music):
    res = scan(unraid)
    assert res["media_groups"] == 2
    groups = media_groups(conn)
    assert ["song (1).mp3", "song copy.mp3", "song.mp3"] in groups
    assert ["clip.mkv", "clip.mp4"] in groups
    # The byte-identical copy is also an exact copy; the exact group is not counted twice.
    exact = conn.execute("SELECT COUNT(*) FROM groups WHERE method = 'exact'").fetchone()[0]
    assert exact == 1
    st = queries.index_stats(conn, unraid.db_path)
    assert st["total_wasted"] == st["media_wasted"]
    assert st["songs"] == 4 and st["videos"] == 2
    # The song at the same length with different audio got a stream hash but no group.
    other = conn.execute("SELECT h.kind FROM hashes h JOIN files f ON f.id = h.file_id "
                         "WHERE f.name = 'other.mp3' AND h.kind LIKE 'stream%'").fetchone()
    assert other is not None and other[0] == "stream"
    # The suggested keeper has the most complete tags.
    gid = conn.execute("SELECT g.id FROM groups g JOIN group_members m ON m.group_id = g.id JOIN files f "
                       "ON f.id = m.file_id WHERE g.method = 'media' AND f.name = 'song.mp3'").fetchone()[0]
    members = queries.group_members(conn, [gid])[gid]
    keeper = next(m for m in members if m["keeper"])
    assert keeper["user_path"].endswith("Music/Artist/song.mp3")
    assert {"artist", "album"} <= members[0]["differs"]


@needs_ffmpeg
def test_stream_hash_only_reads_files_with_a_twin(unraid, conn, music):
    song(music / "disk1/Docs/Music/long.mp3", seconds=7)  # no other file is 7 s long
    scan(unraid)
    hashed = {r[0] for r in conn.execute(
        "SELECT f.name FROM hashes h JOIN files f ON f.id = h.file_id WHERE h.kind IN ('stream', 'stream_none')")}
    assert "long.mp3" not in hashed and "song.mp3" in hashed
    probed = conn.execute("SELECT COUNT(*) FROM av").fetchone()[0]
    assert probed == 7
    # A second scan reads nothing again.
    calls = []
    real = av.stream_hash
    av.stream_hash = lambda *a: calls.append(a) or real(*a)
    try:
        scan(unraid)
    finally:
        av.stream_hash = real
    assert calls == []


@needs_ffmpeg
def test_unreadable_media_keeps_exact_matching(unraid, conn):
    m = unraid.mnt
    junk = b"not really a video" * 1000
    for d in ("disk1", "disk2"):
        p = m / d / "Docs" / "broken.mp4"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(junk)
    db.set_settings(conn, {"shares": ["Docs"]})
    scan(unraid)
    assert conn.execute("SELECT COUNT(*) FROM av WHERE error IS NOT NULL").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM groups WHERE method = 'exact'").fetchone()[0] == 1


# -- toggles ----------------------------------------------------------------------------------


def _off(conn, *keys):
    s = db.get_settings(conn)
    db.set_settings(conn, {"scanners": {**s["scanners"], **dict.fromkeys(keys, False)}})


@needs_ffmpeg
def test_every_scanner_off_means_raw_content_only(unraid, conn, music):
    from tests.test_images import jpeg, photo

    (music / "disk1/Docs/p.jpg").write_bytes(jpeg(photo(1)))
    (music / "disk2/Docs/p.jpg").write_bytes(jpeg(photo(1), exif={0x010F: "Canon"}))
    _off(conn, "photo", "video", "audio", "archive")
    res = scan(unraid)
    assert conn.execute("SELECT COUNT(*) FROM av").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM hashes WHERE kind IN ('img_exact', 'phash', 'stream')").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert set(r[0] for r in conn.execute("SELECT DISTINCT method FROM groups")) == {"exact"}
    assert res["groups"] == 1  # the byte-identical song


@needs_ffmpeg
def test_turning_video_off_drops_only_video_groups_and_marks(unraid, conn, music):
    scan(unraid)
    gid = conn.execute("SELECT g.id FROM groups g JOIN group_members m ON m.group_id = g.id JOIN files f "
                       "ON f.id = m.file_id WHERE g.method = 'media' AND f.name = 'clip.mp4'").fetchone()[0]
    keep = conn.execute("SELECT id FROM files WHERE name = 'clip.mp4'").fetchone()[0]
    assert changes.mark_group(conn, gid, keep) == 1
    _off(conn, "video")
    regroup(conn, db.get_settings(conn))
    assert media_groups(conn) == [["song (1).mp3", "song copy.mp3", "song.mp3"]]
    assert changes.marks_summary(conn)["files"] == 0
    _off(conn, "audio")
    regroup(conn, db.get_settings(conn))
    assert media_groups(conn) == []


def test_archive_scanner_off_leaves_archives_closed(unraid, conn):
    import io
    import zipfile

    m = unraid.mnt
    data = b"x" * 5000
    (m / "disk1/Docs").mkdir(parents=True)
    (m / "disk1/Docs/a.bin").write_bytes(data)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("inside/a.bin", data)
    (m / "disk2/Docs/backup.zip").write_bytes(buf.getvalue())
    db.set_settings(conn, {"shares": ["Docs"], "archives_everywhere": True})
    scan(unraid)
    assert conn.execute("SELECT COUNT(*) FROM groups WHERE method = 'exact'").fetchone()[0] == 1
    _off(conn, "archive")
    regroup(conn, db.get_settings(conn))
    assert conn.execute("SELECT COUNT(*) FROM groups WHERE method = 'exact'").fetchone()[0] == 0
    (m / "disk2/Docs/backup2.zip").write_bytes(buf.getvalue() + b"")
    scan(unraid)
    assert conn.execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 1  # the new zip was not opened


# -- changes ---------------------------------------------------------------------------------------


@needs_ffmpeg
def test_removing_a_retagged_copy_checks_the_streams_again(unraid, conn, music):
    scan(unraid)
    db.set_settings(conn, {"allow_changes": True})
    keep = conn.execute("SELECT id FROM files WHERE name = 'clip.mp4'").fetchone()[0]
    gid = conn.execute("SELECT group_id FROM group_members m JOIN groups g ON g.id = m.group_id "
                       "WHERE m.file_id = ? AND g.method = 'media'", (keep,)).fetchone()[0]
    assert changes.mark_group(conn, gid, keep) == 1
    # Swap the copy for a different video with the same size and time: the re-check refuses it.
    mkv = music / "disk2/Docs/Videos/clip.mkv"
    st = mkv.stat()
    aid = changes.create_removal(conn, unraid.mnt, "quarantine")
    other = video(music / "disk2/Docs/tmp.mkv", seconds=2, pattern="testsrc2")
    data = other.read_bytes()
    other.unlink()
    import os
    with open(mkv, "r+b") as f:
        f.seek(0)
        f.write(data[: st.st_size].ljust(st.st_size, b"\0"))
    os.utime(mkv, ns=(st.st_atime_ns, st.st_mtime_ns))
    changes.run_action(changes.RunContext(unraid.db_path, unraid.mnt, aid, Control(), Status()))
    item = conn.execute("SELECT status, note FROM action_items WHERE action_id = ?", (aid,)).fetchone()
    assert item["status"] == "skipped" and mkv.exists()


@needs_ffmpeg
def test_removing_a_retagged_copy(unraid, conn, music):
    scan(unraid)
    db.set_settings(conn, {"allow_changes": True})
    keep = conn.execute("SELECT id FROM files WHERE name = 'clip.mp4'").fetchone()[0]
    gid = conn.execute("SELECT group_id FROM group_members m JOIN groups g ON g.id = m.group_id "
                       "WHERE m.file_id = ? AND g.method = 'media'", (keep,)).fetchone()[0]
    changes.mark_group(conn, gid, keep)
    aid = changes.create_removal(conn, unraid.mnt, "quarantine")
    changes.run_action(changes.RunContext(unraid.db_path, unraid.mnt, aid, Control(), Status()))
    item = conn.execute("SELECT status FROM action_items WHERE action_id = ?", (aid,)).fetchone()
    assert item["status"] == "done"
    assert not (music / "disk2/Docs/Videos/clip.mkv").exists()
    assert media_groups(conn) == [["song (1).mp3", "song copy.mp3", "song.mp3"]]


# -- web -----------------------------------------------------------------------------------------


@needs_ffmpeg
def test_media_pages(unraid, conn, music):
    from tests.test_web import make_client

    scan(unraid)
    with make_client(unraid) as client:
        r = client.get("/duplicates?method=media")
        assert r.status_code == 200 and "Same recording, other tags" in r.text and "suggested keeper" in r.text
        r = client.get("/browse?loc=share:Docs&view=music")
        assert r.status_code == 200 and "Artist" in r.text and "Other" in r.text
        r = client.get("/browse?loc=share:Docs&view=music&artist=Artist")
        assert r.status_code == 200 and "Album" in r.text and "Song" in r.text
        r = client.get("/browse?loc=share:Docs&codec=mpeg4")
        assert r.status_code == 200 and "clip.mp4" in r.text and "song.mp3" not in r.text
        r = client.get("/browse?loc=share:Docs&length=short")
        assert "clip.mp4" in r.text and "song.mp3" in r.text
        fid = conn.execute("SELECT id FROM files WHERE name = 'clip.mp4'").fetchone()[0]
        r = client.get(f"/file/{fid}")
        assert f'src="/media/{fid}"' in r.text and "mpeg4" in r.text
        r = client.get(f"/media/{fid}", headers={"Range": "bytes=0-99"})
        assert r.status_code == 206 and len(r.content) == 100 and r.headers["content-type"] == "video/mp4"
        sid = conn.execute("SELECT id FROM files WHERE name = 'song.mp3'").fetchone()[0]
        assert client.get(f"/media/{sid}").headers["content-type"] == "audio/mpeg"
        assert "Artist" in client.get(f"/file/{sid}").text
        r = client.get("/")
        assert "Same recording, other tags" in r.text


def test_media_route_only_serves_indexed_media(unraid, conn):
    from tests.conftest import write
    from tests.test_web import make_client

    m = unraid.mnt
    write(m / "disk1/Docs/notes.txt", b"secret")
    db.set_settings(conn, {"shares": ["Docs"]})
    _off(conn, "video", "audio")
    scan(unraid)
    fid = conn.execute("SELECT id FROM files WHERE name = 'notes.txt'").fetchone()[0]
    with make_client(unraid) as client:
        assert client.get(f"/media/{fid}").status_code == 404
        assert client.get("/media/999999").status_code == 404
    with make_client(unraid, signed_in=False) as client:
        r = client.get(f"/media/{fid}", follow_redirects=False)
        assert r.status_code in (303, 401)


def test_setup_toggles(unraid, conn):
    from tests.test_web import make_client

    with make_client(unraid) as client:
        r = client.get("/setup")
        assert r.status_code == 200 and 'name="scan_video"' in r.text and "Music and audio" in r.text
        r = client.post("/setup/settings", data={"scan_photo": "1", "image_matching": "1", "min_size": "1",
                                                 "disk_mode": "single", "schedule_start": "01:00",
                                                 "schedule_end": "07:00"})
        assert r.status_code == 200
        s = db.get_settings(conn)
        assert s["scanners"] == {"photo": True, "video": False, "audio": False, "archive": False}
        assert not scanners.raw_only(s) and scanners.methods(s) == ["exact", "image"]
        client.post("/setup/settings", data={"min_size": "1", "disk_mode": "single", "schedule_start": "01:00",
                                             "schedule_end": "07:00"})
        assert scanners.raw_only(db.get_settings(conn))
        r = client.get("/")
        assert "Raw content only" in r.text
        r = client.get("/duplicates")
        assert "Same photo, different metadata" not in r.text


def test_probe_json_round_trip_is_what_ffprobe_prints():
    # ffprobe's JSON keys used by parse_probe; guards against a typo in a key name.
    sample = json.loads('{"streams":[{"codec_type":"audio","codec_name":"flac","channels":1,"sample_rate":"48000",'
                        '"duration":"2.5"}],"format":{"format_name":"flac","tags":{"TITLE":"x"}}}')
    p = av.parse_probe(sample)
    assert p.duration == 2.5 and p.tags == {"title": "x"} and p.container == "flac"
