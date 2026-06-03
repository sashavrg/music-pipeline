"""Tests for fill_missing_tracks generic-query guard and fallback dedup.

Regression coverage for the May 2026 incident where:
  - A held "Disc 2" folder caused fill-missing to search "Disc 2", match
    arbitrary albums' disc subfolders (e.g. Aretha Franklin's
    Queen_Of_Soul_Disc_1), and queue them into orphan complete/ folders.
  - "Rare Groove Story" hit fallback_assign every 6h, re-downloading the
    same `20. Pina Colada` track (already present) into duplicate files
    with timestamp suffixes.
"""

import unittest

from pipeline.fill_missing_tracks import fallback_assign, is_generic_query


class _FakeFolder:
    def __init__(self, files):
        self.files = files


class GenericQueryTests(unittest.TestCase):
    def test_blocks_disc_and_cd_and_volume_patterns(self):
        for q in ("Disc 2", "CD 1", "cd2", "Vol 3", "Volume 12", "1", "07"):
            with self.subTest(q=q):
                self.assertTrue(is_generic_query(q))

    def test_allows_real_album_names(self):
        for q in ("disc", "Rare Groove Story", "Disc 2 Soundtrack",
                  "Music Has The Right To Children"):
            with self.subTest(q=q):
                self.assertFalse(is_generic_query(q))


class FallbackAssignTests(unittest.TestCase):
    def test_skips_when_source_only_has_present_tracks(self):
        """Rare Groove Story regression: source has track 20 only, we need
        [1..12]; fallback must NOT requeue track 20."""
        src = _FakeFolder([{"filename": r"foo\bar\20. Pina Colada.flac"}])
        self.assertEqual(
            fallback_assign([src], {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12}),
            [],
        )

    def test_falls_through_when_filenames_unparseable(self):
        src = _FakeFolder([
            {"filename": r"foo\untagged.flac"},
            {"filename": r"foo\another.flac"},
        ])
        result = fallback_assign([src], {1, 2, 3})
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0][1]), 2)

    def test_falls_through_when_source_has_needed_track(self):
        src = _FakeFolder([
            {"filename": r"foo\03. Track.flac"},
            {"filename": r"foo\99. Extra.flac"},
        ])
        result = fallback_assign([src], {1, 2, 3})
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0][1]), 2)

    def test_empty_when_no_folders(self):
        self.assertEqual(fallback_assign([], {1, 2, 3}), [])


if __name__ == "__main__":
    unittest.main()
