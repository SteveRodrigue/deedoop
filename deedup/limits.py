"""Resource limits for reading untrusted files (rule SC4, audit finding A5).

Backups hold files from anywhere, so an archive or a photo may be crafted to
exhaust memory, disk or time ("zip bombs"). deedup never extracts archives to
disk; these limits bound the work of reading them. An archive over a limit is
recorded as "too large to index safely" and the scan moves on.
"""

from __future__ import annotations

# Per archive (members of a nested archive count towards its parent).
MAX_ARCHIVE_BYTES = 2 * 1024**4          # 2 TiB of uncompressed data
MAX_MEMBERS = 1_000_000                  # files inside one archive
MAX_RATIO = 200                          # uncompressed bytes per byte on disk ...
RATIO_FLOOR = 10 * 1024**2               # ... once more than this much was read
# Nesting: an archive inside an archive is opened (Takeout does this); deeper ones are files.
MAX_DEPTH = 1


class ArchiveTooLarge(Exception):
    """An archive went over one of the limits above."""


class Budget:
    """Counts what one archive (and the archives inside it) decompress to."""

    def __init__(self, archive_bytes: int):
        self.archive_bytes = max(1, archive_bytes)
        self.bytes = 0
        self.members = 0

    def member(self) -> None:
        self.members += 1
        if self.members > MAX_MEMBERS:
            raise ArchiveTooLarge(f"too large to index safely: more than {MAX_MEMBERS:,} files inside")

    def read(self, n: int) -> None:
        self.bytes += n
        if self.bytes > MAX_ARCHIVE_BYTES:
            raise ArchiveTooLarge("too large to index safely: more than 2 TiB uncompressed")
        if self.bytes > RATIO_FLOOR and self.bytes > MAX_RATIO * self.archive_bytes:
            raise ArchiveTooLarge(f"too large to index safely: expands more than {MAX_RATIO} times "
                                  "(a zip bomb?)")
