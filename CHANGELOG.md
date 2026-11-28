# Changelog

All notable changes to deedoop are listed here, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) (before 1.0, a minor version is a new
feature set and a patch version is a fix or a small improvement).

The version is the one in `pyproject.toml` and in the footer of every page. Each
pull request that changes behaviour adds its entry under its version here.
What is planned next is in [ROADMAP.md](ROADMAP.md).

## [0.11.1] - 2026-11-28

### Changed

- The documentation is reorganized: the README is short (what deedoop is,
  screenshots, a quick install and links), the release history moved here, the
  plans to [ROADMAP.md](ROADMAP.md), and the guides to `docs/`:
  [features](docs/features.md), [configuration](docs/configuration.md) and
  [development](docs/development.md). No change to the app.

## [0.11.0] - 2026-11-28

Browse is quick on a big index ([#30](https://github.com/SteveRodrigue/deedoop/pull/30)).

### Changed

- Browse shows the first 100 files at once and the next ones a page at a time
  (**Show more**), instead of sorting a whole share for the first page.
- The search count (capped at 100,000) and the filter lists load just after the
  page is shown; the filter lists are cached until a scan starts or ends.
- Folder and search queries use the folder index. On about 2 million files,
  switching to Browse took 2 to 5 seconds even with no scan running.

### Fixed

- The Music view listed one "artist" per song.

## [0.10.4] - 2026-11-28

The benchmark proposal is shown next to the current Disk mode
([#29](https://github.com/SteveRodrigue/deedoop/pull/29)).

### Added

- The benchmark card has a Now / Proposed table for the disk mode, disks read at
  once and hash threads, with each change highlighted. **Apply N changes** shows
  only when something would change, and the confirmation lists each change.
- Never benchmarked: Disk mode says so with a **Run benchmark** shortcut, and the
  dashboard suggests it (**Run benchmark** / **Not now**).

### Fixed

- A one-disk-at-a-time proposal no longer overwrites the saved parallel disk count.
- Starting a benchmark during a scan says why it did not start, instead of
  "Benchmark started".

## [0.10.3] - 2026-11-28

### Security

- Adding a custom folder checks that it is inside `/mnt` before touching the
  filesystem (CodeQL alert 33, rule SC7), with a test for `..` escapes and a
  sibling folder named like the mount
  ([#28](https://github.com/SteveRodrigue/deedoop/pull/28)).

## [0.10.2] - 2026-11-28

### Changed

- The README screenshots show the current look. `scripts/screenshots.py`
  rebuilds them from a demo array; Playwright is only needed for this script
  ([#27](https://github.com/SteveRodrigue/deedoop/pull/27)).

## [0.10.1] - 2026-11-28

### Fixed

- After an update, deedoop answers from the first second with an "Upgrading the
  database" page that says which step is running, instead of looking dead for
  many minutes while a large write-ahead log is folded in and new indexes are
  built. `/healthz` answers while it starts, and a scan carries on as soon as the
  database is ready ([#26](https://github.com/SteveRodrigue/deedoop/pull/26)).
- The install guide says the first start after an update can take a while, and
  not to restart the container.

## [0.10.0] - 2026-11-28

Pull requests #20 to #24 shipped while the version still said 0.9.0; 0.10.0 is
the first version label that includes them.

### Added

- The footer shows the version, the build's commit and date, with links to the
  repository and the commit; `/api/status` reports them too
  ([#25](https://github.com/SteveRodrigue/deedoop/pull/25)).
- A status bar at the bottom of every page but the Dashboard while a job runs:
  phase, percentage, speed and time left
  ([#22](https://github.com/SteveRodrigue/deedoop/pull/22)).
- A first-scan warning: the first scan of a large array can take days.
- Rescans show how long each phase took last time, and finished scans show how
  long they took.
- A save confirms with a floating pill, and the page keeps its scroll position.
  Confirmations leave after 5 seconds; errors stay until closed
  ([#21](https://github.com/SteveRodrigue/deedoop/pull/21)).
- A warning on every page when changes are allowed but `/mnt` is read-only.

### Changed

- First run in two steps: the setup code first, with directions to the Unraid
  log, then the account form. The page never carries the code
  ([#20](https://github.com/SteveRodrigue/deedoop/pull/20)).
- The install guide has one action per step and says where each step happens,
  and spells out the GitHub token type, scope and expiry.
- Scans and other jobs run in their own low-priority process, so pages stay quick
  during a scan. A job process that dies is recorded as failed with a message.
- Dashboard and per-disk totals are much faster on a big index (8 s to 0.1 s on
  2 million files).
- Starting a scan with nothing selected says so and links to Setup; a scan
  started while another job runs is queued behind it.
- The live scan view refreshes every second, with files per second or MB per
  second, and long paths keep their end (`.../folder/file.ext`).
- The Plans tab is now **Organize**; old links keep working
  ([#23](https://github.com/SteveRodrigue/deedoop/pull/23)).
- Square cards with a 45-degree cut at the bottom right and a teal mark on the
  top-left corner; pills and toggles stay round
  ([#24](https://github.com/SteveRodrigue/deedoop/pull/24)).
- The thumbnail cache is capped at 1 GB, oldest first.
- Damaged-metadata warnings from photos go to the scan log once per file, with
  its path, instead of filling the container log.

### Fixed

- The database's write-ahead log is capped at 64 MB (it reached 6.7 GB during a
  first scan).

## [0.9.0] - 2026-11-26

deedup is now **deedoop**, with the Ledger design
([#16](https://github.com/SteveRodrigue/deedoop/pull/16)).

### Added

- The deedoop logo (the second e and o greyed, like a duplicate row) and the
  Ledger palette, checked for colour blindness, in light and dark themes.
- A private Unraid install: the template in `unraid/deedoop.xml` with its icon,
  [docs/install-unraid.md](docs/install-unraid.md), and tests for the template
  ([#19](https://github.com/SteveRodrigue/deedoop/pull/19)).
- README logo and screenshots ([#17](https://github.com/SteveRodrigue/deedoop/pull/17)).

### Changed

- The image is `ghcr.io/steverodrigue/deedoop`, and variables start with `DEEDOOP_`.
- GitHub Actions bumped: checkout 7.0.1, login-action 4.8.0, codeql-action 4.39.4
  ([#18](https://github.com/SteveRodrigue/deedoop/pull/18), combining Dependabot
  #9 to #12).

### Removed

- The upgrade path from the old deedup name ([#17](https://github.com/SteveRodrigue/deedoop/pull/17)).

## [0.8.0] - 2026-11-26

Milestone 8: re-encoded songs and videos
([#15](https://github.com/SteveRodrigue/deedoop/pull/15)).

### Added

- Audio fingerprints (Chromaprint, built into ffmpeg) of a song's first 2
  minutes, and a 12-frame pHash for videos, to find a FLAC converted to MP3 or a
  video re-encoded by a phone or Plex.
- The **Re-encoded songs and videos** method on the Duplicates screen. These are
  suggestions and are not counted in the reclaimable total.
- A **Songs and videos** queue on the Review screen, with players, the fields
  that differ, the match strength and a suggested keeper. Decisions survive
  rescans and moves.
- The **Find re-encoded songs and videos** switch in Setup.

## [0.7.5] - 2026-11-26

Milestone 7.5: HTTPS and scan progress
([#14](https://github.com/SteveRodrigue/deedoop/pull/14)).

### Added

- Built-in HTTPS (`DEEDOOP_HTTPS=on`): your certificate from the `tls` folder,
  otherwise a self-signed one whose fingerprint Setup shows. TLS 1.2 and 1.3
  only. New dependency: `cryptography`.
- Scan progress: start time, time running, **phase x of y**, and the time left in
  the phase.
- The scan log (Dashboard > Full log): each phase with its duration, and every
  file or folder that could not be read, for the last 30 jobs.

## [0.7.0] - 2026-11-26

Milestone 7: video and audio, and scanners you can turn off
([#8](https://github.com/SteveRodrigue/deedoop/pull/8), landed on main by
[#13](https://github.com/SteveRodrigue/deedoop/pull/13)).

### Added

- ffprobe details for videos and songs: duration, codecs, resolution, bitrate
  and tags.
- **Same recording, other tags**: a stream hash that leaves tags, cover art and
  the container out, to find a retagged song or a remuxed video. These copies
  count towards reclaimable space.
- In Browse, filters by artist, codec and length, a **Music** view, and players
  on file pages.
- The scanner registry: photos, videos, music and audio, and archives each have a
  switch in Setup. With all of them off, deedoop compares raw contents only.

## [0.6.5] - 2026-11-26

Milestone 6.5: hardening and sign-in
([#7](https://github.com/SteveRodrigue/deedoop/pull/7)).

### Added

- One admin account, created on first start with a one-time setup code from the
  container log. Argon2id passwords, expiring sessions, and a throttle after five
  wrong passwords.
- Read-only unless signed in: every change needs the admin. An admin switch
  decides what signed-out visitors see.
- The password is asked again for risky changes after 10 minutes.
- A security log of sign-ins and changes in Setup.
- `deedoop reset-password` from the container.

### Security

- CSRF protection on every change, security headers, and the secure coding rules
  SC1 to SC10 ([docs/secure-coding.md](docs/secure-coding.md)) enforced by ruff
  and tests in CI. The container runs locked down.

### Removed

- Basic auth. An old `DEEDOOP_PASSWORD` becomes the admin password on first start.

## [0.6.0] - 2026-11-25

Milestone 6: remove duplicates and apply plans
([#6](https://github.com/SteveRodrigue/deedoop/pull/6)). Write access to `/mnt`
was turned on only after Steve's OK.

### Added

- The **Changes** screen: mark the copy to keep in each group, or whole filtered
  lists at once, and confirm.
- Quarantine by default (rename into a hidden folder on the same disk, 30 days,
  **Undo**), hardlinks for copies on the same disk, and opt-in permanent delete.
- Checks just before each change: the file is unchanged and the kept copy is
  still there and still the same.
- Applying saved plans.
- Changes need both **Allow changes** in Setup and `/mnt` mounted read-write.

## [0.5.0] - 2026-11-25

Milestone 5: organize files, preview only
([#5](https://github.com/SteveRodrigue/deedoop/pull/5)).

### Added

- Move rules such as `Photos/{year}/{month}/{name}`, with a preview of where
  every file would go, disk-aware moves, saved plans and CSV export.

## [0.4.0] - 2026-11-25

Milestone 4: similar photos ([#4](https://github.com/SteveRodrigue/deedoop/pull/4)).

### Added

- pHash and dHash for every photo, loose or in an archive, to find recompressed
  and resized copies such as Google "Storage saver".
- The **Review** queue with keyboard answers (K, J, B, N, S), stored against the
  image data so they survive rescans.

## [0.3.0] - 2026-11-25

Milestone 3: archives and Google Takeout
([#3](https://github.com/SteveRodrigue/deedoop/pull/3)).

### Added

- Zip, tar and 7z members are read in place, never extracted, and join the
  duplicate report.
- The **Archives** screen: how much of each Takeout is already on disk, with
  multi-part Takeouts shown as one and JSON sidecars matched to their photos.
- Local CI (`make ci`).

## [0.2.0] - 2026-11-25

Milestone 2: photos and metadata ([#2](https://github.com/SteveRodrigue/deedoop/pull/2)).

### Added

- Photos that differ only in their metadata, compared on their image data.
- ExifTool metadata, **Browse** with thumbnails and filters, and file pages.
- Light and dark themes.

## [0.1.0] - 2026-11-25

Milestone 1: exact duplicates, report only
([#1](https://github.com/SteveRodrigue/deedoop/pull/1)).

### Added

- Setup with Unraid shares, disks and pools; scans read each disk, one at a time
  by default, with an optional parallel mode and a benchmark.
- Tiered hashing (size, then a quick hash, then BLAKE3) and an incremental
  SQLite index.
- The duplicate report with filters and CSV or JSON export.

[0.11.1]: https://github.com/SteveRodrigue/deedoop/pulls
[0.11.0]: https://github.com/SteveRodrigue/deedoop/pull/30
[0.10.4]: https://github.com/SteveRodrigue/deedoop/pull/29
[0.10.3]: https://github.com/SteveRodrigue/deedoop/pull/28
[0.10.2]: https://github.com/SteveRodrigue/deedoop/pull/27
[0.10.1]: https://github.com/SteveRodrigue/deedoop/pull/26
[0.10.0]: https://github.com/SteveRodrigue/deedoop/pull/25
[0.9.0]: https://github.com/SteveRodrigue/deedoop/pull/16
[0.8.0]: https://github.com/SteveRodrigue/deedoop/pull/15
[0.7.5]: https://github.com/SteveRodrigue/deedoop/pull/14
[0.7.0]: https://github.com/SteveRodrigue/deedoop/pull/13
[0.6.5]: https://github.com/SteveRodrigue/deedoop/pull/7
[0.6.0]: https://github.com/SteveRodrigue/deedoop/pull/6
[0.5.0]: https://github.com/SteveRodrigue/deedoop/pull/5
[0.4.0]: https://github.com/SteveRodrigue/deedoop/pull/4
[0.3.0]: https://github.com/SteveRodrigue/deedoop/pull/3
[0.2.0]: https://github.com/SteveRodrigue/deedoop/pull/2
[0.1.0]: https://github.com/SteveRodrigue/deedoop/pull/1
