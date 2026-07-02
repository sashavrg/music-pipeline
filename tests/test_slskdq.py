"""Tests for slskdq.py — the slskd in-flight ledger + admission gate (phase 6).

Two layers (mirrors test_reconcile.py):
  - Pure-function tests (always run): the generic-query guard, the poll
    state-machine classifier, slskd dir/state mapping — no DB, no network.
  - Ledger + gate tests against an ISOLATED pipeline.db (a tempdir; db.DB_PATH is
    redirected in setUp) with slskd + the library oracle mocked. These encode the
    invariants the whole gate rests on: the atomic in-flight claim, every refusal
    reason, the skip_in_library/skip_cooldown bypasses, and the claim/settle
    multi-source path used by fill-missing.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import db                # noqa: E402
from pipeline import slskdq as Q       # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Pure functions — no DB, no network
# ─────────────────────────────────────────────────────────────────────────────
class TestGenericQuery(unittest.TestCase):
    def test_empty_artist_is_generic(self):
        self.assertTrue(Q.is_generic_query('', 'Some Album'))

    def test_empty_album_is_generic(self):
        self.assertTrue(Q.is_generic_query('Some Artist', ''))

    def test_one_char_album_is_generic(self):
        self.assertTrue(Q.is_generic_query('Artist', 'X'))

    def test_real_album_is_not_generic(self):
        self.assertFalse(Q.is_generic_query('Boards of Canada', 'Geogaddi'))

    def test_artist_plus_greatest_hits_is_ok(self):
        # the non-empty artist makes this specific enough — must NOT be refused
        self.assertFalse(Q.is_generic_query('Queen', 'Greatest Hits'))


class TestDirMapping(unittest.TestCase):
    def test_dirkey_backslash_and_case(self):
        self.assertEqual(Q._dirkey(r'@@music\Boards of Canada\Geogaddi'), 'geogaddi')
        self.assertEqual(Q._dirkey('/x/y/Geogaddi/'), 'geogaddi')

    def test_dir_file_states_match(self):
        transfers = [{'username': 'u1', 'directories': [
            {'directory': r'shared\Geogaddi', 'files': [
                {'state': 'Completed, Succeeded'}, {'state': 'InProgress'}]}]}]
        self.assertEqual(Q._dir_file_states(transfers, 'u1', 'Geogaddi'),
                         ['Completed, Succeeded', 'InProgress'])

    def test_dir_file_states_wrong_user(self):
        transfers = [{'username': 'u1', 'directories': [
            {'directory': r'shared\Geogaddi', 'files': [{'state': 'X'}]}]}]
        self.assertEqual(Q._dir_file_states(transfers, 'u2', 'Geogaddi'), [])

    def test_dir_file_states_absent(self):
        self.assertEqual(Q._dir_file_states([], 'u1', 'Geogaddi'), [])


class TestClassifyRow(unittest.TestCase):
    """poll()'s per-row state machine."""
    def _row(self, state='queued', age_h=1.0, user='u', rdir='D'):
        return {'state': state, 'username': user, 'remote_dir': rdir,
                'queued_at': time.time() - age_h * 3600}

    def _tx(self, states, user='u', rdir='D'):
        return [{'username': user, 'directories': [
            {'directory': rdir, 'files': [{'state': s} for s in states]}]}]

    def test_all_succeeded_completes(self):
        new, _ = Q._classify_row(self._row(), self._tx(['Completed, Succeeded'] * 3), time.time())
        self.assertEqual(new, 'completed')

    def test_any_errored_fails(self):
        tx = self._tx(['Completed, Succeeded', 'Completed, Errored'])
        new, _ = Q._classify_row(self._row(), tx, time.time())
        self.assertEqual(new, 'failed')

    def test_cancelled_fails(self):
        new, _ = Q._classify_row(self._row(), self._tx(['Completed, Cancelled']), time.time())
        self.assertEqual(new, 'failed')

    def test_still_queued_stays_live(self):
        new, _ = Q._classify_row(self._row(), self._tx(['Queued, Remotely']), time.time())
        self.assertIsNone(new)

    def test_queued_with_active_promotes_to_downloading(self):
        new, _ = Q._classify_row(self._row('queued'), self._tx(['InProgress']), time.time())
        self.assertEqual(new, 'downloading')

    def test_absent_and_old_expires(self):
        old = self._row(age_h=Q.cfg.LEDGER_STALE_EXPIRE_H + 1)
        new, _ = Q._classify_row(old, [], time.time())
        self.assertEqual(new, 'expired')

    def test_absent_and_young_waits(self):
        young = self._row(age_h=1.0)
        new, _ = Q._classify_row(young, [], time.time())
        self.assertIsNone(new)


