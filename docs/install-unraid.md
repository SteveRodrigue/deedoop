# Install deedoop on Unraid (private)

deedoop is not in Community Applications, and its image
`ghcr.io/steverodrigue/deedoop` is private. You install it from the template in
this repo, after signing your Unraid server in to GitHub's registry. Nothing
here publishes anything.

You need: Unraid 6.10 or later, a terminal on the server (the `>_` icon in the
web UI, or SSH), and a GitHub account that can read the package.

Each step below does one thing. Step 1 is on github.com, steps 2 to 5 in the
Unraid terminal, steps 6 to 8 in the Unraid web UI.

## 1. Create a read-only GitHub token (on github.com)

The server signs in to GitHub's registry with a token, not your password.

| What | Value |
|---|---|
| Token type | **Personal access token (classic)**. The registry does not accept fine-grained tokens ([GitHub docs](https://docs.github.com/en/packages/learn-github-packages/about-permissions-for-github-packages#about-scopes-and-permissions-for-package-registries)) |
| Scope | **`read:packages`** only: it can download images and nothing else |
| Expiration | **90 days**. Avoid "No expiration" |

1. Open <https://github.com/settings/tokens/new> (Settings > Developer settings >
   Personal access tokens > **Tokens (classic)**).
2. **Note**: `unraid deedoop pull`. **Expiration**: 90 days.
3. Under **Select scopes**, tick **only** `read:packages`. Leave `repo`,
   `write:packages` and every other box empty.
4. Click **Generate token** and copy it (it starts with `ghp_`). GitHub shows it
   once; you only need it for step 3.

## 2. Open the Unraid terminal

In the Unraid web UI, click the `>_` icon at the top right (or connect with
SSH as `root`). Steps 3 to 5 run here.

## 3. Sign in to ghcr.io with the token

Replace `YOUR_GITHUB_LOGIN`, run these lines, and paste the token when the
cursor waits (nothing is shown):

```sh
read -rs GHCR_TOKEN
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_LOGIN --password-stdin
unset GHCR_TOKEN
```

You should see `Login Succeeded`. The token stays out of the shell history.

**After a reboot this sign-in is gone** (Unraid keeps `/root` in memory).
deedoop keeps running, but updates fail until you repeat this step. To stay
signed in, see [Staying signed in](#staying-signed-in).

## 4. Download the image

```sh
docker pull ghcr.io/steverodrigue/deedoop:latest
```

If this fails with `denied` or `unauthorized`, redo step 3 (wrong login,
expired token, or a scope other than `read:packages`).

## 5. Add the template

Still in the Unraid terminal:

```sh
wget -O /boot/config/plugins/dockerMan/templates-user/my-deedoop.xml \
  https://raw.githubusercontent.com/SteveRodrigue/deedoop/main/unraid/deedoop.xml
```

This is the last terminal command; you can close the terminal. (Without the
terminal: copy [`unraid/deedoop.xml`](../unraid/deedoop.xml) through the
`flash` share as `config/plugins/dockerMan/templates-user/my-deedoop.xml`.)

## 6. Create the container (Unraid web UI)

1. **Docker** tab > **Add Container**, then pick **deedoop** in the **Template**
   list.
2. Check the settings in the table below. The defaults are fine for a first look.
3. Click **Apply**.

| Setting | Default | What it is |
|---|---|---|
| Web UI port | `8080` | Port of the web UI on the server |
| Disks and shares | `/mnt` → `/mnt`, **Read Only - Slave** | What deedoop can see. Read-only means it cannot change a file, whatever happens in the app |
| Appdata | `/mnt/user/appdata/deedoop` → `/config` | The index database and, with HTTPS, the `tls` folder. Keep it on the cache pool |
| Admin password (first run only) | empty | Creates the `admin` account on first start. Ignored afterwards |
| HTTPS | `off` | `on` serves HTTPS on the same port |
| Certificate names (advanced) | empty | Extra names and IPs for the self-signed certificate, like `tower.local,192.168.1.10` |
| Trusted proxies (advanced) | empty | Only behind a reverse proxy such as SWAG or Nginx Proxy Manager |

The template also runs the container as `nobody:users` (99:100) with a
read-only root filesystem, a private `/tmp`, no Linux capabilities and
`no-new-privileges` (**Extra Parameters**, in the advanced view). Leave them.

## 7. Create the admin account

1. Open `http://<server>:8080` (`https://` if you turned HTTPS on). deedoop
   asks for a **setup code**.
2. Get it from the container log: on the **Docker** tab, click the deedoop
   icon > **Logs**, and copy the code at the end of the line
   `deedoop has no admin account yet. … setup code: XXXX-XXXX-XXXX-XXXX`.
   Or in the terminal: `docker logs deedoop 2>&1 | grep "setup code" | tail -1`.
3. Enter the code and click **Continue**. A code lasts 60 minutes; after that,
   reload the page and a new one is written to the log.
4. Choose your user name and password, and click **Create the admin account**.

(Or fill **Admin password (first run only)** in step 6, and remove it once you
have signed in.)

## 8. Start a scan

In deedoop's **Setup**, tick your shares, then start a scan from the
**Dashboard**.

## Letting deedoop change files

Read-only is the default on purpose. To let deedoop quarantine duplicates or
apply plans, both of these are needed:

1. Edit the container and set **Disks and shares** to **Read/Write - Slave**.
2. In deedoop, sign in as the admin and tick **Allow changes** in **Setup**.

Set it back to **Read Only - Slave** whenever you want to be sure nothing
moves.

## HTTPS

Set **HTTPS** to `on`. deedoop uses `cert.pem` and `key.pem` from
`/mnt/user/appdata/deedoop/tls/` if you put them there, otherwise it makes a
self-signed certificate (your browser warns once; Setup shows the fingerprint
to compare). Then edit the container in the advanced view and change
**WebUI** to `https://[IP]:[PORT:8080]/`, so the Docker tab's **WebUI** link
opens the right address. Plain `http://` no longer answers on that port.

## Updating

Unraid checks for updates without your sign-in, so for a private image the
Docker tab may say the update status is **not available** instead of offering
an update. To update:

1. Sign in again if the server rebooted since (step 3).
2. Docker tab, switch to the **Advanced View**, and click **force update** on
   deedoop. Or, in the terminal: `docker pull ghcr.io/steverodrigue/deedoop:latest`,
   then edit the container and click **Apply**.

Each version is also tagged (`ghcr.io/steverodrigue/deedoop:0.9.0`, …). To stay
on one, or to roll back, change **Repository** in the template to that tag.

## Staying signed in

Optional. This keeps the token on the flash drive, which anyone who can read
the `flash` share can copy, so keep that share private (Main > click **Flash** >
**SMB Security Settings** > Export: **No**, or Private) and keep the token to `read:packages`.

1. After `docker login` (step 3), save the sign-in on the flash drive:

   ```sh
   mkdir -p /boot/config/deedoop
   cp /root/.docker/config.json /boot/config/deedoop/docker-config.json
   ```

2. Restore it at boot by adding these lines to `/boot/config/go`, before the
   `emhttp` line:

   ```sh
   mkdir -p /root/.docker
   install -m 600 /boot/config/deedoop/docker-config.json /root/.docker/config.json
   ```

When the token expires, make a new one, sign in again and copy the file again.
To undo: delete `/boot/config/deedoop/docker-config.json`, remove the two lines
from `go`, revoke the token on GitHub.

## The icon

The template's icon is `unraid/deedoop-icon.png` in this repo, loaded from
`raw.githubusercontent.com`. The repo is public, so the icon shows without
publishing the image or listing deedoop anywhere. Unraid downloads it once and
caches it. If the server cannot reach GitHub, the Docker tab shows a generic
icon; nothing else is affected.

## Removing deedoop

Docker tab > deedoop > **Remove** (tick removing the image too). Then delete
`/mnt/user/appdata/deedoop` (the index) and
`/boot/config/plugins/dockerMan/templates-user/my-deedoop.xml`. Files
deedoop put in quarantine live in `.deedoop-quarantine` folders inside your
shares: empty the quarantine in the app first, or delete those folders.
Finally, `docker logout ghcr.io` and revoke the token on GitHub.
