"""Tests for wishlist_fulfillment fuzzy matchers.

Designed to be conservative: we accept missed fulfillments (the row gets
re-queried next cycle, harmless) but never a false positive that marks an
unrelated album as fulfilled.
"""

import unittest

from pipeline.wishlist_fulfillment import (
    album_matches,
    artist_matches,
    find_fulfilled_ids,
    normalize,
)


class NormalizeTests(unittest.TestCase):
    def test_strips_diacritics_and_punctuation(self):
        self.assertEqual(normalize("Aleksi Perälä"), "aleksi perala")
        self.assertEqual(normalize("blink‐182"), "blink 182")
        self.assertEqual(normalize("  Kern, Vol. 3  "), "kern vol 3")

    def test_handles_none_and_empty(self):
        self.assertEqual(normalize(None), "")
        self.assertEqual(normalize(""), "")


class ArtistMatchesTests(unittest.TestCase):
    def test_exact(self):
        self.assertTrue(artist_matches("Glass Animals", "Glass Animals"))

    def test_diacritics_and_dashes(self):
        self.assertTrue(artist_matches("Aleksi Perälä", "Aleksi Perala"))
        self.assertTrue(artist_matches("blink-182", "blink‐182"))

    def test_token_subset_either_direction(self):
        # Wishlist sometimes carries a fuller credit than beets keeps
        self.assertTrue(artist_matches("DJ LOSTBOI & Torus", "Torus"))
        # Or vice versa
        self.assertTrue(artist_matches("The Body", "The Body & Full of Hell"))

    def test_various_artists_passes_through(self):
        self.assertTrue(artist_matches("Various Artists", "Some Compiler"))
        self.assertTrue(artist_matches("Some Compiler", "VA"))

    def test_unrelated_artists_rejected(self):
        self.assertFalse(artist_matches("Glass Animals", "Robert Miles"))
        self.assertFalse(artist_matches("Nas", "Anastasia"))


class AlbumMatchesTests(unittest.TestCase):
    def test_exact_after_normalize(self):
        self.assertTrue(album_matches("Kern Vol.3", "Kern, Vol. 3"))
        self.assertTrue(album_matches("Dreamland", "Dreamland"))

    def test_strong_prefix(self):
        self.assertTrue(album_matches(
            "I'm Wide Awake, It's Morning",
            "I'm Wide Awake It's Morning (2022 VMP Remaster Vinyl)",
        ))

    def test_token_subset(self):
        self.assertTrue(album_matches(
            "Life is Like a Dice Game",
            "Understanding / Life Is Like a Dice Game",
        ))

    def test_rejects_unrelated_albums(self):
        # Wishlist asked for Volume Two; library has Volume One
        self.assertFalse(album_matches(
            "Ambivert Tools Volume Two EP",
            "Ambivert Tools Volume One",
        ))
        # Albums sharing one word should not match
        self.assertFalse(album_matches("Dreamland", "Disneyland"))

    def test_rejects_short_prefix(self):
        # Single short tokens shouldn't tip the scale
        self.assertFalse(album_matches("Live", "Live in Houston 1981"))


class FindFulfilledIdsTests(unittest.TestCase):
    def setUp(self):
        self.pending = [
            {"id": 1, "artist": "Glass Animals",  "album": "Dreamland"},
            {"id": 2, "artist": "Lone",           "album": "Ambivert Tools Volume Two EP"},
            {"id": 3, "artist": "Various",        "album": "Rare Groove Story"},
            {"id": 4, "artist": "Torus",          "album": "The Flash (Deluxe)"},
        ]

    def test_matches_canonical_imports(self):
        library = [
            ("Glass Animals", "Dreamland"),
            ("Various Artists", "Rare Groove Story CD1"),
            ("DJ LOSTBOI & Torus", "The Flash (Deluxe)"),
        ]
        self.assertEqual(find_fulfilled_ids(self.pending, library), {1, 3, 4})

    def test_misses_wrong_volume(self):
        # Vol One in library should NOT mark Vol Two pending as fulfilled
        library = [("Lone", "Ambivert Tools Volume One")]
        self.assertEqual(find_fulfilled_ids(self.pending, library), set())

    def test_empty_inputs(self):
        self.assertEqual(find_fulfilled_ids([], [("X", "Y")]), set())
        self.assertEqual(find_fulfilled_ids(self.pending, []), set())


if __name__ == "__main__":
    unittest.main()
