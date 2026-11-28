"""Regenerate the README screenshots in docs/images from a demo array.

Any pull request that changes how the UI looks refreshes these (see CLAUDE.md).

    pip install -e '.[dev]' playwright     # playwright is only needed here, not in the image
    python scripts/screenshots.py          # writes docs/images/*.png
    python scripts/screenshots.py --out /tmp/shots

It builds a small fake Unraid /mnt in a temporary folder (photos, Storage saver
copies, a Google Takeout zip, documents and WAV songs), scans it with the real
engine, then drives Chromium against the app in process: every browser request
is answered by a signed-in FastAPI TestClient, so no server or port is needed.
Set CHROMIUM to use another browser binary (default /opt/pw-browsers/chromium
when it exists, else Playwright's own).
"""

import argparse
import io
import os
import sys
import tempfile
import time
import wave
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from deedoop import db  # noqa: E402
from deedoop.config import AppConfig  # noqa: E402
from deedoop.web.app import create_app  # noqa: E402
from deedoop.worker import Worker  # noqa: E402

BASE = "http://deedoop.local"
USER, PASSWORD = "admin", "demo password for screenshots"
DESKTOP = {"width": 1600, "height": 1125}
PHONE = {"width": 390, "height": 844}


# -- the demo array ----------------------------------------------------------------

def rr(r, lo: int, hi: int) -> int:
    return int(r.integers(lo, hi))


