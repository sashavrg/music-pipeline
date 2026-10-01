"""Tests for whole-folder queueing and park-aware import notifications.

2026-10-01: '梶浦由記 - Madoka Magica Ultimate Best' matched only the 4 of 18
tracks whose filenames carry Kajiura's name. The bot queued exactly those search
hits, reconcile parked the fragment, and the Telegram notification still opened
with "✅ Import complete". Encodes:
  - expand_folder swaps search hits for the peer's full directory listing,
    same format only, and falls back to the original on any browse failure
  - the bot queues the expanded folder
  - a park-only run never claims success and names what was parked and why
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_ALLOWED_CHAT_ID', '0')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import recover                    # noqa: E402
from pipeline import reconcile_import as RI     # noqa: E402
from pipeline import telegram_bot as bot        # noqa: E402

DIR = r"music\Puella Magi Madoka Magica Ultimate Best"


def _folder(names, fmt="flac"):
    files = [{"filename": f"{DIR}\\{n}", "size": 1} for n in names]
    return recover.FolderResult(username="peer", directory=DIR, files=files,
                                fmt=fmt, score=900, upload_speed=5_000_000,
                                file_count=len(files))


def _listing(names, name=DIR):
    return [{"name": name, "fileCount": len(names),
             "files": [{"filename": n, "size": 2} for n in names]}]


FULL = [f"{i:02d} - track.flac" for i in range(1, 19)]


class TestExpandFolder(unittest.TestCase):
    def test_expands_to_full_directory(self):
        f = _folder(FULL[:4])
        with mock.patch.object(recover, "api_post", return_value=_listing(FULL)) as post:
            out = recover.expand_folder(f)
        self.assertEqual(out.file_count, 18)
        self.assertEqual(len(out.files), 18)
        self.assertTrue(all(x["filename"].startswith(DIR + "\\") for x in out.files))
        self.assertIn("/api/v0/users/peer/directory", post.call_args[0][0])
        self.assertEqual(post.call_args[0][1], {"directory": DIR})
        # original untouched (dataclass replace, not mutation)
        self.assertEqual(f.file_count, 4)

    def test_only_same_format_audio(self):
        names = FULL + ["01 - track.mp3", "cover.jpg", "rip.log", "rip.cue"]
        with mock.patch.object(recover, "api_post", return_value=_listing(names)):
            out = recover.expand_folder(_folder(FULL[:4]))
        self.assertEqual(out.file_count, 18)
        self.assertTrue(all(x["filename"].endswith(".flac") for x in out.files))

    def test_ignores_other_directories(self):
        listing = _listing(FULL, name=DIR + r"\Scans")
        with mock.patch.object(recover, "api_post", return_value=listing):
            f = _folder(FULL[:4])
            self.assertIs(recover.expand_folder(f), f)

    def test_browse_failure_falls_back(self):
        f = _folder(FULL[:4])
        with mock.patch.object(recover, "api_post", side_effect=RuntimeError("offline")):
            self.assertIs(recover.expand_folder(f), f)
        with mock.patch.object(recover, "api_post", return_value=None):
            self.assertIs(recover.expand_folder(f), f)

    def test_never_shrinks(self):
        f = _folder(FULL[:4])
        with mock.patch.object(recover, "api_post", return_value=_listing(FULL[:2])):
            self.assertIs(recover.expand_folder(f), f)


class TestBotQueuesExpanded(unittest.TestCase):
    def test_process_query_queues_full_folder(self):
        cand = _folder(FULL[:4])
        queued = []
        with mock.patch.object(recover, "pending_download_count", return_value=0), \
             mock.patch.object(recover, "count_existing_tracks", return_value=0), \
             mock.patch.object(recover, "slskd_search", return_value=[{"x": 1}]), \
             mock.patch.object(recover, "find_all_folders", return_value=[cand]), \
             mock.patch.object(recover, "api_post", return_value=_listing(FULL)), \
             mock.patch.object(recover, "queue_download",
                               side_effect=lambda f: queued.append(f) or True):
            ok, msg = bot.process_query(recover, "梶浦由記", "Madoka Magica Ultimate Best")
        self.assertTrue(ok)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].file_count, 18)
        self.assertIn("Files: 18", msg)


class TestParkNotification(unittest.TestCase):
    PARKED = [{"folder": "Madoka Ultimate Best", "tracks": 4,
               "reason": "orphan-fragment (incomplete; acquire full release)"}]

    def test_park_only_run_does_not_claim_success(self):
        _, text = bot.render_notification({
            "event": "reconcile_import", "new": 0, "upgrade": 0, "park": 1,
            "albums": [], "parked": self.PARKED})
        self.assertNotIn("Import complete", text)
        self.assertIn("Nothing imported", text)
        self.assertIn("Madoka Ultimate Best · 4 tracks", text)
        self.assertIn("orphan-fragment", text)

    def test_mixed_run_still_lists_parks(self):
        _, text = bot.render_notification({
            "event": "reconcile_import", "new": 1, "upgrade": 0, "park": 1,
            "albums": [{"artist": "A", "album": "B", "route": "NEW"}],
            "parked": self.PARKED})
        self.assertIn("✅ Import complete", text)
        self.assertIn("Parked (not in library)", text)

    def test_old_payload_without_parked_key(self):
        _, text = bot.render_notification({
            "event": "reconcile_import", "new": 0, "upgrade": 0, "park": 2})
        self.assertIn("Nothing imported", text)
        self.assertIn("parked 2", text)

    def test_read_parked_from_plan(self):
        tmp = tempfile.mkdtemp(prefix="ri-parked-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        run = Path(tmp) / "reconcile" / "import-x"
        run.mkdir(parents=True)
        (run / "plan.json").write_text(json.dumps({"candidates": [
            {"route": "PARK", "route_reason": "orphan-fragment",
             "candidate": {"path": "/inbox/Madoka", "n_audio_files": 4}},
            {"route": "NEW", "candidate": {"path": "/inbox/Other"}},
        ]}), encoding="utf-8")
        with mock.patch.object(RI.cfg, "LOG_DIR", tmp):
            out = RI._read_parked("import-x")
        self.assertEqual(out, [{"folder": "Madoka", "reason": "orphan-fragment", "tracks": 4}])


if __name__ == "__main__":
    unittest.main()
