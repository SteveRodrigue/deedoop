# Install deedoop on Unraid (private)

deedoop is not in Community Applications, and its image
`ghcr.io/steverodrigue/deedoop` is private. You install it from the template in
this repo, after signing your Unraid server in to GitHub's registry. Nothing
here publishes anything.

You need: Unraid 6.10 or later, a terminal on the server (the `>_` icon in the
web UI, or SSH), and a GitHub account that can read the package.

## 1. Make a token that can only read packages

The server signs in to GitHub's container registry (`ghcr.io`) with a GitHub
personal access token instead of your password. This token needs exactly one
permission, or "scope":

| What | Value |
|---|---|
| Token type | **Personal access token (classic)**. GitHub's registry does not accept fine-grained tokens ("GitHub Packages only supports authentication using a personal access token (classic)", [GitHub docs](https://docs.github.com/en/packages/learn-github-packages/about-permissions-for-github-packages#about-scopes-and-permissions-for-package-registries)) |
| Scope | **`read:packages`** only. It lets the token download container images and nothing else: it cannot push or delete images, or read or change your code, issues or settings |
| Expiration | **90 days** is a good default. You only need it to install and update; when it expires, make a new one and sign in again (step 2) |
| Account | One that can read the package. The owner's own account (SteveRodrigue) always can |

Do **not** tick `repo`, `write:packages` or `delete:packages`. A pull does not
need them, and ticking `write:packages` makes GitHub tick `repo` too, which gives
the token read and write access to all your repositories.

1. Open <https://github.com/settings/tokens/new> (Settings > Developer settings >
   Personal access tokens > **Tokens (classic)** > Generate new token >
   Generate new token (classic)). Not "Fine-grained tokens".
2. **Note**: `unraid deedoop pull`, so you know later what it is for.
3. **Expiration**: 90 days (or a custom date). Avoid "No expiration".
4. Under **Select scopes**, tick **only** `read:packages`. Leave every other box
   empty, including `repo`.
5. Click **Generate token** and copy it (it starts with `ghp_`). GitHub shows it
   only once. You paste it in step 2 and do not need to keep it after that.

The token stands in for your GitHub password, for `ghcr.io` only: in step 2 you give
Docker your GitHub login as the user name and the token as the password. Unraid's
Docker tab has no field for registry credentials, so this sign-in happens once
in the terminal, and **Apply** and **force update** then use it.

To revoke it at any time: <https://github.com/settings/tokens>, click the token,
**Delete**. Nothing else on GitHub is affected.

## 2. Sign the server in to ghcr.io

In the Unraid terminal (replace `YOUR_GITHUB_LOGIN`):

```sh
read -rs GHCR_TOKEN   # paste the ghp_ token from step 1, press Enter (nothing is shown)
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_LOGIN --password-stdin
unset GHCR_TOKEN
docker pull ghcr.io/steverodrigue/deedoop:latest   # checks the sign-in works
```

`--password-stdin` keeps the token out of the shell history and the process
list. Docker saves the sign-in in `/root/.docker/config.json`.

**After a reboot the sign-in is gone.** Unraid keeps `/root` in memory. deedoop
keeps running (the image is already on the server), but pulling an update fails
until you run `docker login` again. That is the simplest and safest setup:
sign in, update, done. If you would rather stay signed in, see
[Staying signed in](#staying-signed-in) below.

## 3. Add the template

Still in the **Unraid terminal** from step 2 (the `>_` icon, or SSH), run:

```sh
wget -O /boot/config/plugins/dockerMan/templates-user/my-deedoop.xml \
  https://raw.githubusercontent.com/SteveRodrigue/deedoop/main/unraid/deedoop.xml
```

This saves the template on the server's flash drive, where the Docker tab
looks for templates. That is the last terminal command: step 4 happens in the
web UI.

(Or, without the terminal, copy [`unraid/deedoop.xml`](../unraid/deedoop.xml)
there through the `flash` share as
`config/plugins/dockerMan/templates-user/my-deedoop.xml`.)

## 4. Create the container

Back in the Unraid **web UI**:

1. **Docker** tab > **Add Container**, then pick **deedoop** in the **Template**
   list.
2. Check the settings (table below). The defaults are fine for a first look.
3. Click **Apply**. Unraid pulls the image with the sign-in from step 2.
4. Open the container's **Logs** and copy the one-time setup code.
5. Open `http://<server>:8080` (`https://` if you turned HTTPS on), enter the
   code and create the admin account. Or fill **Admin password (first run
   only)** before Apply, and remove it once you have signed in.
6. In **Setup**, tick your shares, then start a scan from the **Dashboard**.

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

### Letting deedoop change files

Read-only is the default on purpose. To let deedoop quarantine duplicates or
apply plans, both of these are needed:

1. Edit the container and set **Disks and shares** to **Read/Write - Slave**.
2. In deedoop, sign in as the admin and tick **Allow changes** in **Setup**.

Set it back to **Read Only - Slave** whenever you want to be sure nothing
moves.

### HTTPS

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

1. Sign in again if the server rebooted since (step 2).
2. Docker tab, switch to the **Advanced View**, and click **force update** on
   deedoop. Or, in the terminal: `docker pull ghcr.io/steverodrigue/deedoop:latest`,
   then edit the container and click **Apply**.

Each version is also tagged (`ghcr.io/steverodrigue/deedoop:0.9.0`, …). To stay
on one, or to roll back, change **Repository** in the template to that tag.

## Staying signed in

Optional. This keeps the token on the flash drive, which anyone who can read
the `flash` share can copy, so keep that share private (Main > click **Flash** >
**SMB Security Settings** > Export: **No**, or Private) and keep the token to `read:packages`.

1. After `docker login` (step 2), save the sign-in on the flash drive:

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
