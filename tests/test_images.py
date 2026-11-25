import io
import os
import shutil
import subprocess
import time

import pytest
from PIL import Image

from deedup import db
from deedup.imaging import NotAnImage, jpeg_data_hash, pixel_hash
from tests.conftest import scan, write

needs_exiftool = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")


def photo(seed: int, size=(320, 240)) -> Image.Image:
    import random

    rnd = random.Random(seed)
    im = Image.new("RGB", size)
    px = im.load()
    for x in range(size[0]):
        for y in range(size[1]):
            px[x, y] = ((x * seed) % 256, (y * 3 + seed) % 256, rnd.randrange(256))
    return im


def jpeg(im: Image.Image, quality=90, exif: dict | None = None) -> bytes:
    buf = io.BytesIO()
    kw = {}
    if exif:
        e = Image.Exif()
        for k, v in exif.items():
            e[k] = v
        kw["exif"] = e.tobytes()
    im.save(buf, "JPEG", quality=quality, **kw)
    return buf.getvalue()


def png(im: Image.Image, text: dict | None = None) -> bytes:
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    for k, v in (text or {}).items():
        info.add_text(k, v)
    buf = io.BytesIO()
    im.save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


def exiftool(*args):
    subprocess.run(["exiftool", "-q", "-overwrite_original", *map(str, args)], check=True)


# -- hashing -----------------------------------------------------------------


def test_jpeg_hash_ignores_exif_but_not_pixels():
    im = photo(1)
    a = jpeg(im, exif={0x010F: "Canon", 0x0132: "2019:01:01 10:00:00"})
    b = jpeg(im, exif={0x010F: "Nikon", 0x0132: "2024:12:31 23:59:59"})
    assert a != b
    assert jpeg_data_hash(a) == jpeg_data_hash(b) == jpeg_data_hash(jpeg(im))
    assert jpeg_data_hash(jpeg(im, quality=60)) != jpeg_data_hash(a)  # recompressed: milestone 4
    assert jpeg_data_hash(jpeg(photo(2))) != jpeg_data_hash(a)


def test_jpeg_hash_ignores_trailing_data_after_eoi():
    a = jpeg(photo(3))
    assert jpeg_data_hash(a + b"\x00samsung trailer" * 10) == jpeg_data_hash(a)


def test_png_pixel_hash_ignores_text_chunks():
    im = photo(4)
    assert pixel_hash(png(im, {"Comment": "a"})) == pixel_hash(png(im, {"Author": "b", "Date": "c"}))
    assert pixel_hash(png(photo(5))) != pixel_hash(png(im))


def test_garbage_is_not_an_image():
    with pytest.raises(NotAnImage):
        jpeg_data_hash(os.urandom(1000))
    with pytest.raises(NotAnImage):
        pixel_hash(os.urandom(1000))


def test_heic_pixel_hash():
    pytest.importorskip("pillow_heif")
    buf = io.BytesIO()
    try:
        photo(6).save(buf, "HEIF")
    except (KeyError, OSError, ValueError) as e:
        pytest.skip(f"no HEIF encoder: {e}")
    assert len(pixel_hash(buf.getvalue())) == 32


# -- scanning ------------------------------------------------------------------


def select(conn, *shares):
    db.set_settings(conn, {"shares": list(shares)})


def image_groups(conn):
    out = []
    for g in conn.execute("SELECT * FROM groups WHERE method = 'image' ORDER BY id"):
        names = sorted(r[0] for r in conn.execute(
            "SELECT f.name FROM group_members m JOIN files f ON f.id = m.file_id WHERE m.group_id = ?", (g["id"],)))
        out.append((g["copies"], g["wasted"], names))
    return out


