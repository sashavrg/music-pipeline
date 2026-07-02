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

    def _run(self, summary, rc=0, raises=None, busy=frozenset()):
        with mock.patch.object(RI, "setup_logging"), \
             mock.patch.object(RI, "_ledger_poll", return_value=0) as poll, \
             mock.patch.object(RI, "_busy_dirs", return_value=busy), \
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


class TestBusyShield(unittest.TestCase):
    """The transfer-race guard: a folder slskd is still downloading into must
    never be swept (2026-07-02: one album was sliced into 4 park fragments
    because mtime-settling can't see slskd's between-file quiescence). Busy
    dirs are passed to reconcile as --skip-dir; an unknown busy set (API down)
    skips the whole sweep — unknown is not empty."""

    def setUp(self):
        RI._log_fh = None

    def _main(self, busy):
        with mock.patch.object(RI, "setup_logging"), \
             mock.patch.object(RI, "_ledger_poll", return_value=0), \
             mock.patch.object(RI, "_busy_dirs", return_value=busy), \
             mock.patch.object(RI, "_prune_empty_dirs", return_value=0) as prune, \
             mock.patch.object(RI, "_read_summary", return_value={}), \
             mock.patch.object(RI, "_plex_refresh"), \
             mock.patch.object(RI.pipeline_db, "push_notification"), \
             mock.patch.object(RI.reconcile, "main", return_value=0) as rmain:
            ret = RI.main([])
        return ret, rmain, prune

    def test_busy_dirs_become_skip_dir_args(self):
        ret, rmain, prune = self._main({"journey live", "cool kids"})
        self.assertEqual(ret, 0)
        argv = rmain.call_args[0][0]
        self.assertEqual(argv.count("--skip-dir"), 2)
        self.assertIn("journey live", argv)
        self.assertIn("cool kids", argv)
        # prune also receives the shield
        self.assertEqual(prune.call_args[0][2], {"journey live", "cool kids"})

    def test_unknown_busy_set_skips_sweep_without_failing(self):
        ret, rmain, prune = self._main(None)
        self.assertEqual(ret, 0)          # skipped, not failed — timer retries
        rmain.assert_not_called()
        prune.assert_not_called()

    def test_empty_busy_set_sweeps_normally(self):
        ret, rmain, _ = self._main(set())
        self.assertEqual(ret, 0)
        rmain.assert_called_once()
        self.assertNotIn("--skip-dir", rmain.call_args[0][0])

    def test_busy_dirs_helper_none_on_api_error(self):
        with mock.patch("pipeline.slskdq.busy_local_dirs",
                        side_effect=ConnectionError("slskd down")):
            self.assertIsNone(RI._busy_dirs())

    def test_prune_skips_busy_dir(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="ri-busy-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        active = _mkdir(tmp, "Active Album", age_min=30)     # empty AND settled
        stale = _mkdir(tmp, "Done Album", age_min=30)
        n = RI._prune_empty_dirs(Path(tmp), min_age_min=10, busy={"active album"})
        self.assertEqual(n, 1)
        self.assertTrue(active.exists())   # shielded despite being empty+old
        self.assertFalse(stale.exists())


class TestLedgerPoll(unittest.TestCase):
    """The scheduled ledger poll: rows settle on their own each run, and a
    slskd/API failure degrades to a warning — it must never fail the unit
    (in-library-at-admit is authoritative, a missed poll can't dup)."""

    def setUp(self):
        RI._log_fh = None

    def test_main_polls_ledger_every_run(self):
        with mock.patch.object(RI, "setup_logging"), \
             mock.patch.object(RI, "_ledger_poll", return_value=0) as poll, \
             mock.patch.object(RI, "_prune_empty_dirs", return_value=0), \
             mock.patch.object(RI, "_read_summary", return_value={}), \
             mock.patch.object(RI, "_plex_refresh"), \
             mock.patch.object(RI.pipeline_db, "push_notification"), \
             mock.patch.object(RI.reconcile, "main", return_value=0):
            self.assertEqual(RI.main([]), 0)
        poll.assert_called_once()

    def test_poll_transitions_are_counted(self):
        changes = [(1, "ch:abc", "queued", "failed"),
                   (2, "mbrg:def", "downloading", "completed")]
        with mock.patch("pipeline.slskdq.poll", return_value=changes) as p:
            self.assertEqual(RI._ledger_poll(), 2)
        p.assert_called_once_with(execute=True)

    def test_poll_failure_is_nonfatal(self):
        with mock.patch("pipeline.slskdq.poll",
                        side_effect=ConnectionError("slskd down")):
            self.assertEqual(RI._ledger_poll(), 0)


if __name__ == "__main__":
    unittest.main()
