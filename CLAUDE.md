# deedoop: notes for Claude and other AI agents

deedoop is a duplicate finder for Unraid (Python 3.12, FastAPI + Jinja + htmx,
SQLite). It can move and delete files on someone's array, so security rules
are not optional.

## Always

- Follow the secure coding rules in [docs/secure-coding.md](docs/secure-coding.md)
  (SC1 to SC10) and cite the rule id in comments where it explains a choice.
- Run `make check` while working and `make ci` before every push. Both must pass.
- New state-changing routes are `POST`; the guard in `deedoop/web/security.py`
  covers them. Add `{% include "_csrf.html" %}` to every `<form method="post">`.
- File changes on `/mnt` only through `deedoop/safepath.py`.
- Archives are never extracted; use `deedoop/limits.py` budgets.
- SQL uses `?` placeholders for every value.
- No inline `<script>` or `on…=` attributes in templates; put behaviour in
  `static/app.js`.
- Every security fix ships with a test that fails without it.
- Every screen works in light and dark themes and on a phone.
- Any PR that changes how the UI looks refreshes the affected screenshots
  (README, `docs/images`): run `python scripts/screenshots.py` and say so in the PR.
- Bump the version in `pyproject.toml` and `deedoop/__init__.py` in every PR that
  changes the image, so the footer shows which build is running.
- Every PR that changes behaviour adds an entry to `CHANGELOG.md` under its
  version (Keep a Changelog: Added, Changed, Fixed, Removed, Security), with the
  PR number. Plans and ideas go in `ROADMAP.md`; move them to the changelog when
  they ship. Keep the README short: feature details go in `docs/features.md`,
  settings in `docs/configuration.md`, dev notes in `docs/development.md`.
- Brand: Ledger palette and the deedoop logo (second e and o in the greyed `--copy` tone).
  Design explorations live in `branding/`, which is gitignored: keep design work there.

## Never

- Weaken, skip or delete a security test, or add `# noqa` to a security rule
  without a reason on the same line, to make a build pass.
- Add a dependency without `make lock` and a reason in the PR.
- Log or display passwords, session tokens or setup codes.
- Commit `.local-exiftool`, `.ca-local.crt` or other local-only CI files.

## Layout

- `deedoop/web/app.py` routes; `deedoop/web/security.py` guard and headers;
  `deedoop/auth.py` accounts, sessions, throttling.
- `deedoop/changes.py` removals and moves (M6); `deedoop/safepath.py` the only
  filesystem writes.
- `deedoop/archives.py` + `deedoop/limits.py` archive reading.
- `deedoop/metadata.py` ExifTool and `deedoop/av.py` ffprobe/ffmpeg (the only subprocesses).
- `deedoop/media_similar.py` re-encoded songs and videos (M8): fingerprints,
  pairs, review decisions.
- `deedoop/tls.py` built-in HTTPS (your certificate or a self-signed one);
  `deedoop/control.py` progress, ETA and the scan log lines.
- `deedoop/scanners.py` the scanner registry: photo, video, audio and archive
  analysis are plugins an admin can turn off. A new kind of analysis is a new
  `Scanner` row with hooks, never an `if` in the engine.
- `tests/test_security.py` the security tests, named by rule and finding.
