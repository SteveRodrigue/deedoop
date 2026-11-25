# deedup

**deedup: dedupe your hoard, safely.**

deedup finds the duplicates other tools miss on your Unraid server: identical files with different names, photos that differ only in their metadata, re-compressed copies, and the contents of Google Takeout zips you've already got on disk. It indexes your terabytes incrementally, one disk at a time, and shows you everything before touching a single file.

## Status: milestone 4

deedup finds **exact duplicates of any file type**, **photos that differ only in their metadata** and **similar photos** (recompressed or resized copies, which you review), looks **inside zip, tar and 7z archives and Google Takeout**, and reports what it finds. It is **report-only**: the container mounts `/mnt` read-only and never changes your files.

| Milestone | What it adds | Status |
|---|---|---|
| M1 | Exact duplicates, any file type, report only | done |
| M2 | Photos that differ only in metadata (EXIF-blind image hash), metadata, Browse | done |
| M3 | Zip/tar/7z members and Google Takeout coverage ("what's already on disk"), local CI | done |
| M4 | Similar photos (recompressed, resized, "Storage saver") with a review queue | this release |
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
- **Same photo, different metadata** (milestone 2). JPEGs are compared on their compressed image data with EXIF, XMP, IPTC, ICC, embedded thumbnails and comments skipped; PNG, HEIC/HEIF, WebP, AVIF, TIFF and BMP are compared on their decoded pixels. A date fix, a GPS tag or a photo manager's keywords no longer hide a copy. The report shows both photos side by side with the fields that differ highlighted. Recompressed or resized copies (Google "Storage saver") are covered by similar photos, below.
- **Metadata** (milestone 2). ExifTool reads date taken, camera, resolution, duration and GPS from photos and videos, headers only.
- **Browse** (milestone 2). Walk a share as one folder tree across its disks, as a list or as thumbnails, and filter by type, year taken, camera, missing GPS, missing date or duplicates only. Each file has a details page with its metadata, hashes and duplicate groups. Thumbnails are made on demand and cached in `/config/thumbs`.
- **Archives and Google Takeout** (milestone 3). Zip, tar (also .tar.gz, .tgz, .tar.bz2, .tar.xz) and 7z files in your Takeout folders, or in every scanned folder if you turn that on, are read in place: nothing is extracted to disk. Each file inside is hashed like a loose file, so it joins the duplicate report, and an archive inside an archive is opened one level deep. The **Archives** screen shows, for every archive and every album in it, how much is already on your disks (byte for byte, or the same photo with other metadata), what exists only in another archive, and what is not on disk yet, with previews. The parts of a multi-part Takeout (`takeout-…-001.zip`, `-002.zip`, …) are shown as one, and Google's JSON sidecars are matched to their photos across parts, including the truncated and `(1)` names Google produces, to give each photo its date taken and GPS. Copies inside archives never count as reclaimable space, because they can't be removed on their own; tick "Include copies only inside archives" on the Duplicates screen to see them.
- **Similar photos** (milestone 4). Every photo, loose or inside an archive, gets two 64-bit perceptual hashes (pHash and dHash) computed on a small greyscale copy with its EXIF rotation applied, so a Google "Storage saver" copy, a resized export or a re-saved JPEG still matches its original. Close pairs are found with multi-index hashing, which takes roughly one to two minutes per million photos. The **Review** screen shows each pair side by side with the fields that differ highlighted, the distance, supporting hints (same date taken, same shape, same name) and a suggested keeper (highest resolution, then largest file, then earliest date). Answer with the keyboard: **K** keep left, **J** keep right, **B** same photo but keep both, **N** not the same photo, **S** decide later. Decisions are stored against the photos' image data, so they survive rescans, renames and moves, and a pair marked "not the same photo" never comes back. Nothing is moved or deleted; the decisions are used by milestone 6. Similar matches are suggestions and are not counted in the reclaimable total. The match closeness (0 to 10, default 8) is set on the Review or Setup screen. In the Archives screen, Takeout photos that exist on disk only as a similar copy are counted apart.
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
DEEDUP_CONFIG=./data DEEDUP_MNT=/path/to/test/mnt python -m deedup
```

### Local CI

`make ci` runs exactly what GitHub runs on every pull request, and needs only Docker:

1. **Lint and tests** in the Dockerfile's `test` stage, on the same Debian and ExifTool as the image, as a non-root user like on Unraid.
2. **Image build** (`deedup:ci`).
3. **Smoke test**: starts the image with an empty read-only `/mnt`, loads every page and checks it runs as uid 99.

`make check` is the quick version: ruff and pytest with the Python on your machine, no Docker. GitHub's workflow (`.github/workflows/ci.yml`) calls `make ci` too, then pushes the image on `main`.

Layout: `deedup/discovery.py` (disks, shares, roots), `scanner.py` (directory walk), `hashing.py` (partial and BLAKE3 hashes), `imaging.py` (metadata-blind image hash, thumbnails), `metadata.py` (ExifTool), `archives.py` (reading zip, tar and 7z members, Takeout sidecars), `archive_index.py` (storing members, sidecar pairing, coverage), `similar.py` (perceptual hashes, similar pairs, review decisions), `engine.py` (scan pipeline and grouping), `benchmark.py`, `worker.py` (job queue and schedule), `web/` (FastAPI, Jinja and htmx UI).

## Licence

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
