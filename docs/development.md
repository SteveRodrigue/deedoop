# Development

How to run deedoop from source, the checks every pull request passes, and where the code lives. Contributors (and AI agents, see [CLAUDE.md](../CLAUDE.md)) also follow [CONTRIBUTING.md](../CONTRIBUTING.md) and the [secure coding rules](secure-coding.md).

## Run from source

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'   # also install ExifTool and ffmpeg, e.g. apt install libimage-exiftool-perl ffmpeg
DEEDOOP_CONFIG=./data DEEDOOP_MNT=/path/to/test/mnt python -m deedoop
```

## Local CI

`make ci` runs exactly what GitHub runs on every pull request, and needs only Docker:

1. **Lint and tests** in the Dockerfile's `test` stage, on the same Debian and ExifTool as the image, as a non-root user like on Unraid. Ruff includes the bandit security rules (`S`) and a list of banned functions (`TID251`) that enforce the [secure coding rules](secure-coding.md).
2. **pip-audit** of the hash-pinned `requirements.lock` (also `make audit`).
3. **Image build** (`deedoop:ci`).
4. **Trivy** scan of the image for known High and Critical vulnerabilities with a fix.
5. **Smoke test**: starts the image locked down (read-only root filesystem, no capabilities) with an empty read-only `/mnt`, loads every page, and checks the security headers, that a signed-out change is refused, that it runs as uid 99 without pip, and that the setup code is in the log.

`make check` is the quick version: ruff and pytest with the Python on your machine, no Docker. GitHub's workflow (`.github/workflows/ci.yml`) calls `make ci` too, then pushes the image on `main`; CodeQL runs on every pull request and weekly. After changing dependencies in `pyproject.toml`, run `make lock` and commit `requirements.lock`. Every pull request that changes behaviour adds an entry to [CHANGELOG.md](../CHANGELOG.md) under its version.

## Layout

Everything is in `deedoop/`:

- `scanners.py`: the scanner registry and its switches
- `av.py`: ffprobe and ffmpeg
- `media_index.py`: video and audio phases and groups
- `discovery.py`: disks, shares, roots
- `scanner.py`: directory walk
- `hashing.py`: partial and BLAKE3 hashes
- `imaging.py`: metadata-blind image hash, thumbnails
- `metadata.py`: ExifTool
- `archives.py`: reading zip, tar and 7z members, Takeout sidecars
- `archive_index.py`: storing members, sidecar pairing, coverage
- `similar.py`: perceptual hashes, similar pairs, review decisions
- `engine.py`: scan pipeline and grouping
- `db.py`: the SQLite index, its schema and settings
- `queries.py`: read-only queries behind the dashboard, the report and exports
- `benchmark.py`: disk and hashing benchmark and its proposal
- `worker.py`: job queue and schedule
- `plans.py`: move rules and plan previews (writes nothing)
- `changes.py`: removals and moves
- `safepath.py`: the only file changes on `/mnt`
- `limits.py`: archive limits
- `auth.py`: accounts, sessions, throttling
- `web/`: FastAPI, Jinja and htmx UI; `web/security.py` is the sign-in, CSRF and headers guard; `web/startup.py` is the page shown while the database upgrades
- `buildinfo.py`: the version, commit and build date in the footer
- `media_similar.py`: re-encoded songs and videos: fingerprints, pairs, review decisions
- `tls.py`: built-in HTTPS
- `control.py`: progress, ETA and the scan log lines

## Screenshots

`python scripts/screenshots.py` rebuilds the README screenshots in `docs/images` from a demo array (a fake `/mnt` scanned with the real engine, pages rendered by Chromium through Playwright). Playwright is only needed for this script. Any pull request that changes how the UI looks refreshes them.