def landscape(seed: int, size=(2400, 1800)) -> Image.Image:
    """A photo-like landscape: sky gradient, sun, layered hills."""
    r = np.random.default_rng(seed)
    w, h = size
    top = [rr(r, 60, 200) for _ in range(3)]
    bottom = [rr(r, 150, 255) for _ in range(3)]
    im = Image.new("RGB", size)
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)))
    sx, sy, sr = rr(r, w // 5, 4 * w // 5), rr(r, h // 12, h // 3), rr(r, w // 40, w // 18)
    d.ellipse((sx - sr, sy - sr, sx + sr, sy + sr), fill=(255, 240, 200))
    for layer in range(4):
        base = h * (0.55 + 0.12 * layer)
        col = tuple(max(0, c - 25 * layer) for c in (rr(r, 30, 110), rr(r, 80, 150), rr(r, 30, 90)))
        for _ in range(3):
            cx, rx, ry = rr(r, -w // 6, w), rr(r, w // 5, w // 2), rr(r, h // 8, h // 4)
            d.ellipse((cx - rx, base - ry, cx + rx, base + ry * 2), fill=col)
    return im.filter(ImageFilter.GaussianBlur(2))


def jpg(im: Image.Image, quality=92, scale=1.0) -> bytes:
    if scale != 1.0:
        im = im.resize((int(im.width * scale), int(im.height * scale)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def put(path: Path, data: bytes, days_ago: float = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    t = time.time() - days_ago * 86400
    os.utime(path, (t, t))


def song(path: Path, freq: int, title: str, artist: str, album: str, seconds: int = 40) -> None:
    """A WAV song (two tones) with RIFF INFO tags; the same notes give the same audio stream."""
    t = np.arange(seconds * 22050) / 22050
    pcm = (0.3 * np.sin(2 * np.pi * freq * t) + 0.2 * np.sin(3 * np.pi * freq * t)) * 32767
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(pcm.astype("<i2").tobytes())
    info = b"INFO"
    for key, value in ((b"INAM", title), (b"IART", artist), (b"IPRD", album)):
        v = value.encode() + b"\0"
        v += b"\0" * (len(v) % 2)
        info += key + len(v).to_bytes(4, "little") + v
    data = bytearray(buf.getvalue()) + b"LIST" + len(info).to_bytes(4, "little") + info
    data[4:8] = (len(data) - 8).to_bytes(4, "little")
    put(path, bytes(data), 900)


def build_array(mnt: Path) -> None:
    shares = ("Photos", "Backups", "Takeout", "Documents", "Music", "Media")
    for share in shares:
        (mnt / "user" / share).mkdir(parents=True, exist_ok=True)
    (mnt / "user0").mkdir(parents=True, exist_ok=True)
    r = np.random.default_rng(7)

    # Photos: originals on disk1, backups of the phone on disk2, Storage saver copies in Takeout.
    for i, (day, num) in enumerate([("20190614", 112), ("20200101", 114), ("20210822", 87), ("20230704", 114),
                                    ("20230705", 9), ("20240212", 31), ("20240930", 55), ("20250418", 3)]):
        im = landscape(100 + i)
        name = f"IMG_{day}_000{num:03d}.jpg"
        original = jpg(im)
        put(mnt / f"disk1/Photos/{day[:4]}/{name}", original, 900 - i * 90)
        if i % 2 == 0:
            put(mnt / f"disk2/Backups/Pixel 4a/DCIM/{name}", original, 800 - i * 90)
            put(mnt / f"disk3/Backups/Old laptop/Pictures/{day[:4]}/{name}", original, 700 - i * 80)
        if i in (1, 3, 5):
            put(mnt / f"disk2/Backups/Pixel 4a/DCIM/{name[:-4]} (1).jpg", jpg(im, quality=85), 600)
            put(mnt / f"disk3/Takeout/Google Photos/Photos from {day[:4]}/{name}", jpg(im, 55, 0.5), 300)
            put(mnt / f"disk2/Backups/Old laptop/Pictures/{name}", jpg(im, 70, 0.25), 500)

    # Same photo, other metadata: a comment segment changes the bytes, not the pixels.
    for i, (day, num) in enumerate([("20190614", 112), ("20210822", 87), ("20240930", 55)]):
        original = (mnt / f"disk1/Photos/{day[:4]}/IMG_{day}_000{num:03d}.jpg").read_bytes()
        note = f"Edited on the old laptop #{i}".encode()
        tagged = original[:2] + b"\xff\xfe" + (len(note) + 2).to_bytes(2, "big") + note + original[2:]
        put(mnt / f"disk2/Backups/Old laptop/Pictures/Edited/IMG_{day}_{num:03d}_edit.jpg", tagged, 350)

    # A Google Takeout zip: one photo already on disk (Storage saver size), one new.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Takeout/Google Photos/Photos from 2024/IMG_20240212_000031.jpg", jpg(landscape(105), 55, 0.5))
        z.writestr("Takeout/Google Photos/Photos from 2024/IMG_20241225_000001.jpg", jpg(landscape(150), 80, 0.5))
        z.writestr("Takeout/Google Photos/Photos from 2019/IMG_20190614_000112.jpg", jpg(landscape(100)))
    put(mnt / "disk3/Takeout/takeout-20250301T101010Z-001.zip", buf.getvalue(), 200)

    # Documents and installers: exact copies with other names, in other shares.
    docs = {
        "Documents/Taxes/2023 return.pdf": 1_400_000, "Documents/Manuals/NAS manual.pdf": 2_200_000,
        "Documents/House/Inspection report.pdf": 3_100_000, "Documents/Scans/passport-scan.png": 900_000,
        "Media/Installers/ubuntu-24.04-desktop-amd64.iso": 24_000_000,
        "Media/Home videos/2018 Christmas.mp4": 18_000_000, "Media/Home videos/Lake trip.mov": 12_500_000,
    }
    for rel, size in docs.items():
        data = r.bytes(size)
        share, rest = rel.split("/", 1)
        put(mnt / f"disk1/{rel}", data, 1000)
        stem, ext = os.path.splitext(Path(rest).name)
        put(mnt / f"disk2/Backups/Old laptop/{share}/{stem} (copy){ext}", data, 400)
        if size > 10_000_000:
            put(mnt / f"disk3/Backups/USB stick/{Path(rest).name}", data, 300)
    for i in range(6):
        data = r.bytes(rr(r, 60_000, 400_000))
        put(mnt / f"cache/Documents/Notes/note-{i:02d}.txt", data, 50)
        put(mnt / f"disk2/Backups/Old laptop/Documents/Notes/note-{i:02d} copy.txt", data, 450)

    # Music: the same recording with other tags, and a re-encoded copy.
    song(mnt / "disk1/Music/Northbound/Open Water/01 Harbour Lights.wav", 440, "Harbour Lights", "Northbound",
         "Open Water")
    song(mnt / "disk1/Music/Northbound/Open Water/02 Tidewater.wav", 523, "Tidewater", "Northbound", "Open Water")
    song(mnt / "disk2/Backups/Old laptop/Music/Harbour Lights.wav", 440, "Harbour Lights (album version)",
         "Northbound", "Best of")


# -- the app in process ------------------------------------------------------------

def make_client(cfg: AppConfig):
    from fastapi.testclient import TestClient

    from deedoop import auth

    c = db.connect(cfg.db_path)
    try:
        if not auth.has_admin(c):
            auth.create_admin(c, USER, PASSWORD)
    finally:
        c.close()
    client = TestClient(create_app(cfg, worker=Worker(cfg)), base_url=BASE)
    client.__enter__()
    r = client.post("/login", data={"username": USER, "password": PASSWORD}, follow_redirects=False)
    if r.status_code != 303:
        raise SystemExit(f"sign-in failed: {r.status_code}")
    c = db.connect(cfg.db_path)
    try:
        client.headers["X-CSRF-Token"] = c.execute(
            "SELECT csrf FROM sessions ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    finally:
        c.close()
    return client


def wait_idle(client, timeout=600) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get("/api/status").json()["active"] is None:
            return
        time.sleep(0.3)
    raise SystemExit("scan did not finish")


def busy_scan(client, mnt: Path) -> None:
    """Start a slowed-down rescan so the status bar has something to show."""
    from deedoop.control import Control

    r = np.random.default_rng(11)
    for i in range(400):
        put(mnt / f"disk2/Backups/Old laptop/Documents/Inbox/scan-{i:03d}.pdf", r.bytes(20_000), 10)
    real = Control.checkpoint

    def slow(self) -> None:
        time.sleep(0.25)
        real(self)

    Control.checkpoint = slow  # type: ignore[method-assign]
    client.post("/scan/start")
    deadline = time.time() + 120
    while time.time() < deadline:
        s = client.get("/partials/jobbar").text
        if "Phase" in s and "%" in s:
            time.sleep(3)  # let the rate and ETA settle
            return
        time.sleep(0.5)


def prepare(work: Path):
    mnt = work / "mnt"
    build_array(mnt)
    cfg = AppConfig(config_dir=work / "config", mnt=mnt)
    db.init(cfg.db_path)
    c = db.connect(cfg.db_path)
    try:
        db.set_settings(c, {"shares": ["Photos", "Backups", "Takeout", "Documents", "Music", "Media"],
                            "takeout_paths": [str(mnt / "user/Takeout")]})
    finally:
        c.close()
    client = make_client(cfg)
    client.post("/scan/start")
    wait_idle(client)
    return client


def shoot(client, out: Path, work: Path) -> None:
    from playwright.sync_api import sync_playwright

    def forward(route, request):
        path = request.url[len(BASE):] or "/"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ("cookie", "host")}
        r = client.request(request.method, path, headers=headers, content=request.post_data_buffer,
                           follow_redirects=False)
        body = r.content
        if r.headers.get("content-type", "").startswith("text/html"):
            body = body.replace(str(work).encode(), b"")  # show /mnt/..., not the temporary folder
        headers = {k: v for k, v in r.headers.items() if k.lower() != "content-length"}
        route.fulfill(status=r.status_code, headers=headers, body=body)

    exe = os.environ.get("CHROMIUM") or ("/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists()
                                         else None)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()

        def page(theme: str, viewport: dict, scale: int = 2, mobile: bool = False):
            ctx = browser.new_context(viewport=viewport, device_scale_factor=scale, is_mobile=mobile,
                                      has_touch=mobile, color_scheme=theme)
            ctx.add_init_script(f"try {{ localStorage.setItem('deedoop-theme', '{theme}') }} catch (e) {{}}")
            ctx.route(f"{BASE}/**", forward)
            return ctx, ctx.new_page()

        def snap(name: str, url: str, theme: str, viewport=DESKTOP, scale=1, mobile=False, scroll_to=""):
            ctx, pg = page(theme, viewport, scale, mobile)
            pg.goto(BASE + url)
            pg.wait_for_load_state("networkidle")
            if scroll_to:
                pg.locator(scroll_to).first.evaluate("e => { const h = document.querySelector('header');"
                    " const top = h && getComputedStyle(h).position === 'sticky' ? h.offsetHeight : 0;"
                    " window.scrollTo(0, e.getBoundingClientRect().top + window.scrollY - top - 12); }")
                pg.wait_for_load_state("networkidle")
            pg.evaluate("document.fonts.ready")
            pg.screenshot(path=str(out / name))
            ctx.close()
            print("wrote", out / name)

        snap("dashboard-light.png", "/", "light")
        busy_scan(client, work / "mnt")  # the rest show the status bar of a running scan
        snap("duplicates-dark.png", "/duplicates", "dark")
        snap("similar-light.png", "/duplicates?method=similar", "light")
        snap("phone-dark.png", "/duplicates?method=similar", "dark", PHONE, scale=2, mobile=True,
             scroll_to="details.group")
        client.post("/scan/stop")
        browser.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "docs/images")
    ap.add_argument("--keep", action="store_true", help="keep the demo array and print where it is")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="deedoop-demo-", delete=not args.keep) as tmp:
        work = Path(tmp)
        client = prepare(work)
        shoot(client, args.out, work)
        client.__exit__(None, None, None)
        if args.keep:
            print("demo array kept in", work)


if __name__ == "__main__":
    main()
