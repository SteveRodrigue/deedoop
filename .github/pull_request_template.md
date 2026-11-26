## What and why

<!-- What does this change, and why? Link the milestone or issue. -->

## Security checklist (docs/secure-coding.md)

- [ ] SC1: new state-changing routes are POST; every new POST form includes `_csrf.html`; no GET writes
- [ ] SC2: file changes on /mnt only through `deedup.safepath`
- [ ] SC3: no new subprocess or shell; ExifTool arguments checked
- [ ] SC4: archives read within `deedup.limits`, never extracted
- [ ] SC5: SQL values passed as `?` parameters
- [ ] SC6: no `|safe`, `Markup`, inline scripts or `on…=` handlers
- [ ] SC7: new inputs typed and bounded; redirects through `safe_next`
- [ ] SC8: no secrets in logs, URLs or messages
- [ ] SC9: new dependencies justified, `make lock` run, pip-audit clean
- [ ] SC10: security changes have tests that fail without them
- [ ] `make ci` passes; screens checked in light and dark, desktop and phone
