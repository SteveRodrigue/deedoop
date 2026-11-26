# Secure coding rules

deedup holds the keys to someone's whole backup hoard, so every change follows
these rules. Each rule has an id (SC1 to SC10) that code comments, tests and
review use. Where a tool can check a rule, `make ci` and GitHub CI check it,
and a broken rule fails the build.

## Threat model

| Who | How they get in | What stops them |
|---|---|---|
| A web page you visit | Your browser posts a form to deedup on your LAN (CSRF), frames it (clickjacking) or rebinds a DNS name to it | SC1 sign-in + CSRF token + Origin check, `SameSite=Strict` cookies, `frame-ancestors 'none'`, optional host allow-list |
| Another device on your network | Opens the web UI, guesses the password | Signed out is read-only (SC1), Argon2id, lockout per address, slow-down per account |
| A crafted file in your backups | A file name with a line break (ExifTool options), a zip bomb, a symlink swapped in after the scan, a name like `=HYPERLINK()` in a CSV export | SC3, SC4, SC2, SC7 |
| A compromised dependency or image | A malicious or vulnerable package | SC9: hash-pinned lock file, pip-audit, Trivy, Dependabot, digest-pinned base image |

deedup does not defend against someone with a shell on the Unraid server:
they can already read and change every file.

## The rules

### SC1 Writes need sign-in

A route that changes anything uses `POST` (or another non-GET method). The
app-wide guard in `deedup/web/security.py` then requires the signed-in admin,
a valid CSRF token and a same-site `Origin`. Never add a route that bypasses
the guard, and never change state in a `GET` (the thumbnail cache in
`/config/thumbs` is the one named exception).

Every `<form method="post">` starts with `{% include "_csrf.html" %}`.
Dangerous actions (turning on changes or permanent delete, permanent delete,
emptying the quarantine, changing the password) call `_confirm()` so the
password is asked again after 10 minutes.

*Checked by:* `test_every_non_get_route_needs_sign_in`,
`test_no_get_route_writes_to_the_index`, `test_every_post_form_carries_the_csrf_token`.

### SC2 Files on /mnt only through `deedup.safepath`

Renames, links, deletes and new files on `/mnt` go through `safepath`, which
opens every folder of the path with `O_NOFOLLOW` and acts through folder
handles, so a symlink swapped in after the scan is refused instead of
followed. Moves never overwrite.

```python
safepath.rename_noclobber(src, dst)   # right
os.rename(src, dst)                   # wrong: follows symlinks, overwrites
```

*Checked by:* ruff `TID251` bans `os.rename`, `os.replace`, `os.link`,
`os.unlink`, `os.remove`, `os.rmdir`, `os.mkdir`, `shutil.move` and
`shutil.rmtree` outside `safepath.py`; `test_safepath_never_follows_a_swapped_folder`.

### SC3 No shell, and checked arguments

Subprocesses run in two modules only: ExifTool in `metadata.py`, and ffprobe
and ffmpeg in `av.py` (milestone 7). Arguments are a list of fixed options
plus paths that passed `safe_for_exiftool()` or `safe_for_ffmpeg()`
(absolute, no line breaks or control characters). Never `shell=True`, never
`os.system`.

ffmpeg can follow references inside a media file (an HLS playlist or an
`ffconcat` list names other files or URLs), so a crafted file in the backups
could make it read something else. Every ffprobe and ffmpeg call therefore
opens only local files (`-protocol_whitelist file`), only with the demuxers of
ordinary media files (`-format_whitelist`), gives the path as `file:/…`, and
has a time limit.

*Checked by:* ruff `S6xx` rules and `TID251` (`subprocess`, `os.system`,
`os.popen` banned outside `metadata.py` and `av.py`);
`test_a_file_name_cannot_add_exiftool_options`, `test_a_file_name_cannot_add_ffmpeg_options`,
`test_a_crafted_media_file_cannot_open_other_files`.

### SC4 Archives are read, never extracted

Members are streamed into hashes in memory. No `extractall`, no writing
members to disk. Every reader counts against a `limits.Budget`; an archive
over a limit is recorded as "too large to index safely". If extraction is
ever needed, it must use `tarfile`'s `filter="data"` and a `safepath` join.

*Checked by:* `test_zip_bomb_is_skipped_quickly`, `test_nested_bomb_counts_towards_its_parent`,
`test_no_extractall` (below).

### SC5 SQL takes parameters

User values go into `?` placeholders. An f-string in SQL may only splice
module constants or a list of `?`, and says so in a comment. ruff `S608` is
on everywhere; the modules that build SQL from constants are listed in
`pyproject.toml` and reviewed when they change.

```python
conn.execute("SELECT * FROM files WHERE name = ?", (name,))    # right
conn.execute(f"SELECT * FROM files WHERE name = '{name}'")      # wrong
```

### SC6 Templates escape

Jinja autoescape stays on. No `|safe`, `Markup` or `autoescape false`. No
inline scripts or `on…=` handlers: behaviour goes in `static/app.js` with
`data-` attributes. The Content-Security-Policy allows only `'self'` scripts
plus the hashed theme snippet in `base.html` (the hash is computed at start).

*Checked by:* `test_templates_escape_and_have_no_inline_script`, `test_security_headers`.

### SC7 Validate at the edge

Every query and form value has a type and bounds (`Annotated[int, Query(ge=, le=)]`,
`Form(max_length=)`). Redirect targets go through `safe_next()`. Values that
leave deedup are escaped for where they land (`csv_cell()` for CSV).

*Checked by:* `test_redirects_stay_local`, `test_csv_cells_cannot_be_formulas`.

### SC8 Secrets stay secret

Passwords, session tokens and setup codes never appear in logs, error
messages, URLs or the database in plain form. Passwords are Argon2id hashes,
session tokens are stored as SHA-256. `auth.event()` lines say what happened,
never the secret. Compare secrets with `secrets.compare_digest`.

With built-in HTTPS on (`tls.py`), the private key deedup makes is written
owner-only (0600) and atomically, so it never exists with looser permissions;
the server accepts TLS 1.2 and 1.3 only, with forward-secret AEAD ciphers. A
certificate that does not load falls back to the self-signed one rather than
to plain HTTP. The scan log takes warnings from the scan modules only, never
from the sign-in code, so it cannot hold a setup code or password.

*Checked by:* ruff `S105`/`S106`/`S107`, `test_session_cookie_flags_and_sign_out`,
`test_self_signed_key_is_owner_only`, `test_https_refuses_old_tls`,
`test_scan_log_never_takes_sign_in_messages`.

### SC9 Pinned and audited dependencies

A new dependency needs a reason in the PR, an entry in `pyproject.toml` and
a regenerated `requirements.lock` (`make lock`), which the image installs with
`--require-hashes`. The base image is pinned by digest, GitHub Actions by
commit SHA. Dependabot proposes updates weekly.

*Checked by:* `make audit` (pip-audit on the lock file) and Trivy (image scan)
in `make ci`; CodeQL on GitHub.

### SC10 Every control has a test

A security fix ships with a test that fails without it. Never weaken,
skip or delete a security test to make a build pass; fix the code instead.

## Container

The image runs as `nobody:users` (99:100) with no `pip`. The Unraid template
and `docker-compose.yml` add a read-only root filesystem, `/tmp` as tmpfs,
`--cap-drop=ALL` and `no-new-privileges`. The smoke test in `make ci` checks
all of these.
