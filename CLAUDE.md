# deedup: notes for Claude and other AI agents

deedup is a duplicate finder for Unraid (Python 3.12, FastAPI + Jinja + htmx,
SQLite). It can move and delete files on someone's array, so security rules
are not optional.

## Always

- Follow the secure coding rules in [docs/secure-coding.md](docs/secure-coding.md)
  (SC1 to SC10) and cite the rule id in comments where it explains a choice.
- Run `make check` while working and `make ci` before every push. Both must pass.
- New state-changing routes are `POST`; the guard in `deedup/web/security.py`
  covers them. Add `{% include "_csrf.html" %}` to every `<form method="post">`.
- File changes on `/mnt` only through `deedup/safepath.py`.
- Archives are never extracted; use `deedup/limits.py` budgets.
- SQL uses `?` placeholders for every value.
- No inline `<script>` or `on…=` attributes in templates; put behaviour in
  `static/app.js`.
- Every security fix ships with a test that fails without it.
- Every screen works in light and dark themes and on a phone.

## Never

- Weaken, skip or delete a security test, or add `# noqa` to a security rule
  without a reason on the same line, to make a build pass.
- Add a dependency without `make lock` and a reason in the PR.
- Log or display passwords, session tokens or setup codes.
- Commit `.local-exiftool`, `.ca-local.crt` or other local-only CI files.

## Layout

- `deedup/web/app.py` routes; `deedup/web/security.py` guard and headers;
  `deedup/auth.py` accounts, sessions, throttling.
- `deedup/changes.py` removals and moves (M6); `deedup/safepath.py` the only
  filesystem writes.
- `deedup/archives.py` + `deedup/limits.py` archive reading.
- `deedup/metadata.py` ExifTool (the only subprocess).
- `tests/test_security.py` the security tests, named by rule and finding.
