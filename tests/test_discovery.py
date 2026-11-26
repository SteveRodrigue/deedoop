import pytest

from deedoop import db
from deedoop.discovery import RootError, add_custom_root, list_disks, list_shares, sync_share_roots


def test_lists_disks_pools_and_unassigned(unraid):
    disks = {d.name: d.kind for d in list_disks(unraid.mnt)}
    assert disks == {"disk1": "array", "disk2": "array", "cache": "pool", "disks/usb": "unassigned"}
    assert [d.name for d in list_disks(unraid.mnt)][:2] == ["disk1", "disk2"]


def test_lists_shares_with_their_disks(unraid):
    shares = {s.name: s.disks for s in list_shares(unraid.mnt)}
    assert shares == {"appdata": ["disk1"], "Docs": ["disk2", "cache"], "Photos": ["disk1", "disk2"]}


def test_natural_disk_order(tmp_path):
    mnt = tmp_path / "mnt"
    for d in ("user", "disk10", "disk2", "disk1"):
        (mnt / d).mkdir(parents=True)
    assert [d.name for d in list_disks(mnt)] == ["disk1", "disk2", "disk10"]


def test_share_selection_creates_one_root_per_disk(unraid, conn):
    db.set_settings(conn, {"shares": ["Photos"]})
    sync_share_roots(conn, unraid.mnt)
    rows = conn.execute("SELECT path, disk, user_path, enabled FROM roots ORDER BY path").fetchall()
    assert [(r["disk"], r["user_path"], r["enabled"]) for r in rows] == [
        ("disk1", str(unraid.mnt / "user/Photos"), 1),
        ("disk2", str(unraid.mnt / "user/Photos"), 1),
    ]
    # A share that spreads onto a new disk is picked up on the next sync.
    (unraid.mnt / "cache/Photos").mkdir()
    sync_share_roots(conn, unraid.mnt)
    assert conn.execute("SELECT COUNT(*) FROM roots WHERE enabled = 1").fetchone()[0] == 3
    # Unticking disables (keeps the index), it does not delete.
    db.set_settings(conn, {"shares": []})
    sync_share_roots(conn, unraid.mnt)
    assert conn.execute("SELECT COUNT(*) FROM roots WHERE enabled = 1").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM roots").fetchone()[0] == 3


def test_custom_root_rules(unraid, conn):
    add_custom_root(conn, unraid.mnt, str(unraid.mnt / "disks/usb/Backup"))
    with pytest.raises(RootError):
        add_custom_root(conn, unraid.mnt, str(unraid.mnt / "disks/usb"))  # overlaps
    with pytest.raises(RootError):
        add_custom_root(conn, unraid.mnt, str(unraid.mnt / "user/Photos"))  # FUSE view
    with pytest.raises(RootError):
        add_custom_root(conn, unraid.mnt, "/etc")  # outside the mount
    with pytest.raises(RootError):
        add_custom_root(conn, unraid.mnt, "relative/path")
    row = conn.execute("SELECT disk, source FROM roots").fetchone()
    assert (row["disk"], row["source"]) == ("disks/usb", "custom")
