#!/usr/bin/env bash
# Local CI: the same checks GitHub runs on every pull request.
#
#   scripts/ci.sh          lint + tests (in Docker), pip-audit, image build, Trivy, smoke test
#   scripts/ci.sh quick    lint + tests with the Python on this machine
#   scripts/ci.sh audit    pip-audit of requirements.lock (needs the test image or pip-audit here)
#   scripts/ci.sh smoke    start an already built image and check it
#
# Only Docker is needed for the full run. Set DOCKERFILE to build another file.
# TRIVY=0 skips the image scan (only for machines that cannot reach its database;
# GitHub CI always runs it).
set -euo pipefail
cd "$(dirname "$0")/.."

DOCKERFILE=${DOCKERFILE:-Dockerfile}
IMAGE=${IMAGE:-deedup:ci}
TRIVY=${TRIVY:-1}
# Extra options for docker build / docker run, for machines behind a proxy.
read -r -a BUILD_OPTS <<< "${BUILD_OPTS:-}"
read -r -a RUN_OPTS <<< "${RUN_OPTS:-}"
TRIVY_IMAGE=${TRIVY_IMAGE:-aquasec/trivy:0.76.2@sha256:8f6bb947dbc528902e11f2f5be52f7b519f6a39d5d148f41166f2040499768fb}
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

quick() {
  step "ruff (including the security rules S and TID251)"
  ruff check .
  step "pytest"
  if ! command -v exiftool >/dev/null; then
    echo "note: exiftool is not installed here, so metadata tests are skipped; the full run has it"
  fi
  if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
    echo "note: ffmpeg is not installed here, so video and audio tests are skipped; the full run has it"
  fi
  pytest -q
}

audit() {
  step "pip-audit (SC9)"
  if docker image inspect "$IMAGE-test" >/dev/null 2>&1; then
    docker run --rm "${RUN_OPTS[@]}" --entrypoint pip-audit "$IMAGE-test" --cache-dir /tmp/pip-audit \
      -r /src/requirements.lock --require-hashes \
      --disable-pip --progress-spinner off
  else
    pip-audit -r requirements.lock --require-hashes --disable-pip --progress-spinner off
  fi
}

trivy() {
  step "Trivy image scan (SC9)"
  if [ "$TRIVY" = 0 ]; then
    echo "  skipped (TRIVY=0)"
    return
  fi
  docker run --rm "${RUN_OPTS[@]}" -v /var/run/docker.sock:/var/run/docker.sock "$TRIVY_IMAGE" image --quiet \
    --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed "$IMAGE"
}

full() {
  command -v docker >/dev/null || { echo "Docker is required (or run: scripts/ci.sh quick)"; exit 1; }
  step "lint and tests (Docker test stage)"
  docker build "${BUILD_OPTS[@]}" -f "$DOCKERFILE" --target test -t "$IMAGE-test" .
  audit
  step "image"
  docker build "${BUILD_OPTS[@]}" -f "$DOCKERFILE" -t "$IMAGE" .
  trivy
  smoke
  step "all checks passed"
}

smoke() {
  step "smoke test"
  local name="deedup-ci-$$"
  # An empty, read-only /mnt is enough to start the app and render every page.
  # Locked down as the Unraid template runs it.
  docker run -d --name "$name" --read-only --tmpfs /tmp --tmpfs /config:uid=99,gid=100 \
    --cap-drop=ALL --security-opt=no-new-privileges --tmpfs /mnt:ro,size=1m "$IMAGE" >/dev/null
  local probe='import urllib.request as u, urllib.error as e
base = "http://127.0.0.1:8080"
for p in ("/healthz", "/login", "/", "/setup", "/duplicates", "/browse", "/review", "/archives", "/reorganize",
          "/plans", "/changes", "/same-path", "/browse?view=music", "/duplicates?method=media"):
    r = u.urlopen(base + p, timeout=4)
    assert r.status == 200, p
    print("  ok", p, "->", r.url.replace(base, ""))
r = u.urlopen(base + "/login", timeout=4)
h = r.headers
assert "frame-ancestors" in h["Content-Security-Policy"] and h["X-Frame-Options"] == "DENY", dict(h)
assert h["X-Content-Type-Options"] == "nosniff"
print("  ok security headers")
class NoRedirect(u.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None
try:
    u.build_opener(NoRedirect).open(u.Request(base + "/setup/changes", data=b"allow_changes=1",
                                              headers={"Accept": "text/html"}), timeout=4)
    raise SystemExit("a signed-out POST was accepted")
except e.HTTPError as err:
    assert err.code == 303 and err.headers["Location"].startswith("/login"), err.code
print("  ok signed-out POST refused")'
  local ok=0
  for _ in $(seq 1 30); do
    if docker exec "$name" python -c "$probe" 2>/dev/null; then ok=1; break; fi
    sleep 1
  done
  if [ "$ok" = 1 ]; then
    docker exec "$name" python -c "$probe" >/dev/null
    [ "$(docker exec "$name" id -u)" = 99 ] && echo "  ok runs as uid 99" || ok=0
    if docker exec "$name" sh -c 'touch /usr/local/x' 2>/dev/null; then echo "  root filesystem is writable"; ok=0
    else echo "  ok root filesystem is read-only"; fi
    if docker exec "$name" python -c 'import pip' 2>/dev/null; then echo "  pip is in the image"; ok=0
    else echo "  ok no pip in the image"; fi
    if docker exec "$name" sh -c 'ffprobe -version && ffmpeg -version && exiftool -ver' >/dev/null 2>&1; then
      echo "  ok ffprobe, ffmpeg and exiftool in the image"
    else echo "  ffprobe, ffmpeg or exiftool missing"; ok=0; fi
    if docker logs "$name" 2>&1 | grep -q "setup code"; then echo "  ok setup code in the log"
    else echo "  no setup code in the log"; ok=0; fi
  fi
  if [ "$ok" != 1 ]; then
    docker logs "$name" 2>&1 | tail -40
    echo "smoke test failed"
  fi
  docker stop -t 5 "$name" >/dev/null || true
  docker container rm "$name" >/dev/null 2>&1 || true
  [ "$ok" = 1 ]
}

case "${1:-full}" in
  full) full ;;
  quick) quick ;;
  audit) audit ;;
  smoke) smoke ;;
  *) echo "usage: $0 [full|quick|audit|smoke]"; exit 2 ;;
esac
