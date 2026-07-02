"""Tests for quarantine_requeue — the folder parser and the identity-based
in-library check that replaced the substring-LIKE matcher (2026-07-02: the old
matcher's false negatives re-acquired owned albums; its false positives gated
a hard delete — now the only disposition is a move to the gate's inbox).
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import quarantine_requeue as QR   # noqa: E402
from pipeline import identity as I              # noqa: E402


class TestCleanName(unittest.TestCase):
    def test_strips_requeued_suffix(self):
        # the marker forked identity keys -> same release queued twice
        self.assertEqual(
            QR.clean_name('Album Name (FLAC).requeued-20260527-1854'),
            'Album Name')

    def test_strips_requeued_suffix_with_seconds(self):
        self.assertEqual(QR.clean_name('X.requeued-20260527-185412'), 'X')

    def test_plain_name_unchanged(self):
        self.assertEqual(QR.clean_name('Artist - Album'), 'Artist - Album')


class TestParseFolder(unittest.TestCase):
    def _parse(self, name):
        return QR.parse_folder(Path(f'/q/{name}'))

    def test_normal_artist_album(self):
        self.assertEqual(self._parse('Daniel Johnston - Alive in New York City'),
                         ('Daniel Johnston', 'Alive in New York City'))

    def test_date_fragment_is_not_an_artist(self):
        # "1977-05-21 - Venue" loses its year prefix, leaving "05-21 - Venue";
        # a date is not an artist and would poison the slskd query
        artist, album = self._parse('1977-05-21 - Lakeland Civic Center Arena')
        self.assertEqual(artist, '')

    def test_disc_marker_is_not_an_artist(self):
        artist, album = self._parse('DISC 3 - TV LUPIN UNRELEASED BGM Vol.3')
        self.assertEqual(artist, '')

    def test_cd_marker_is_not_an_artist(self):
        self.assertEqual(self._parse('CD2 - Bonus Material')[0], '')

    def test_year_only_artist_still_refused(self):
        self.assertEqual(self._parse('1998 - Some Album')[0], '')

    def test_requeued_suffix_gone_from_album(self):
        artist, album = self._parse(
            'The Cool Kids - BABY OIL (FLAC).requeued-20260527-1854')
        self.assertEqual((artist, album), ('The Cool Kids', 'BABY OIL'))

    def test_year_prefix_inside_album_stripped(self):
        # "Artist - 2022 - Album" — the year survives the artist split and
        # forked the identity key (Cool Kids double-queue, 2026-07-02)
        artist, album = self._parse('The Cool Kids - 2022 - BABY OIL STAIRCASE')
        self.assertEqual((artist, album), ('The Cool Kids', 'BABY OIL STAIRCASE'))


def _lib_row(aid, artist, album, rg=''):
    return I.AlbumRow(aid, artist, album, 2020, '', rg, [])


class TestInLibrary(unittest.TestCase):
    """The oracle check must be the gate's convergence, not substring LIKE."""

    def setUp(self):
        self._orig = QR._LIB_CACHE
        QR._LIB_CACHE = [
            _lib_row(1, 'Journey', 'Live In Houston 1981: The Escape Tour'),
            _lib_row(2, 'Boards of Canada', 'Music Has the Right to Children',
                     rg='11d276e6-2b78-3247-9906-0d4bee1e17ff'),
        ]
        self.addCleanup(self._restore)

    def _restore(self):
        QR._LIB_CACHE = self._orig

    def test_exact_album_is_owned(self):
        cand = I.AlbumRow(-1, 'Journey', 'Live In Houston 1981: The Escape Tour',
                          None, '', '', [])
        self.assertTrue(QR.in_library(cand))

    def test_mbid_candidate_converges_on_library_rg(self):
        cand = I.AlbumRow(-1, 'BoC', 'MHTRTC', None, '',
                          '11d276e6-2b78-3247-9906-0d4bee1e17ff', [])
        self.assertTrue(QR.in_library(cand))

    def test_absent_album_is_not_owned(self):
        cand = I.AlbumRow(-1, 'Glass Animals', 'Dreamland', None, '', '', [])
        self.assertFalse(QR.in_library(cand))

    def test_substring_overmatch_does_not_own(self):
        # the old LIKE matcher would have matched album "Live" against
        # anything containing the word; the oracle must not
        cand = I.AlbumRow(-1, 'Someone Else', 'Live', None, '', '', [])
        self.assertFalse(QR.in_library(cand))


class TestFolderCandidate(unittest.TestCase):
    def test_falls_back_to_parsed_names_when_scan_fails(self):
        with mock.patch.object(QR.reconcile, 'scan_folder_to_albumrow',
                               side_effect=RuntimeError('no audio')):
            row = QR.folder_candidate(Path('/q/X - Y'), 'X', 'Y')
        self.assertEqual((row.album_id, row.albumartist, row.album), (-1, 'X', 'Y'))

    def test_prefers_tag_row_when_album_present(self):
        tag_row = I.AlbumRow(-1, 'Journey', 'Live In Houston 1981', None, '', '', [])
        with mock.patch.object(QR.reconcile, 'scan_folder_to_albumrow',
                               return_value=(tag_row, None)):
            row = QR.folder_candidate(Path('/q/mangled name'), 'wrong', 'names')
        self.assertEqual(row.albumartist, 'Journey')

    def test_empty_tag_album_falls_back(self):
        tag_row = I.AlbumRow(-1, 'Someone', '', None, '', '', [])
        with mock.patch.object(QR.reconcile, 'scan_folder_to_albumrow',
                               return_value=(tag_row, None)):
            row = QR.folder_candidate(Path('/q/A - B'), 'A', 'B')
        self.assertEqual(row.album, 'B')


if __name__ == '__main__':
    unittest.main()
