# deedup: dedupe your hoard, safely.
# SC9: the base image is pinned by digest and Python packages by hash
# (requirements.lock, made with `make lock`). Dependabot proposes updates.
FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b AS build
WORKDIR /src
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes --prefix=/install -r requirements.lock
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY deedup ./deedup
RUN pip install --no-cache-dir --no-deps --prefix=/install .

# What the app needs at run time, shared by the test stage and the image.
FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b AS base
# ExifTool reads photo and video metadata; ffmpeg (with ffprobe) reads video and
# audio for milestone 7. `upgrade` pulls in Debian security
# fixes newer than the pinned base image (Trivy fails the build without them).
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends libimage-exiftool-perl ffmpeg \
 && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

# Lint and tests on the same system as the image. `make ci` builds this
# stage; a plain `docker build .` skips it.
FROM base AS test
WORKDIR /src
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY deedup ./deedup
RUN pip install --no-cache-dir -e '.[dev]'
COPY tests ./tests
# Not root, like the image, so permission tests behave as they do on Unraid.
RUN chown -R 99:100 /src
USER 99:100
RUN ruff check --no-cache . && pytest -q -p no:cacheprovider

FROM base
LABEL org.opencontainers.image.title="deedup" \
      org.opencontainers.image.description="Duplicate finder for Unraid: dedupe your hoard, safely." \
      org.opencontainers.image.source="https://github.com/SteveRodrigue/deedup" \
      org.opencontainers.image.licenses="Apache-2.0"
COPY --from=build /install /usr/local
# No package manager at run time: nothing can be installed into a running container.
RUN pip uninstall -y -q pip setuptools wheel 2>/dev/null; rm -rf /usr/local/lib/python3.12/ensurepip \
 && mkdir -p /config && chown 99:100 /config
ENV DEEDUP_CONFIG=/config \
    DEEDUP_MNT=/mnt \
    DEEDUP_PORT=8080
# Unraid's nobody:users. Override with --user if your files belong to someone else.
USER 99:100
VOLUME ["/config"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"DEEDUP_PORT\",\"8080\")}/healthz', timeout=4)"
ENTRYPOINT ["python", "-m", "deedup"]