# ─────────────────────────────────────────────────────────────────────────────
# Ledger + gate against an isolated pipeline.db
# ─────────────────────────────────────────────────────────────────────────────
class _DBTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='slskdq_test_')
        self._orig = (db.DB_DIR, db.DB_PATH)
        db.DB_DIR = Path(self.tmp)
        db.DB_PATH = Path(self.tmp) / 'pipeline.db'
        db.init_db()

    def tearDown(self):
        db.DB_DIR, db.DB_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestLedgerCRUD(_DBTest):
    def test_atomic_inflight_claim(self):
        r1 = db.ledger_insert('ch:a', 'A', 'B', 'recover')
        r2 = db.ledger_insert('ch:a', 'A', 'B', 'quarantine')  # must be refused
        self.assertIsNotNone(r1)
        self.assertIsNone(r2)
        self.assertEqual(db.ledger_live('ch:a')['source'], 'recover')

    def test_terminal_frees_slot_and_stamps_terminal_at(self):
        r1 = db.ledger_insert('ch:a', 'A', 'B', 'recover')
        db.ledger_set_state(r1, 'failed', 'peer offline')
        st, ts = db.ledger_last_terminal('ch:a')
        self.assertEqual(st, 'failed')
        self.assertIsInstance(ts, float)
        # slot now free
        self.assertIsNotNone(db.ledger_insert('ch:a', 'A', 'B', 'fill'))

    def test_set_state_live_does_not_stamp_terminal(self):
        r1 = db.ledger_insert('ch:a', 'A', 'B', 'recover')
        db.ledger_set_state(r1, 'downloading')
        self.assertIsNone(db.ledger_last_terminal('ch:a'))

    def test_delete_rolls_back_claim(self):
        r1 = db.ledger_insert('ch:a', 'A', 'B', 'recover')
        self.assertTrue(db.ledger_delete(r1))
        self.assertIsNone(db.ledger_live('ch:a'))
        # no terminal row left -> re-claim is clean
        self.assertIsNotNone(db.ledger_insert('ch:a', 'A', 'B', 'recover'))

    def test_prune_removes_terminal_only(self):
        live = db.ledger_insert('ch:live', 'A', 'B', 'recover')
        dead = db.ledger_insert('ch:dead', 'C', 'D', 'recover')
        db.ledger_set_state(dead, 'completed')
        self.assertEqual(db.ledger_prune(0), 1)
        self.assertEqual(len(db.ledger_live_rows()), 1)
        self.assertEqual(db.ledger_live_rows()[0]['id'], live)


