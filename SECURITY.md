# Security policy

## What deedoop promises

- **Read-only unless you open the gate.** deedoop cannot change a file until you
  mount `/mnt` read-write in the container settings **and** tick "Allow changes"
  in Setup **and** confirm a list of changes on the Changes screen.
- **Signed out means read-only.** Every request that changes anything (files,
  the index, marks, plans, settings, scans) needs the signed-in admin. Signed
  out, visitors see only the scan status and the sign-in form, unless the admin
  turns on "Signed-out visitors can browse (read-only)".
- **Removals are reversible by default.** Files go to a quarantine on the same
  disk and can be put back for 30 days. Permanent delete is opt-in and asks for
  the password again.
- **Archives are never extracted.** Zip, tar and 7z files are read in place
  with limits on size, file count and expansion.
- **No secrets in plain form.** Passwords are stored as Argon2id hashes and
  session cookies as SHA-256 hashes. Neither appears in logs.

## Supported versions

Only the latest release gets security fixes. Images are tagged with their
version (for example `ghcr.io/steverodrigue/deedoop:0.8.0`) and `latest`.

## Reporting a vulnerability

Please report it privately through GitHub:
**Security > Report a vulnerability** on
<https://github.com/SteveRodrigue/deedoop/security/advisories/new>.
Do not open a public issue. Include what you found, how to reproduce it and
which version you ran. You will get an answer within a week.

## Running deedoop safely

- Keep `/mnt` read-only unless you are about to remove duplicates or apply a plan.
- deedoop speaks plain HTTP on your LAN unless you set `DEEDOOP_HTTPS=on`; then
  it serves HTTPS (TLS 1.2 and 1.3) with your certificate, or a self-signed one
  whose fingerprint the Setup screen shows, and marks its cookie `Secure`.
- For access from outside your home, use a VPN (Tailscale, WireGuard) or a
  reverse proxy with HTTPS (SWAG, Nginx Proxy Manager), and set
  `DEEDOOP_TRUSTED_PROXIES` to the proxy's address so deedoop sees the real
  client address and marks its cookie `Secure`.
- Forgot the admin password? From the Unraid terminal:
  `docker exec -it deedoop deedoop reset-password`.

For contributors, the rules that keep these promises are in
[docs/secure-coding.md](docs/secure-coding.md).
