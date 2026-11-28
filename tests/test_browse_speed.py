"""Browse on a big index: the page answers at once and the rest comes a page
at a time (found live: switching to Browse took seconds on ~2M files)."""

import re

from deedoop import db, queries
from tests.test_web import make_client


def _root(c, disk: str, share: str = "Photos") -> int:
    return c.execute("INSERT INTO roots(path, disk, share, user_path, created_at) VALUES (?, ?, ?, ?, 0)",
                     (f"/mnt/{disk}/{share}", disk, share, f"/mnt/user/{share}")).lastrowid


def _file(c, root: int, d: str, name: str, ext: str = "jpg", state: str = "present") -> int:
    return c.execute("INSERT INTO files(root_id, dir, name, size, mtime_ns, dev, inode, nlink, ext, last_seen_run, "
                     "state) VALUES (?, ?, ?, 1, 0, 1, abs(random()), 1, ?, 1, ?)",
                     (root, d, name, ext, state)).lastrowid


def _all_pages(c, flt):
    seen, after, skip = [], None, 0
    for _ in range(100):
        res = queries.browse(c, flt, after, skip)
        seen += [(f["dir"], f["name"], f["disk"]) for f in res["files"]]
        nxt = res["next"]
        if not nxt:
            return seen
        after = (nxt["after_dir"], nxt["after_name"], nxt["after_root"]) if "after_dir" in nxt else None
        skip = nxt.get("skip", 0)
    raise AssertionError("never reached the last page")


def test_pages_follow_on_across_disks_without_repeats(conn):
    """A share spread over disks, the same path on two of them, pages of 3:
    every file once, in folder and name order, like one big sorted list."""
    r1, r2, r3 = _root(conn, "disk1"), _root(conn, "disk2"), _root(conn, "disk3")
    want = []
    for i in range(10):
        for r, disk in ((r1, "disk1"), (r2, "disk2"), (r3, "disk3")):
            if (i + r) % 2 or i == 4:   # some on one disk only, 2014/n4 on all three
                _file(conn, r, "2014", f"n{i}")
                want.append(("2014", f"n{i}", disk))
    _file(conn, r1, "2014", "gone", state="gone")
    _file(conn, r1, "2014 b", "sibling")           # a sibling folder, not inside 2014
    _file(conn, r2, "2014/sub", "deeper")
    want.sort()
    folder = queries.BrowseFilter(loc="share:Photos", dir="2014", per_page=3)
    assert _all_pages(conn, folder) == want
    search = queries.BrowseFilter(loc="share:Photos", dir="2014", kind="image", per_page=3)
    assert _all_pages(conn, search) == sorted(want + [("2014/sub", "deeper", "disk2")])
    assert queries.browse_count(conn, folder) == (len(want), False)
    assert queries.browse_count(conn, search) == (len(want) + 1, False)
    assert queries.browse_count(conn, search, cap=5) == (5, True)


def test_folder_pages_read_the_folder_not_the_whole_disk(conn):
    """files_root_state (root, state, container, size) looks like the better
    index to SQLite, but walks every file of the disk: 0.7 s a query on 2M files."""
    r = _root(conn, "disk1")
    for i in range(50):
        _file(conn, r, f"d{i % 5}", f"n{i}")
    sqls = []
    conn.set_trace_callback(sqls.append)
    queries.browse(conn, queries.BrowseFilter(loc="share:Photos", dir="d1"))
    queries.browse(conn, queries.BrowseFilter(loc="share:Photos", dir="d1", kind="image"))
    queries.browse_count(conn, queries.BrowseFilter(loc="share:Photos", dir="d1"))
    queries.browse_count(conn, queries.BrowseFilter(loc="share:Photos", dir="d1", q="n"))
    conn.set_trace_callback(None)
    listings = [s for s in sqls if "FROM files f" in s and "f.dir" in s]
    assert len(listings) >= 4
    for sql in listings:
        plan = " ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql))
        assert "files_root_state" not in plan and "SCAN f" not in plan, plan


def test_music_lists_each_artist_once(conn):
    """GROUP BY name meant files.name: one "artist" per song (43 MB page on 200k songs)."""
    r = _root(conn, "disk1", "Music")
    for i in range(6):
        fid = _file(conn, r, "Lanterns", f"{i}.flac", "flac")
        conn.execute("INSERT INTO av(file_id, kind, artist, album, probed_at) VALUES (?, 'audio', ?, 'A', 0)",
                     (fid, "Lanterns" if i < 4 else "Tidewater"))
    m = queries.music(conn, queries.BrowseFilter(loc="share:Music"))
    assert [(a["name"], a["tracks"]) for a in m["artists"]] == [("Lanterns", 4), ("Tidewater", 2)]


def test_filter_lists_are_kept_until_a_scan_starts_or_ends(conn):
    r = _root(conn, "disk1")
    conn.execute("INSERT INTO media(file_id, camera, extracted_at) VALUES (?, 'Pixel 7', 0)", (_file(conn, r, "", "a"),))
    assert queries.facets(conn)["cameras"] == ["Pixel 7"]
    conn.execute("INSERT INTO media(file_id, camera, extracted_at) VALUES (?, 'EOS R6', 0)", (_file(conn, r, "", "b"),))
    assert queries.facets(conn)["cameras"] == ["Pixel 7"]          # kept
    db.enqueue_job(conn, "scan")
    assert sorted(queries.facets(conn)["cameras"]) == ["EOS R6", "Pixel 7"]


def test_browse_shows_the_page_first_and_fetches_the_rest(unraid):
    c = db.connect(unraid.db_path)
    r = _root(c, "disk1")
    for i in range(130):
        fid = _file(c, r, "2019", f"IMG_{i:04}.jpg")
        c.execute("INSERT INTO media(file_id, camera, extracted_at) VALUES (?, 'Pixel 7', 0)", (fid,))
    c.close()
    with make_client(unraid) as client:
        page = client.get("/browse?loc=share:Photos&dir=2019").text
        assert "IMG_0000.jpg" in page and "IMG_0099.jpg" in page and "IMG_0100.jpg" not in page
        assert "130 files in this folder" in page
        assert 'hx-get="/browse/facets?' in page and ">Pixel 7</option>" not in page   # filter lists come after
        more = re.search(r'hx-get="(/browse/more\?[^"]+)"', page).group(1).replace("&amp;", "&")
        rest = client.get(more).text
        assert "IMG_0100.jpg" in rest and "IMG_0129.jpg" in rest and "IMG_0099.jpg" not in rest
        assert "/browse/more" not in rest                                    # the last page
        # without JavaScript the same link opens the next page on its own
        alone = client.get(more.replace("/browse/more", "/browse")).text
        assert "IMG_0100.jpg" in alone and "Back to the first files" in alone
        assert ">Pixel 7</option>" in client.get("/browse/facets?loc=share:Photos").text
        search = client.get("/browse?loc=share:Photos&dir=2019&q=IMG").text
        assert 'hx-get="/browse/count?' in search
        assert "130 files match" in client.get("/browse/count?loc=share:Photos&dir=2019&q=IMG").text
        assert client.get("/browse/more?after_dir=" + "x" * 5000).status_code == 422   # SC7: bounded
