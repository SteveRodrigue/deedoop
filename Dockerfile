# deedup: dedupe your hoard, safely.
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY deedup ./deedup
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
LABEL org.opencontainers.image.title="deedup" \
      org.opencontainers.image.description="Duplicate finder for Unraid: dedupe your hoard, safely." \
      org.opencontainers.image.source="https://github.com/SteveRodrigue/deedup" \
      org.opencontainers.image.licenses="Apache-2.0"
# ExifTool reads photo and video metadata (milestone 2).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libimage-exiftool-perl \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /install /usr/local
ENV DEEDUP_CONFIG=/config \
    DEEDUP_MNT=/mnt \
    DEEDUP_PORT=8080 \
    PYTHONUNBUFFERED=1
RUN mkdir -p /config && chown 99:100 /config
# Unraid's nobody:users. Override with --user if your files belong to someone else.
USER 99:100
VOLUME ["/config"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"DEEDUP_PORT\",\"8080\")}/healthz', timeout=4)"
ENTRYPOINT ["python", "-m", "deedup"]
