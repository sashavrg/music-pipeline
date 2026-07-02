"""Tests for parks.py (fail-to-review visibility) and the quarantine
dead-letter path (terminal refusals stop retrying forever and retire to
unparsed/ with a single notification)."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import parks as P                  # noqa: E402
from pipeline import quarantine_requeue as QR    # noqa: E402
from pipeline import db                          # noqa: E402


class TestCollectParks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='parks-'))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.plans = Path(tempfile.mkdtemp(prefix='plans-'))
        self.addCleanup(lambda: shutil.rmtree(self.plans, ignore_errors=True))
        self._orig = P.PLAN_ROOT
        P.PLAN_ROOT = self.plans
        self.addCleanup(lambda: setattr(P, 'PLAN_ROOT', self._orig))

    def _park(self, run_id, name, n_files=2, age_days=0.0, reason=None):
        d = self.tmp / run_id / name
        d.mkdir(parents=True)
        for i in range(n_files):
            (d / f'{i}.flac').write_text('x')
        if age_days:
            old = time.time() - age_days * 86400
            os.utime(d, (old, old))
        if reason is not None:
            pd = self.plans / run_id
            pd.mkdir(parents=True, exist_ok=True)
            (pd / 'plan.json').write_text(json.dumps({'entries': [
                {'candidate': {'path': f'/inbox/{name}'}, 'route_reason': reason}
            ]}))
        return d

    def test_collects_with_reason_oldest_first(self):
        self._park('run-a', 'Old Album', age_days=20, reason='quality-ambiguous')
        self._park('run-b', 'New Album', age_days=1)
        parks = P.collect_parks(self.tmp)
        self.assertEqual([p['name'] for p in parks], ['Old Album', 'New Album'])
        self.assertEqual(parks[0]['reason'], 'quality-ambiguous')
        self.assertEqual(parks[1]['reason'], '')   # no plan -> empty, not crash
        self.assertEqual(parks[0]['n_files'], 2)

    def test_missing_root_is_empty(self):
        self.assertEqual(P.collect_parks(self.tmp / 'nope'), [])


class TestDeadLetter(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='dl-'))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self._orig_root = QR.QUARANTINE_ROOT
        QR.QUARANTINE_ROOT = self.tmp
        self.addCleanup(lambda: setattr(QR, 'QUARANTINE_ROOT', self._orig_root))
        QR._log_fh = None

    def test_moves_to_unparsed_and_notifies_once(self):
        folder = self.tmp / 'incomplete' / 'Hopeless Case'
        folder.mkdir(parents=True)
        (folder / 'a.flac').write_text('x')
        with mock.patch.object(QR.pipeline_db, 'push_notification') as notify, \
             mock.patch.object(QR.pipeline_db, 'delete_quarantine_state') as dels:
            ok = QR.dead_letter(folder, 'incomplete/Hopeless Case', 6)
        self.assertTrue(ok)
        self.assertFalse(folder.exists())
        self.assertTrue((self.tmp / 'unparsed' / 'Hopeless Case' / 'a.flac').exists())
        notify.assert_called_once()
        self.assertEqual(notify.call_args[0][0], 'quarantine_dead_letter')
        dels.assert_called_once()

    def test_unparsed_is_never_rescanned(self):
        # the retirement destination must be invisible to collect_folders
        (self.tmp / 'unparsed' / 'Dead Album').mkdir(parents=True)
        (self.tmp / 'incomplete' / 'Live Album').mkdir(parents=True)
        keys = {k for _, k in QR.collect_folders()}
        self.assertEqual(keys, {'incomplete/Live Album'})

    def test_move_failure_returns_false(self):
        missing = self.tmp / 'incomplete' / 'Ghost'
        with mock.patch.object(QR.pipeline_db, 'push_notification') as notify:
            self.assertFalse(QR.dead_letter(missing, 'incomplete/Ghost', 6))
        notify.assert_not_called()


class TestFruitlessState(unittest.TestCase):
    """quarantine_state.fruitless round-trips through the migration + CRUD."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='dbfruit-')
        self._orig = (db.DB_DIR, db.DB_PATH)
        db.DB_DIR = Path(self.tmp)
        db.DB_PATH = Path(self.tmp) / 'pipeline.db'
        db.init_db()
        self.addCleanup(self._restore)

    def _restore(self):
        db.DB_DIR, db.DB_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roundtrip_and_reset(self):
        db.upsert_quarantine_state('k', 1.0, 0, fruitless=3)
        self.assertEqual(db.get_quarantine_state()['k']['fruitless'], 3)
        db.upsert_quarantine_state('k', 2.0, 0)          # default resets
        self.assertEqual(db.get_quarantine_state()['k']['fruitless'], 0)


if __name__ == '__main__':
    unittest.main()
