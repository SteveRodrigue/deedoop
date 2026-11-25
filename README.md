# deedup

**deedup: dedupe your hoard, safely.**

deedup finds the duplicates other tools miss on your Unraid server: identical files with different names, photos that differ only in their metadata, re-compressed copies, and the contents of Google Takeout zips you've already got on disk. It indexes your terabytes incrementally, one disk at a time, and shows you everything before touching a single file.

## Status: milestone 2

deedup finds **exact duplicates of any file type** and **photos that differ only in their metadata**, and reports them. It is **report-only**: the container mounts `/mnt` read-only and never changes your files.

| Milestone | What it adds | Status |
|---|---|---|
| M1 | Exact duplicates, any file type, report only | done |
| M2 | Photos that differ only in metadata (EXIF-blind image hash), metadata, Browse | this release |
| M3 | Zip/tar/7z members and Google Takeout coverage ("what's already on disk") | planned |
| M4 | Similar photos (recompressed, resized, "Storage saver") with a review queue | planned |
| M5 | Browse and plan reorganisation (preview only) | planned |
| Gate | Write access to `/mnt` is turned on only with your OK | |
| M6 | Move and delete: quarantine with undo, opt-in direct delete, hardlinks | planned |

## What it does

- **Setup screen.** Lists your Unraid shares, disks and pools. Tick the shares to scan, add other folders (for example on an unassigned device), set exclusions, the disk mode and a nightly schedule.
- **Reads the disks, not `/mnt/user`.** A share is scanned as its folder on each disk (`/mnt/disk1/Photos`, `/mnt/disk2/Photos`, …), so every file knows its disk, and the report still shows the `/mnt/user/...` path you know.
- **One disk at a time by default.** Only one array disk works at once. A parallel mode reads several disks at once, and a **benchmark** measures each disk alone and in combinations, plus hashing speed, and proposes the setting.
- **Lazy, tiered hashing.** Files are grouped by size; only same-size files get a quick hash (first and last 64 KB); only those that still match get a full BLAKE3 hash. Files with a unique size are never opened.
- **Incremental index.** A SQLite index in `/config` remembers size, mtime and inode. A rescan walks folders and rereads only new or changed files. Stopped scans pick up where they left off.
- **Honest numbers.** Hardlinks are recognised as one copy and never counted as wasted space. Empty files are ignored.
- **Report.** Duplicate groups sorted by reclaimable space, filters by type, disk, path and size, and CSV or JSON export.
- **Same photo, different metadata** (milestone 2). JPEGs are compared on their compressed image data with EXIF, XMP, IPTC, ICC, embedded thumbnails and comments skipped; PNG, HEIC/HEIF, WebP, AVIF, TIFF and BMP are compared on their decoded pixels. A date fix, a GPS tag or a photo manager's keywords no longer hide a copy. The report shows both photos side by side with the fields that differ highlighted. Recompressed or resized copies (Google "Storage saver") are milestone 4.
- **Metadata** (milestone 2). ExifTool reads date taken, camera, resolution, duration and GPS from photos and videos, headers only.
- **Browse** (milestone 2). Walk a share as one folder tree across its disks, as a list or as thumbnails, and filter by type, year taken, camera, missing GPS, missing date or duplicates only. Each file has a details page with its metadata, hashes and duplicate groups. Thumbnails are made on demand and cached in `/config/thumbs`.
- **Light and dark themes.** Follows your system setting, or pick Light or Dark in the header; the choice is remembered per browser.
- **Checks.** Files that exist at the same path on two disks (Unraid shows only one of them in `/mnt/user`), and files that could not be read.

## Install on Unraid

Until deedup is in Community Applications, add it with the template in this repo:

1. Copy [`unraid/deedup.xml`](unraid/deedup.xml) to `/boot/config/plugins/dockerMan/templates-user/my-deedup.xml` on your server.
2. In the Docker tab, click **Add Container**, pick **deedup** from the template list, check the paths and click **Apply**.
3. Open `http://<server>:8080`, go to **Setup**, tick your shares, then start a scan from the **Dashboard**.

The template maps:

| Container | Host | Mode |
|---|---|---|
| `/mnt` | `/mnt` | **read-only** |
| `/config` | `/mnt/user/appdata/deedup` | read-write (index database) |
| `8080` | `8080` | web UI |

The container runs as `nobody:users` (99:100). Set `DEEDUP_PASSWORD` to require a password (user name `deedup`); otherwise anyone on your LAN can open the UI.

The image is built by GitHub Actions and published to `ghcr.io/steverodrigue/deedup:latest` on every push to `main`. To build it yourself:

```sh
docker build -t deedup .
docker run -d --name deedup -p 8080:8080 --user 99:100 \
  -v /mnt:/mnt:ro,rslave -v /mnt/user/appdata/deedup:/config deedup
```

See [`docker-compose.yml`](docker-compose.yml) for a Compose example.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DEEDUP_CONFIG` | `/config` | Where the index database lives |
| `DEEDUP_MNT` | `/mnt` | Where the host's `/mnt` is mounted |
| `DEEDUP_PORT` | `8080` | Web UI port |
| `DEEDUP_PASSWORD` | (none) | Turns on basic auth |

Everything else is set in the app on the Setup screen.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'   # also install ExifTool, e.g. apt install libimage-exiftool-perl
pytest -q
ruff check .
DEEDUP_CONFIG=./data DEEDUP_MNT=/path/to/test/mnt python -m deedup
```

Layout: `deedup/discovery.py` (disks, shares, roots), `scanner.py` (directory walk), `hashing.py` (partial and BLAKE3 hashes), `imaging.py` (metadata-blind image hash, thumbnails), `metadata.py` (ExifTool), `engine.py` (scan pipeline and grouping), `benchmark.py`, `worker.py` (job queue and schedule), `web/` (FastAPI, Jinja and htmx UI).

## Licence

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
