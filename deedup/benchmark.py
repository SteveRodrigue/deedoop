"""Disk and CPU benchmark that proposes the parallel-disk settings.

1. Hash speed: BLAKE3 over an in-memory buffer, one thread and all cores.
2. Each disk alone: sequential reads of large sample files.
3. Growing combinations: the first 2, 3, ... disks read at the same time.

When reading k disks together still gives at least 80% of the sum of their
solo speeds, the controller and CPU keep up and k disks in parallel is worth
it. The proposal is the largest such k (capped by CPU cores). Nothing is
written to the disks; the page cache is dropped for the sample files before
each read so the numbers are real disk reads.
"""

from __future__ import annotations

import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import blake3

from .control import Control
from .discovery import natural_key

MIN_SAMPLE_FILE = 4 * 1024 * 1024
CHUNK = 4 * 1024 * 1024
EFFICIENCY = 0.80


@dataclass
class BenchConfig:
    seconds_per_test: float = 8.0
    bytes_per_test: int = 1024 * 1024 * 1024
    max_combo: int = 8
    max_entries_visited: int = 20000
    min_sample_file: int = MIN_SAMPLE_FILE


@dataclass
class DiskSample:
    disk: str
    files: list[str] = field(default_factory=list)
    bytes: int = 0


def find_samples(disk: str, paths: list[str], cfg: BenchConfig, want: int) -> DiskSample:
    sample = DiskSample(disk)
    visited = 0
    stack = list(paths)
    while stack and sample.bytes < want and visited < cfg.max_entries_visited:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    visited += 1
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if not e.name.startswith("."):
                                stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            size = e.stat(follow_symlinks=False).st_size
                            if size >= cfg.min_sample_file:
                                sample.files.append(e.path)
                                sample.bytes += size
                    except OSError:
                        continue
        except OSError:
            continue
    return sample


def _drop(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
    except (OSError, AttributeError):
        pass


def read_test(sample: DiskSample, cfg: BenchConfig, control: Control | None) -> tuple[int, float]:
    """Read the sample files sequentially until the byte or time budget is spent."""
    for f in sample.files:
        _drop(f)
    total = 0
    start = time.monotonic()
    deadline = start + cfg.seconds_per_test
    for f in sample.files:
        try:
            fd = os.open(f, os.O_RDONLY)
        except OSError:
            continue
        try:
            while True:
                if control:
                    control.checkpoint()
                b = os.read(fd, CHUNK)
                if not b:
                    break
                total += len(b)
                if total >= cfg.bytes_per_test or time.monotonic() >= deadline:
                    break
        finally:
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            except (OSError, AttributeError):
                pass
            os.close(fd)
        if total >= cfg.bytes_per_test or time.monotonic() >= deadline:
            break
    return total, max(time.monotonic() - start, 1e-6)


def hash_speed(threads: int, size: int = 256 * 1024 * 1024) -> float:
    buf = os.urandom(16 * 1024 * 1024)
    h = blake3.blake3(max_threads=threads) if threads > 1 else blake3.blake3()
    start = time.monotonic()
    done = 0
    while done < size:
        h.update(buf)
        done += len(buf)
    return done / max(time.monotonic() - start, 1e-6)


def mbps(b: float) -> float:
    return round(b / 1e6, 1)


def run_benchmark(disk_paths: dict[str, list[str]], cfg: BenchConfig | None = None,
                  control: Control | None = None, progress=None) -> dict:
    """``disk_paths`` maps a disk name to the folders to sample on it."""
    cfg = cfg or BenchConfig()
    cpus = os.cpu_count() or 1
    say = progress or (lambda **kw: None)

    say(step="Measuring hash speed")
    h1 = hash_speed(1)
    hn = hash_speed(cpus) if cpus > 1 else h1

    samples: list[DiskSample] = []
    skipped: list[dict] = []
    for disk, paths in disk_paths.items():
        say(step=f"Finding sample files on {disk}")
        s = find_samples(disk, paths, cfg, want=cfg.bytes_per_test)
        if s.files:
            samples.append(s)
        else:
            skipped.append({"disk": disk, "reason": "no files of 4 MB or more found"})

    solo: dict[str, float] = {}
    for s in samples:
        say(step=f"Reading {s.disk} alone")
        n, secs = read_test(s, cfg, control)
        solo[s.disk] = n / secs

    combos = []
    k_max = min(len(samples), cfg.max_combo)
    for k in range(2, k_max + 1):
        group = samples[:k]
        say(step=f"Reading {k} disks at once")
        with ThreadPoolExecutor(max_workers=k) as pool:
            results = list(pool.map(lambda s: read_test(s, cfg, control), group))
        agg = sum(n for n, _ in results) / max(secs for _, secs in results)
        expected = sum(solo[s.disk] for s in group)
        combos.append({
            "disks": [s.disk for s in group],
            "aggregate_mbps": mbps(agg),
            "sum_of_solo_mbps": mbps(expected),
            "efficiency": round(agg / expected, 2) if expected else 0,
        })

    best_k = 1
    for c in combos:
        if c["efficiency"] >= EFFICIENCY and len(c["disks"]) <= cpus:
            best_k = len(c["disks"])
        else:
            break
    fastest = max(solo.values(), default=0.0)
    threads = 1 if fastest <= h1 * 0.8 else min(cpus, math.ceil(fastest / max(h1 * 0.8, 1)))

    if not samples:
        note = "No disk had files large enough to measure; keep single-disk mode."
    elif best_k == 1:
        note = ("Reading disks together did not add throughput (controller, bus or CPU limit), "
                "so one disk at a time is best.")
    else:
        note = (f"{best_k} disks read together kept at least {int(EFFICIENCY * 100)}% of their solo speed. "
                f"Parallel mode with {best_k} disks should scan about {best_k}x faster, "
                f"with {best_k} disks spinning at once.")

    return {
        "created_at": time.time(),
        "cpus": cpus,
        "hash_mbps_1_thread": mbps(h1),
        "hash_mbps_all_threads": mbps(hn),
        "solo": [{"disk": d, "mbps": mbps(v)} for d, v in solo.items()],
        "combos": combos,
        "skipped": skipped,
        "proposal": {
            "disk_mode": "parallel" if best_k > 1 else "single",
            "parallel_disks": max(best_k, 2),
            "hash_threads": threads,
        },
        "note": note,
    }


def disk_paths_for_benchmark(conn, disks) -> dict[str, list[str]]:
    """Sample from the selected roots; fall back to every listed disk."""
    out: dict[str, list[str]] = {}
    for r in conn.execute("SELECT disk, path FROM roots WHERE enabled = 1 ORDER BY path"):
        out.setdefault(r["disk"], []).append(r["path"])
    if not out:
        for d in disks:
            if d.kind in ("array", "pool", "folder"):
                out[d.name] = [str(d.path)]
    return dict(sorted(out.items(), key=lambda kv: natural_key(kv[0])))