class TestGate(_DBTest):
    """enqueue()/claim() decisions with slskd + oracle mocked."""
    def setUp(self):
        super().setUp()
        # default: slskd idle, nothing in library
        self._p = mock.patch.object(Q, 'pending_download_count', return_value=0)
        self._lib = mock.patch.object(
            Q, 'resolve_in_library',
            side_effect=lambda a, al, db_path=Q.BEETS_DB: (Q.ledger_key(a, al), False))
        self._p.start()
        self._lib.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(self._lib.stop)

    def test_generic_refused(self):
        d = Q.enqueue('Artist', 'X', source='recover', execute=False)
        self.assertEqual(d.state, Q.REFUSED_GENERIC)

    def test_in_library_refused(self):
        with mock.patch.object(Q, 'resolve_in_library',
                               side_effect=lambda a, al, db_path=Q.BEETS_DB: (Q.ledger_key(a, al), True)):
            d = Q.enqueue('Real Artist', 'Real Album', source='recover', execute=False)
        self.assertEqual(d.state, Q.REFUSED_LIBRARY)

    def test_owned_dryrun_is_not_would_admit_regression(self):
        # regression: Decision.__bool__ == admitted, so `refusal or WOULD_ADMIT`
        # silently swallowed refusals. An owned album dry-run MUST refuse.
        with mock.patch.object(Q, 'resolve_in_library',
                               side_effect=lambda a, al, db_path=Q.BEETS_DB: (Q.ledger_key(a, al), True)):
            d = Q.enqueue('Real Artist', 'Real Album', source='recover', execute=False)
        self.assertNotEqual(d.state, Q.WOULD_ADMIT)
        self.assertFalse(d.admitted)

    def test_skip_in_library_bypasses_library(self):
        with mock.patch.object(Q, 'resolve_in_library',
                               side_effect=lambda a, al, db_path=Q.BEETS_DB: (Q.ledger_key(a, al), True)):
            d = Q.enqueue('Real Artist', 'Real Album', source='fill',
                          skip_in_library=True, execute=False)
        self.assertEqual(d.state, Q.WOULD_ADMIT)

    def test_capacity_refused(self):
        with mock.patch.object(Q, 'pending_download_count', return_value=999):
            d = Q.enqueue('Real Artist', 'Real Album', source='recover',
                          max_pending=100, execute=False)
        self.assertEqual(d.state, Q.REFUSED_CAPACITY)

    def test_cooling_after_completed(self):
        key = Q.ledger_key('Real Artist', 'Real Album')
        rid = db.ledger_insert(key, 'Real Artist', 'Real Album', 'recover')
        db.ledger_set_state(rid, 'completed')   # arms completed-settle cooldown
        d = Q.enqueue('Real Artist', 'Real Album', source='recover', execute=False)
        self.assertEqual(d.state, Q.REFUSED_COOLING)

    def test_skip_cooldown_bypasses_cooling(self):
        key = Q.ledger_key('Real Artist', 'Real Album')
        rid = db.ledger_insert(key, 'Real Artist', 'Real Album', 'recover')
        db.ledger_set_state(rid, 'completed')
        d = Q.enqueue('Real Artist', 'Real Album', source='quarantine',
                      skip_cooldown=True, execute=False)
        self.assertEqual(d.state, Q.WOULD_ADMIT)

    def test_in_flight_refused(self):
        key = Q.ledger_key('Real Artist', 'Real Album')
        db.ledger_insert(key, 'Real Artist', 'Real Album', 'recover')  # live row
        d = Q.enqueue('Real Artist', 'Real Album', source='quarantine', execute=False)
        self.assertEqual(d.state, Q.REFUSED_INFLIGHT)

    def test_clear_would_admit(self):
        d = Q.enqueue('Real Artist', 'Real Album', source='recover', execute=False)
        self.assertEqual(d.state, Q.WOULD_ADMIT)


class TestEnqueueExecute(_DBTest):
    def setUp(self):
        super().setUp()
        mock.patch.object(Q, 'pending_download_count', return_value=0).start()
        mock.patch.object(
            Q, 'resolve_in_library',
            side_effect=lambda a, al, db_path=Q.BEETS_DB: (Q.ledger_key(a, al), False)).start()
        self.addCleanup(mock.patch.stopall)

    def test_admitted_posts_once_and_leaves_live_row(self):
        calls = []
        d = Q.enqueue('Real Artist', 'Real Album', source='recover',
                      post=lambda: (calls.append(1) or True))
        self.assertTrue(d.admitted)
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(db.ledger_live(d.identity_key))  # still 'queued'

    def test_second_same_identity_refused_in_flight(self):
        Q.enqueue('Real Artist', 'Real Album', source='recover', post=lambda: True)
        d2 = Q.enqueue('Real Artist', 'Real Album', source='quarantine', post=lambda: True)
        self.assertEqual(d2.state, Q.REFUSED_INFLIGHT)

    def test_post_false_rolls_back(self):
        d = Q.enqueue('Real Artist', 'Real Album', source='recover', post=lambda: False)
        self.assertEqual(d.state, Q.POST_FAILED)
        self.assertIsNone(db.ledger_live(d.identity_key))     # no cooldown row left

    def test_post_raises_rolls_back(self):
        def boom():
            raise RuntimeError('network down')
        d = Q.enqueue('Real Artist', 'Real Album', source='recover', post=boom)
        self.assertEqual(d.state, Q.POST_FAILED)
        self.assertIsNone(db.ledger_live(d.identity_key))

    def test_enqueue_execute_requires_post(self):
        with self.assertRaises(ValueError):
            Q.enqueue('Real Artist', 'Real Album', source='recover', execute=True)


