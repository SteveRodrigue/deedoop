#!/usr/bin/env bash
# Local CI: the same checks GitHub runs on every pull request.
#
#   scripts/ci.sh          lint + tests (in Docker), image build, smoke test
#   scripts/ci.sh quick    lint + tests with the Python on this machine
#   scripts/ci.sh smoke    start an already built image and load every page
#
# Only Docker is needed for the full run. Set DOCKERFILE to build another file.
set -euo pipefail
cd "$(dirname "$0")/.."

DOCKERFILE=${DOCKERFILE:-Dockerfile}
IMAGE=${IMAGE:-deedup:ci}
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

quick() {
  step "ruff"
  ruff check .
  step "pytest"
  if ! command -v exiftool >/dev/null; then
    echo "note: exiftool is not installed here, so metadata tests are skipped; the full run has it"
  fi
  pytest -q
}

full() {
  command -v docker >/dev/null || { echo "Docker is required (or run: scripts/ci.sh quick)"; exit 1; }
  step "lint and tests (Docker test stage)"
  docker build -f "$DOCKERFILE" --target test -t "$IMAGE-test" .
  step "image"
  docker build -f "$DOCKERFILE" -t "$IMAGE" .
  smoke
  step "all checks passed"
}

smoke() {
  step "smoke test"
  local name="deedup-ci-$$"
  # An empty, read-only /mnt is enough to start the app and render every page.
  docker run -d --name "$name" --tmpfs /mnt:ro,size=1m "$IMAGE" >/dev/null
  local probe='import urllib.request as u
for p in ("/healthz", "/", "/setup", "/duplicates", "/browse", "/review", "/archives", "/reorganize", "/plans", "/same-path"):
    r = u.urlopen("http://127.0.0.1:8080" + p, timeout=4)
    assert r.status == 200, p
    print("  ok", p)'
  local ok=0
  for _ in $(seq 1 30); do
    if docker exec "$name" python -c "$probe" 2>/dev/null; then ok=1; break; fi
    sleep 1
  done
  if [ "$ok" = 1 ] && [ "$(docker exec "$name" id -u)" = 99 ]; then
    echo "  ok runs as uid 99"
  else
    docker logs "$name" 2>&1 | tail -40
    echo "smoke test failed"
    ok=0
  fi
  docker stop -t 5 "$name" >/dev/null || true
  docker container rm "$name" >/dev/null 2>&1 || true
  [ "$ok" = 1 ]
}

case "${1:-full}" in
  full) full ;;
  quick) quick ;;
  smoke) smoke ;;
  *) echo "usage: $0 [full|quick|smoke]"; exit 2 ;;
esac
