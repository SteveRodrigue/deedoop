# Configuration

How the container is set up: paths, permissions, environment variables, HTTPS and
building the image yourself. The step-by-step Unraid install is in
[install-unraid.md](install-unraid.md). Everything else (shares, exclusions, disk
mode, schedule, scanners, sign-in options) is set in the app on the Setup screen.

## Paths and permissions

The template maps:

| Container | Host | Mode |
|---|---|---|
| `/mnt` | `/mnt` | **read-only** (set it to read/write to allow changes; see below) |
| `/config` | `/mnt/user/appdata/deedoop` | read-write (index database) |
| `8080` | `8080` | web UI |

To let deedoop remove duplicates or apply plans, edit the container, set the `/mnt` path's access mode to **Read/Write - Slave**, then sign in and tick **Allow changes** on the Setup screen. Both are needed; until then deedoop cannot change a file.

The container runs as `nobody:users` (99:100) with a read-only root filesystem, no Linux capabilities and `no-new-privileges`. Forgot the admin password? From the Unraid terminal: `docker exec -it deedoop deedoop reset-password`. For HTTPS on your network, set **HTTPS** to `on` in the template (see [Environment variables](#environment-variables)). For access from outside your home, use a VPN or a reverse proxy with HTTPS and set **Trusted proxies**; see [SECURITY.md](../SECURITY.md).

## Building the image

The image is built by GitHub Actions and published to `ghcr.io/steverodrigue/deedoop:latest` and `ghcr.io/steverodrigue/deedoop:<version>` on every push to `main`. To build it yourself:

```sh
docker build -t deedoop .
docker run -d --name deedoop -p 8080:8080 --user 99:100 \
  --read-only --tmpfs /tmp --cap-drop=ALL --security-opt=no-new-privileges \
  -v /mnt:/mnt:ro,rslave -v /mnt/user/appdata/deedoop:/config deedoop
```

See [`docker-compose.yml`](../docker-compose.yml) for a Compose example.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `DEEDOOP_CONFIG` | `/config` | Where the index database lives |
| `DEEDOOP_MNT` | `/mnt` | Where the host's `/mnt` is mounted |
| `DEEDOOP_PORT` | `8080` | Web UI port |
| `DEEDOOP_ADMIN_PASSWORD` | (none) | First run only: creates the admin (user name `admin`). Ignored once an admin exists |
| `DEEDOOP_TRUSTED_PROXIES` | (none) | Comma-separated reverse-proxy addresses or networks whose `X-Forwarded-For`/`-Proto` headers are believed |
| `DEEDOOP_HTTPS` | `off` | `on` serves HTTPS on the web UI port instead of HTTP |
| `DEEDOOP_TLS_CERT`, `DEEDOOP_TLS_KEY` | `/config/tls/cert.pem`, `/config/tls/key.pem` | Your certificate (with any intermediates) and its key. If missing or unusable, deedoop uses a self-signed certificate in `/config/tls/self-signed.*` |
| `DEEDOOP_TLS_NAMES` | (none) | Comma-separated extra host names and IP addresses for the self-signed certificate, for example `tower.local,192.168.1.10` |

With HTTPS on, open deedoop with `https://`; plain `http://` on the same port does not answer. On Unraid, also change the template's **WebUI** link to `https://[IP]:[PORT:8080]/`. The self-signed certificate is valid for about 13 months and is renewed on a container restart within its last 30 days. deedoop does not send HSTS: with a self-signed certificate it would make the browser warning impossible to pass.

**Upgrading from 0.6.0 or earlier:** `DEEDOOP_PASSWORD` was the basic-auth password before 0.6.5. If you had set the old **Password** (`DEEDOOP_PASSWORD`), it becomes the admin password (user name `admin`) on the first start; then remove the variable. Basic auth is gone.