class TestClaimSettle(_DBTest):
    """The claim()/settle() path fill-missing uses (multi-source per album)."""
    def setUp(self):
        super().setUp()
        mock.patch.object(Q, 'pending_download_count', return_value=0).start()
        mock.patch.object(
            Q, 'resolve_in_library',
            side_effect=lambda a, al, db_path=Q.BEETS_DB: (Q.ledger_key(a, al), False)).start()
        self.addCleanup(mock.patch.stopall)

    def test_claim_then_settle_ok_keeps_live(self):
        c = Q.claim('A1', 'B1', source='fill', skip_in_library=True, skip_cooldown=True)
        self.assertTrue(c.admitted)
        Q.settle(c.rowid, True, 'queued 2 sources')
        self.assertIsNotNone(db.ledger_live(c.identity_key))

    def test_settle_fail_deletes(self):
        c = Q.claim('A1', 'B1', source='fill', skip_in_library=True, skip_cooldown=True)
        Q.settle(c.rowid, False)
        self.assertIsNone(db.ledger_live(c.identity_key))

    def test_two_distinct_albums_both_live(self):
        c1 = Q.claim('A1', 'B1', source='fill', skip_in_library=True)
        c2 = Q.claim('A2', 'B2', source='fill', skip_in_library=True)
        self.assertTrue(c1.admitted and c2.admitted)
        self.assertEqual(len(db.ledger_live_rows()), 2)


class TestBusyLocalDirs(_DBTest):
    """busy_local_dirs feeds reconcile-import's transfer-race shield: dirs with
    live transfers + live ledger rows are busy; API-unreachable is None (unknown
    ≠ empty — the caller must not sweep on None)."""

    @staticmethod
    def _payload():
        return [
            {'username': 'u1', 'directories': [
                {'directory': 'Music\\Journey\\Live In Houston 1981',
                 'files': [{'state': 'Completed, Succeeded'},
                           {'state': 'InProgress'},
                           {'state': 'Queued, Remotely'}]},
                {'directory': 'Music\\Done\\All Finished Album',
                 'files': [{'state': 'Completed, Succeeded'},
                           {'state': 'Completed, Errored'}]},
            ]},
            {'username': 'u2', 'directories': [
                {'directory': '@@x/share/Requested One',
                 'files': [{'state': 'Requested'}]},
            ]},
        ]

    def test_live_dirs_only_lowercased_basenames(self):
        with mock.patch.object(Q, '_api_get', return_value=self._payload()):
            busy = Q.busy_local_dirs()
        self.assertEqual(busy, {'live in houston 1981', 'requested one'})

    def test_live_ledger_rows_included(self):
        db.ledger_insert('ch:aa', 'A', 'B', 'wishlist', 'u3',
                         'peer\\stuff\\Ledger Only Dir', 5)
        with mock.patch.object(Q, '_api_get', return_value=[]):
            busy = Q.busy_local_dirs()
        self.assertEqual(busy, {'ledger only dir'})

    def test_api_unreachable_is_none_not_empty(self):
        with mock.patch.object(Q, '_api_get', return_value=None):
            self.assertIsNone(Q.busy_local_dirs())


if __name__ == '__main__':
    unittest.main()
