"""Regression coverage for the 2026-10-05 bot audit. All APIs are mocked."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_ALLOWED_CHAT_ID', '0')
from pipeline import telegram_bot as bot, recover, slskdq as q, db


class BotGateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch.object(db, 'DB_PATH', Path(tmp.name) / 'pipeline.db')
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(db, 'DB_DIR', Path(tmp.name))
        p.start()
        self.addCleanup(p.stop)
        db.init_db()
        self.folder = recover.FolderResult('peer', r'music\Album',
            [{'filename': r'music\Album\01.flac', 'size': 1}], 'flac', 900, 5000000, 1)
        patches = [
            patch.object(recover, 'pending_download_count', return_value=0),
            patch.object(recover, 'count_existing_tracks', return_value=0),
            patch.object(recover, 'slskd_search', return_value=[{}]),
            patch.object(recover, 'find_all_folders', return_value=[self.folder]),
            patch.object(recover, 'expand_folder', side_effect=lambda f: f),
            patch.object(q, 'pending_download_count', return_value=0),
            patch.object(q, 'resolve_in_library',
                         side_effect=lambda a, al, path: (q.ledger_key(a, al), False)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_duplicate_request_posts_once_and_registers_ledger(self):
        with patch.object(recover, 'queue_download', return_value=True) as post:
            self.assertTrue(bot.process_query(recover, 'Artist', 'Album')[0])
            ok, msg = bot.process_query(recover, 'Artist', 'Album')
        self.assertFalse(ok)
        self.assertIn('in flight', msg)
        self.assertEqual(post.call_count, 1)
        row = db.ledger_live(q.ledger_key('Artist', 'Album'))
        self.assertEqual(row['source'], 'telegram')
        self.assertEqual(row['remote_dir'], self.folder.directory)

    def test_post_failure_can_fall_back_to_another_peer(self):
        from dataclasses import replace
        other = replace(self.folder, username='other')
        with patch.object(recover, 'find_all_folders', return_value=[self.folder, other]), \
             patch.object(recover, 'queue_download', side_effect=[False, True]) as post:
            ok, msg = bot.process_query(recover, 'Artist', 'Album')
        self.assertTrue(ok)
        self.assertIn('other', msg)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(db.ledger_live(q.ledger_key('Artist', 'Album'))['username'], 'other')

    def test_unknown_capacity_refuses_without_post(self):
        with patch.object(q, 'pending_download_count', return_value=None), \
             patch.object(recover, 'queue_download') as post:
            ok, msg = bot.process_query(recover, 'Artist', 'Album')
        self.assertFalse(ok)
        self.assertIn('unavailable', msg)
        post.assert_not_called()

    def test_audiobook_and_partial_music_keep_library_bypass(self):
        for profile, n_existing in ((recover.AUDIOBOOK, 0), (recover.MUSIC, 1)):
            from dataclasses import replace
            with patch.object(recover, 'count_existing_tracks', return_value=n_existing), \
                 patch.object(recover, 'expand_folder', return_value=replace(self.folder, file_count=2)), \
                 patch.object(q, 'enqueue', return_value=q.Decision(True, q.ADMITTED, 'test', 0, 'ok')) as gate:
                self.assertTrue(bot.process_query(recover, 'Artist', 'Album', profile)[0])
            self.assertTrue(gate.call_args.kwargs['skip_in_library'])


class TransferSafetyTests(unittest.TestCase):
    def test_unknown_capacity_is_not_zero(self):
        with patch.object(q, '_api_get', return_value=None):
            self.assertIsNone(q.pending_download_count())

    def test_legacy_capacity_helper_fails_closed(self):
        with patch.object(recover, 'api_get', side_effect=OSError('offline')):
            self.assertGreaterEqual(recover.pending_download_count(), recover.MAX_PENDING_DL)

    def test_outage_does_not_expire_or_mutate_live_rows(self):
        row = {'id': 1, 'state': 'queued', 'username': 'peer', 'remote_dir': 'Album',
               'queued_at': time.time() - (q.cfg.LEDGER_STALE_EXPIRE_H + 1) * 3600,
               'identity_key': 'audit'}
        with patch.object(q.db, 'init_db'), \
             patch.object(q.db, 'ledger_live_rows', return_value=[row]), \
             patch.object(q.db, 'ledger_set_state') as update, \
             patch.object(q, '_api_get', return_value=None):
            self.assertEqual(q.poll(), [])
        update.assert_not_called()

    def test_same_basename_uses_full_remote_directory(self):
        transfers = [{'username': 'peer', 'directories': [
            {'directory': r'Artist A\Greatest Hits',
             'files': [{'state': 'Completed, Succeeded'}]},
            {'directory': r'Artist B\Greatest Hits',
             'files': [{'state': 'InProgress'}]},
        ]}]
        row = {'state': 'queued', 'username': 'peer',
               'remote_dir': 'Artist B/Greatest Hits/', 'queued_at': time.time()}
        self.assertEqual(q._classify_row(row, transfers, time.time())[0], 'downloading')
        self.assertEqual(q._dir_file_states(transfers, 'peer', ''), [])

    def test_empty_successful_transfer_list_can_expire_old_row(self):
        row = {'id': 1, 'state': 'queued', 'username': 'peer', 'remote_dir': 'Album',
               'queued_at': time.time() - (q.cfg.LEDGER_STALE_EXPIRE_H + 1) * 3600,
               'identity_key': 'audit'}
        with patch.object(q.db, 'init_db'), \
             patch.object(q.db, 'ledger_live_rows', return_value=[row]), \
             patch.object(q, '_api_get', return_value=[]):
            self.assertEqual(q.poll(execute=False)[0][-1], 'expired')


class NotificationSafetyTests(unittest.TestCase):
    def test_all_recipients_attempted_despite_first_failure(self):
        with patch.object(bot, 'ALLOWED_CHAT_IDS', {'1', '2'}), \
             patch.object(bot, 'send_message', side_effect=[False, True]) as send:
            self.assertFalse(bot.send_notification('notification'))
        self.assertEqual([c.args[0] for c in send.call_args_list], ['1', '2'])

    def test_all_success_reports_deliverable(self):
        with patch.object(bot, 'ALLOWED_CHAT_IDS', {'1', '2'}), \
             patch.object(bot, 'send_message', return_value=True):
            self.assertTrue(bot.send_notification('notification'))
