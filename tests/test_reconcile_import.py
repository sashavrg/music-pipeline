"""Tests for reconcile_import.py — the scheduled consumer side (download ->
library -> Plex, unattended).

Two layers:
  - _prune_empty_dirs (filesystem, no DB/network): the post-import cleanup that
    keeps the empty source dir left by beets move:yes from re-PARKing every run.
    Encodes: empty+settled is removed, anything with a file is kept, fresh dirs
    and _/.-prefixed dirs are left alone — so it can never lose audio.
  - main() orchestration (reconcile.main + Plex refresh + notification mocked):
    Plex is refreshed ONLY when something landed in the library (NEW/UPGRADE),
    a park never fails the unit, and a reconcile crash does.
"""
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import reconcile_import as RI   # noqa: E402


def _mkdir(root, name, *, files=(), age_min=0.0):
    d = Path(root) / name
    d.mkdir(parents=True, exist_ok=True)
    for fn in files:
        (d / fn).write_text("x", encoding="utf-8")
    if age_min:
        old = time.time() - age_min * 60
        os.utime(d, (old, old))
    return d


class TestPruneEmptyDirs(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="ri-prune-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        # logging to stdout (no log file) keeps the test hermetic
        RI._log_fh = None

    def test_removes_empty_settled_dir(self):
        d = _mkdir(self.tmp, "Artist - Album", age_min=30)
        n = RI._prune_empty_dirs(Path(self.tmp), min_age_min=10)
        self.assertEqual(n, 1)
        self.assertFalse(d.exists())

    def test_keeps_dir_with_audio(self):
        d = _mkdir(self.tmp, "Has Music", files=("01.flac",), age_min=30)
        n = RI._prune_empty_dirs(Path(self.tmp), min_age_min=10)
        self.assertEqual(n, 0)
        self.assertTrue(d.exists())

    def test_keeps_fresh_empty_dir(self):
        # too young to be a post-import leftover — slskd may be mid-create
        d = _mkdir(self.tmp, "Just Created", age_min=1)
        n = RI._prune_empty_dirs(Path(self.tmp), min_age_min=10)
        self.assertEqual(n, 0)
        self.assertTrue(d.exists())

    def test_skips_underscore_and_dot_dirs(self):
        u = _mkdir(self.tmp, "_graveyard", age_min=30)
        h = _mkdir(self.tmp, ".hidden", age_min=30)
        n = RI._prune_empty_dirs(Path(self.tmp), min_age_min=10)
        self.assertEqual(n, 0)
        self.assertTrue(u.exists())
        self.assertTrue(h.exists())

    def test_empty_with_only_empty_subdirs_is_removed(self):
        d = _mkdir(self.tmp, "Nested", age_min=30)
        (d / "sub").mkdir()
        old = time.time() - 30 * 60
        os.utime(d, (old, old))
        n = RI._prune_empty_dirs(Path(self.tmp), min_age_min=10)
        self.assertEqual(n, 1)
        self.assertFalse(d.exists())

    def test_missing_inbox_is_noop(self):
        self.assertEqual(RI._prune_empty_dirs(Path(self.tmp) / "nope", 10), 0)


class TestMainOrchestration(unittest.TestCase):
    def setUp(self):
        RI._log_fh = None

    def _run(self, summary, rc=0, raises=None):
        with mock.patch.object(RI, "setup_logging"), \
             mock.patch.object(RI, "_prune_empty_dirs", return_value=0), \
             mock.patch.object(RI, "_read_summary", return_value=summary), \
             mock.patch.object(RI, "_plex_refresh", return_value=True) as plex, \
             mock.patch.object(RI.pipeline_db, "push_notification") as notify, \
             mock.patch.object(RI.reconcile, "main") as rmain:
            if raises:
                rmain.side_effect = raises
            else:
                rmain.return_value = rc
            ret = RI.main([])
        return ret, plex, notify, rmain

    def test_refresh_on_new(self):
        ret, plex, notify, _ = self._run({"NEW": 1, "UPGRADE": 0, "PARK": 0, "DUPLICATE": 0})
        self.assertEqual(ret, 0)
        plex.assert_called_once()
        notify.assert_called_once()

    def test_refresh_on_upgrade(self):
        _, plex, _, _ = self._run({"NEW": 0, "UPGRADE": 2, "PARK": 0, "DUPLICATE": 0})
        plex.assert_called_once()

    def test_no_refresh_when_nothing_changed(self):
        ret, plex, notify, _ = self._run({"NEW": 0, "UPGRADE": 0, "PARK": 0, "DUPLICATE": 3})
        self.assertEqual(ret, 0)
        plex.assert_not_called()
        # duplicates-only still notifies? no — only imports or parks are noteworthy
        notify.assert_not_called()

    def test_park_notifies_but_does_not_fail_or_refresh(self):
        ret, plex, notify, _ = self._run({"NEW": 0, "UPGRADE": 0, "PARK": 2, "DUPLICATE": 0}, rc=4)
        self.assertEqual(ret, 0)          # a park is the gate working, not a failure
        plex.assert_not_called()
        notify.assert_called_once()

    def test_reconcile_crash_fails_unit(self):
        ret, plex, _, _ = self._run({}, raises=RuntimeError("locked db"))
        self.assertEqual(ret, 1)
        plex.assert_not_called()

    def test_empty_inbox_is_silent(self):
        ret, plex, notify, _ = self._run({"NEW": 0, "UPGRADE": 0, "PARK": 0, "DUPLICATE": 0})
        self.assertEqual(ret, 0)
        plex.assert_not_called()
        notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
