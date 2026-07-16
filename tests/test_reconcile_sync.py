"""Tests for reconcile_sync.py — the post-Plex-cleanup DB↔disk consolidator.

Hermetic: a temp beets-shaped sqlite DB + real files on disk. The dedup path's
live-DB verifier (reconcile.resolve_dup_plan) is already covered by
test_reconcile.py, so here we mock reconcile.build_dup_plan to assert the
SYNTHESIZED plan (family grouping + keep selection) this module produces.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import reconcile_sync as S   # noqa: E402
from pipeline import identity as I         # noqa: E402


def _mkdb(path, albums, items):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, albumartist TEXT, album TEXT, "
                 "year INTEGER, mb_albumid TEXT, mb_releasegroupid TEXT)")
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, disc INTEGER, "
                 "track INTEGER, title TEXT, path BLOB)")
    conn.executemany("INSERT INTO albums VALUES (?,?,?,?,?,?)", albums)
    conn.executemany("INSERT INTO items VALUES (?,?,?,?,?,?)", items)
    conn.commit(); conn.close()


class TestPrunePlan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.db = str(self.base / 'library.db')

        def f(name, exists=True):
            p = self.base / name
            p.parent.mkdir(parents=True, exist_ok=True)
            if exists:
                p.write_bytes(b'x')
            return os.fsencode(str(p))

        # album 1: intact (both files present) -> omitted
        # album 2: both files gone           -> PRUNE_ALBUM
        # album 3: one of two gone            -> PRUNE_ITEMS (item 32)
        albums = [(1, 'A', 'Intact', 2020, '', ''),
                  (2, 'B', 'Gone', 2020, '', ''),
                  (3, 'C', 'Partial', 2020, '', '')]
        items = [
            (11, 1, 1, 1, 't', f('A/Intact/1.flac', True)),
            (12, 1, 1, 2, 't', f('A/Intact/2.flac', True)),
            (21, 2, 1, 1, 't', f('B/Gone/1.flac', False)),
            (22, 2, 1, 2, 't', f('B/Gone/2.flac', False)),
            (31, 3, 1, 1, 't', f('C/Partial/1.flac', True)),
            (32, 3, 1, 2, 't', f('C/Partial/2.flac', False)),
        ]
        _mkdb(self.db, albums, items)
        self.albums = I.load_albums(self.db)

    def tearDown(self):
        self._tmp.cleanup()

    def test_routes(self):
        plan = S.build_prune_plan(self.albums, self.db)
        by_id = {e['album_id']: e for e in plan}
        self.assertNotIn(1, by_id)                                # intact omitted
        self.assertEqual(by_id[2]['route'], 'PRUNE_ALBUM')        # all gone
        self.assertEqual(by_id[2]['remove_cmd'], ['beet', 'remove', '-a', '-f', 'id:2'])
        self.assertEqual(by_id[3]['route'], 'PRUNE_ITEMS')        # partial
        self.assertEqual(by_id[3]['item_ids'], [32])             # only the missing one
        self.assertEqual(by_id[3]['n_missing'], 1)
        self.assertEqual(by_id[3]['n_total'], 2)


class TestPathResolution(unittest.TestCase):
    """beets stores paths RELATIVE to `directory` and the pipeline never chdirs,
    so resolution against LIBRARY_ROOT is what stands between a real cull and
    'every file looks missing'."""

    def test_abs_joins_relative_and_passes_absolute(self):
        save = S.LIBRARY_ROOT
        S.LIBRARY_ROOT = Path('/mnt/lib/music')
        try:
            self.assertEqual(S._abs(b'Artist/Album/1.flac'), b'/mnt/lib/music/Artist/Album/1.flac')
            self.assertEqual(S._abs(b'/already/abs/2.flac'), b'/already/abs/2.flac')
        finally:
            S.LIBRARY_ROOT = save

    def test_relative_paths_resolve_for_existence(self):
        tmp = tempfile.TemporaryDirectory()
        base = Path(tmp.name)
        root = base / 'music'
        (root / 'X' / 'Alb').mkdir(parents=True)
        (root / 'X' / 'Alb' / '1.flac').write_bytes(b'x')   # present
        db = str(base / 'library.db')
        # stored RELATIVE to root: one present, one absent
        _mkdb(db, [(1, 'X', 'Alb', 2020, '', '')],
              [(11, 1, 1, 1, 't', b'X/Alb/1.flac'), (12, 1, 1, 2, 't', b'X/Alb/gone.flac')])
        save = S.LIBRARY_ROOT
        S.LIBRARY_ROOT = root
        try:
            albums = I.load_albums(db)
            self.assertEqual(S.probe_readable(albums, db), 1)          # the present file resolves
            plan = S.build_prune_plan(albums, db)
            self.assertEqual(plan[0]['route'], 'PRUNE_ITEMS')          # only gone.flac missing
            self.assertEqual(plan[0]['item_ids'], [12])
        finally:
            S.LIBRARY_ROOT = save
            tmp.cleanup()


class TestMassCeiling(unittest.TestCase):
    def test_ceiling(self):
        self.assertFalse(S.over_mass_ceiling(0, 100))
        self.assertFalse(S.over_mass_ceiling(30, 100))            # exactly at 30% not over
        self.assertTrue(S.over_mass_ceiling(31, 100))            # above ceiling
        self.assertFalse(S.over_mass_ceiling(0, 0))              # empty lib -> no divide-by-zero


class TestMountGuard(unittest.TestCase):
    def test_missing_root_aborts(self):
        save = (S.LIBRARY_ROOT, S.RECON_DURABLE)
        S.LIBRARY_ROOT = Path('/nonexistent/stale/mount/xyz')
        S.RECON_DURABLE = Path('/nonexistent/stale/mount/xyz/_reconcile')
        try:
            with self.assertRaises(SystemExit):
                S.assert_mount_healthy()
        finally:
            S.LIBRARY_ROOT, S.RECON_DURABLE = save


class TestDedupFamilySelection(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.db = str(self.base / 'library.db')
        rg = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'   # shared release-group -> same tier-1 key
        # album 10: 2 tracks; album 20: 1 track — SAME release-group => one family
        albums = [(10, 'Artist', 'Rec', 2020, '', rg),
                  (20, 'Artist', 'Rec', 2020, '', rg)]
        items = [
            (101, 10, 1, 1, 'a', os.fsencode(str(self.base / '10/1.flac'))),
            (102, 10, 1, 2, 'b', os.fsencode(str(self.base / '10/2.flac'))),
            (201, 20, 1, 1, 'a', os.fsencode(str(self.base / '20/1.flac'))),
        ]
        _mkdb(self.db, albums, items)
        self.albums = I.load_albums(self.db)

    def tearDown(self):
        self._tmp.cleanup()

    def test_keep_is_most_complete(self):
        captured = {}

        def fake_build_dup_plan(path, db_path):
            with open(path) as fh:
                import json
                captured['plan'] = json.load(fh)
            return []   # verifier output not under test here

        real = S.R.build_dup_plan
        S.R.build_dup_plan = fake_build_dup_plan
        try:
            classified, families = S.build_dedup_plan(self.albums, set(), self.db)
        finally:
            S.R.build_dup_plan = real

        self.assertEqual(len(captured['plan']), 1)               # exactly one dup family
        entry = captured['plan'][0]
        self.assertEqual(entry['keep']['album_id'], 10)          # 2 tracks beats 1
        self.assertEqual([d['album_id'] for d in entry['drop']], [20])

    def test_pruned_ids_excluded_from_families(self):
        # if album 20 was already pruned this run, no family remains
        captured = {}
        S.R.build_dup_plan = lambda p, d: captured.setdefault('n', 1) or []
        try:
            classified, families = S.build_dedup_plan(self.albums, {20}, self.db)
        finally:
            S.R.build_dup_plan = R_ORIG
        self.assertEqual(families, {})


R_ORIG = S.R.build_dup_plan


if __name__ == '__main__':
    unittest.main()