@needs_exiftool
def test_photos_that_differ_only_in_metadata(unraid, conn):
    m = unraid.mnt
    original = write(m / "disk1/Photos/2019/IMG_0001.JPG", jpeg(photo(7)))
    edited = write(m / "disk2/Photos/backup/IMG_0001.JPG", original.read_bytes())
    exiftool("-DateTimeOriginal=2019:07:14 12:00:00", "-GPSLatitude=45.5", "-GPSLatitudeRef=N",
             "-GPSLongitude=73.6", "-GPSLongitudeRef=W", "-Make=Canon", "-Model=Canon EOS 80D", edited)
    write(m / "disk2/Photos/backup/other.jpg", jpeg(photo(8)))
    select(conn, "Photos")
    summary = scan(unraid)
    assert summary["groups"] == 0  # bytes differ
    assert summary["image_groups"] == 1
    [(copies, wasted, names)] = image_groups(conn)
    assert copies == 2 and names == ["IMG_0001.JPG", "IMG_0001.JPG"]
    assert wasted == min(original.stat().st_size, edited.stat().st_size)

    meta = conn.execute(
        "SELECT m.* FROM media m JOIN files f ON f.id = m.file_id JOIN roots r ON r.id = f.root_id "
        "WHERE r.disk = 'disk2' AND f.name = 'IMG_0001.JPG'").fetchone()
    assert meta["taken_at"] == "2019-07-14 12:00:00"
    assert meta["camera"] == "Canon EOS 80D"
    assert (meta["width"], meta["height"]) == (320, 240)
    assert meta["gps_lat"] == pytest.approx(45.5) and meta["gps_lon"] == pytest.approx(-73.6)
    assert meta["mime"] == "image/jpeg"


def test_image_groups_without_exiftool(unraid, conn):
    """Image matching does not need ExifTool; Pillow-written EXIF is enough here."""
    m = unraid.mnt
    im = photo(9)
    write(m / "disk1/Photos/a.jpg", jpeg(im, exif={0x010F: "Canon"}))
    write(m / "disk2/Photos/b.jpeg", jpeg(im, exif={0x010F: "Sony", 0x0110: "A7"}))
    write(m / "disk2/Photos/c.png", png(photo(10), {"x": "1"}))
    write(m / "disk1/Photos/d.png", png(photo(10), {"y": "22"}))
    select(conn, "Photos")
    db.set_settings(conn, {"metadata": False})
    s = scan(unraid)
    assert s["image_groups"] == 2
    assert sorted(n for _, _, names in image_groups(conn) for n in names) == ["a.jpg", "b.jpeg", "c.png", "d.png"]


def test_exact_copies_of_photos_stay_exact(unraid, conn):
    m = unraid.mnt
    data = jpeg(photo(11))
    write(m / "disk1/Photos/a.jpg", data)
    write(m / "disk2/Photos/b.jpg", data)
    select(conn, "Photos")
    s = scan(unraid)
    assert (s["groups"], s["image_groups"]) == (1, 0)
    # The photo pass already produced the exact hashes: no second read.
    assert conn.execute("SELECT COUNT(*) FROM hashes WHERE kind = 'blake3'").fetchone()[0] == 2


def test_broken_photo_still_matches_exactly(unraid, conn):
    m = unraid.mnt
    junk = os.urandom(200_000)
    write(m / "disk1/Photos/broken.jpg", junk)
    write(m / "disk2/Photos/broken copy.jpg", junk)
    select(conn, "Photos")
    s = scan(unraid)
    assert (s["groups"], s["image_groups"]) == (1, 0)
    assert conn.execute("SELECT COUNT(*) FROM hashes WHERE kind = 'img_none'").fetchone()[0] == 2


def test_turning_image_matching_off_drops_its_groups(unraid, conn):
    m = unraid.mnt
    im = photo(12)
    write(m / "disk1/Photos/a.jpg", jpeg(im, exif={0x010F: "A"}))
    write(m / "disk2/Photos/b.jpg", jpeg(im, exif={0x010F: "BB"}))
    select(conn, "Photos")
    assert scan(unraid)["image_groups"] == 1
    db.set_settings(conn, {"image_matching": False})
    assert scan(unraid)["image_groups"] == 0


@needs_exiftool
def test_metadata_refreshes_when_file_changes(unraid, conn):
    m = unraid.mnt
    p = write(m / "disk1/Photos/a.jpg", jpeg(photo(13)))
    select(conn, "Photos")
    scan(unraid)
    assert conn.execute("SELECT taken_at FROM media").fetchone()[0] is None
    exiftool("-DateTimeOriginal=2001:02:03 04:05:06", p)
    os.utime(p, (time.time() + 10, time.time() + 10))
    scan(unraid)
    assert conn.execute("SELECT taken_at FROM media").fetchone()[0] == "2001-02-03 04:05:06"
