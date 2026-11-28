# Contributing to deedoop

Thanks for helping. A few things keep deedoop safe for people's backups.

## Before you push

```sh
make check   # ruff (with the security rules) and pytest, on this machine
make ci      # the full run GitHub does: tests in Docker, pip-audit, image build, Trivy, smoke test
```

`make ci` needs only Docker. A pull request is merged only when it passes.
See [docs/development.md](docs/development.md) for running from source and what
`make ci` checks.

## Changelog and version

A pull request that changes behaviour bumps the version in `pyproject.toml` and
`deedoop/__init__.py` and adds an entry under that version in
[CHANGELOG.md](CHANGELOG.md). Ideas for later go in [ROADMAP.md](ROADMAP.md).

## Security rules

Read [docs/secure-coding.md](docs/secure-coding.md). In short:

1. **SC1** Changes are `POST`, behind the sign-in and CSRF guard; `GET` never writes.
2. **SC2** Files on `/mnt` only through `deedoop.safepath`.
3. **SC3** No shell; ExifTool gets checked arguments only.
4. **SC4** Archives are read, never extracted, within `deedoop.limits`.
5. **SC5** SQL takes `?` parameters.
6. **SC6** Templates escape; no inline scripts.
7. **SC7** Every input typed and bounded; redirects local only.
8. **SC8** No secrets in logs, URLs or the database in plain form.
9. **SC9** New dependencies are justified, hash-pinned (`make lock`) and audited.
10. **SC10** Every security control has a test.

The pull request template has a checklist for these.

## Dependencies

Edit `pyproject.toml`, then run `make lock` to regenerate `requirements.lock`
with hashes, and commit both.

## Reporting a vulnerability

See [SECURITY.md](SECURITY.md). Please don't open a public issue.
