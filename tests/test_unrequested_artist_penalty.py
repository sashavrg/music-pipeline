"""
Tests for recover._unrequested_artist_penalty — the heuristic that demotes
folders whose name lists artists the query didn't ask for (the
"Chic & Sister Sledge Greatest Hits" false-match failure mode), while
preserving legitimate collab credits like "Marvin Gaye & Tammi Terrell".

Run: python3 -m unittest tests.test_unrequested_artist_penalty
"""
import unittest

from pipeline import recover as r


class PenaltyTests(unittest.TestCase):

    def _check(self, folder, artist, album, expected_penalty):
        artist_tokens = r._tokens(artist)
        album_tokens = r._tokens(album)
        got = r._unrequested_artist_penalty(folder, album_tokens, artist_tokens)
        self.assertEqual(
            got, expected_penalty,
            f"folder={folder!r} artist={artist!r} album={album!r}: "
            f"expected {expected_penalty}, got {got}",
        )

    # ── The headline failure: VA comp folder must be penalized ─────────────

    def test_chic_sister_sledge_va_comp_penalized(self):
        # Two ` & ` separators -> three segments; segments 2 and 3 have no
        # overlap with {chic, very, best} -> -60 -60 = -120.
        self._check(
            "Good Times_ The Very Best of Chic & Sister Sledge_ The Hits & The Remixes (2005)",
            artist="Chic", album="The Very Best of Chic",
            expected_penalty=-120,
        )

    # ── Legitimate collabs / duos must NOT be penalized ───────────────────

    def test_marvin_tammi_duo_not_penalized_after_mb_expansion(self):
        # After MB retry, artist becomes the full duo credit. Both segments
        # have query overlap, so no penalty.
        self._check(
            "Marvin Gaye & Tammi Terrell - United (1967)",
            artist="Marvin Gaye & Tammi Terrell", album="United",
            expected_penalty=0,
        )

    def test_marvin_tammi_duo_not_penalized_even_without_mb_expansion(self):
        # Even if user typed just "Marvin Gaye - United", the album token
        # 'united' is in segment 2, so it counts as overlap.
        self._check(
            "Marvin Gaye & Tammi Terrell - United (1967)",
            artist="Marvin Gaye", album="United",
            expected_penalty=0,
        )

    def test_artist_with_ampersand_not_penalized(self):
        # 'Earth, Wind & Fire' is one artist whose name contains '&'.
        # The post-'&' segment 'Fire - I Am' has 'fire' in artist tokens.
        self._check(
            "Earth, Wind & Fire - I Am (1979)",
            artist="Earth, Wind & Fire", album="I Am",
            expected_penalty=0,
        )

    def test_sonny_and_cher_not_penalized(self):
        self._check(
            "Sonny & Cher - The Beat Goes On",
            artist="Sonny & Cher", album="The Beat Goes On",
            expected_penalty=0,
        )

    # ── Single-segment folders: no penalty ────────────────────────────────

    def test_self_titled_no_penalty(self):
        self._check("Calibro 35 - Calibro 35 (2008)",
                    artist="Calibro 35", album="Calibro 35",
                    expected_penalty=0)

    def test_simple_album_no_penalty(self):
        self._check("Chic - C'est Chic (1978)",
                    artist="Chic", album="C'est Chic",
                    expected_penalty=0)

    def test_album_title_with_ampersand_no_split(self):
        # No whitespace around `&` -> not a separator.
        self._check("Salt&Pepper - Greatest Hits",
                    artist="Salt&Pepper", album="Greatest Hits",
                    expected_penalty=0)

    # ── Various separator forms ──────────────────────────────────────────

    def test_feat_separator_recognized(self):
        # Query is Sufjan's 'Illinois' — folder is a Pete Tong DJ mix
        # crediting Some Producer; nothing in segment 2 overlaps.
        self._check(
            "Pete Tong feat. Some Producer - Disco Mix Vol 4",
            artist="Sufjan Stevens", album="Illinois",
            expected_penalty=-60,
        )

    def test_and_not_treated_as_separator(self):
        # "and" appears in too many legitimate album titles to use as a
        # separator. Title here has "and" but no actual unrequested artist.
        self._check(
            "The Beatles - Now and Then",
            artist="The Beatles", album="Now and Then",
            expected_penalty=0,
        )

    def test_with_not_treated_as_separator(self):
        # "with" similarly appears in album titles like "Songs with Friends".
        self._check(
            "Mary Halvorson - Songs with Tomeka Reid",
            artist="Mary Halvorson", album="Songs with Tomeka Reid",
            expected_penalty=0,
        )

    # ── Edge cases ────────────────────────────────────────────────────────

    def test_empty_query_no_penalty(self):
        self._check("X & Y - Z", artist="", album="", expected_penalty=0)

    def test_post_separator_empty_segment_skipped(self):
        # Trailing ' & ' (unusual but guard against it).
        self._check("Chic & ", artist="Chic", album="C'est Chic",
                    expected_penalty=0)


class VaMarkerPenaltyTests(unittest.TestCase):
    """The folder-name marker penalty for VA / Various Artists / Compilation."""

    def _check(self, folder, artist, expected_penalty):
        got = r._va_marker_penalty(folder, r._tokens(artist))
        self.assertEqual(
            got, expected_penalty,
            f"folder={folder!r} artist={artist!r}: "
            f"expected {expected_penalty}, got {got}",
        )

    def test_va_dash_prefix_penalized(self):
        # The headline failure: VA-Good_Times_The_Very_Best_Of_Chic_And_Sister_Sledge...
        self._check(
            "VA-Good_Times_The_Very_Best_Of_Chic_And_Sister_Sledge_The_Hits_And_The_Remixes-Remastered-2CD-FLAC-2005-THEVOiD",
            artist="Chic", expected_penalty=-80,
        )

    def test_va_space_dash_prefix_penalized(self):
        self._check("VA - Good Times The Very Best Of Chic",
                    artist="Chic", expected_penalty=-80)

    def test_va_dotted_penalized(self):
        self._check("V.A. - Disco Classics", artist="Chic", expected_penalty=-80)

    def test_various_artists_penalized(self):
        self._check("Various Artists - Best Of 1979",
                    artist="Chic", expected_penalty=-80)

    def test_compilation_marker_penalized(self):
        self._check("Disco Compilation - 1979 Edition",
                    artist="Chic", expected_penalty=-80)

    def test_no_penalty_when_user_searches_va(self):
        # If you ACTUALLY want a VA comp, no penalty.
        self._check("VA - Disco Classics",
                    artist="Various Artists", expected_penalty=0)

    def test_no_penalty_for_real_album(self):
        self._check("Chic - The Very Best of Chic (2000)",
                    artist="Chic", expected_penalty=0)

    def test_band_name_containing_va_not_penalized(self):
        # 'Cavalcade' or 'Valencia' don't start with VA word-boundary token.
        self._check("Cavalcade - Some Album", artist="Cavalcade", expected_penalty=0)
        self._check("Valencia - Live", artist="Valencia", expected_penalty=0)

    def test_empty_artist_no_penalty(self):
        self._check("VA - Whatever", artist="", expected_penalty=0)


class ScoringIntegrationTests(unittest.TestCase):
    """End-to-end: VA-comp folder must score below a clean best-of folder."""

    def _file(self, path, size=20_000_000, length=240, bitrate=900_000):
        return {
            'filename': path, 'size': size, 'length': length,
            'bitRate': bitrate, 'bitDepth': 16, 'sampleRate': 44100,
        }

    def _score(self, folder_name, n_tracks, artist, album):
        files = [self._file(f"{folder_name}/{i:02} Track.flac")
                 for i in range(n_tracks)]
        return r._score_folder(
            files, upload_speed=5_000_000,
            album_tokens=r._tokens(album),
            artist_tokens=r._tokens(artist),
        )

    def test_va_comp_loses_to_real_best_of(self):
        va = self._score(
            "Good Times_ The Very Best of Chic & Sister Sledge_ The Hits & The Remixes (2005)",
            n_tracks=28, artist="Chic", album="The Very Best of Chic",
        )
        real = self._score(
            "Chic - The Very Best of Chic (2000)",
            n_tracks=18, artist="Chic", album="The Very Best of Chic",
        )
        self.assertGreater(real, va,
            f"real best-of should outscore VA comp (real={real}, va={va})")

    def test_underscore_va_prefix_loses_to_real_best_of(self):
        # The actual top-ranked folder seen in production. Underscores between
        # words and capital `And`, so the artist-credit-separator penalty alone
        # didn't fire — the VA-marker penalty must catch it.
        va_under = self._score(
            "VA-Good_Times_The_Very_Best_Of_Chic_And_Sister_Sledge_The_Hits_And_The_Remixes-Remastered-2CD-FLAC-2005-THEVOiD",
            n_tracks=28, artist="Chic", album="The Very Best of Chic",
        )
        real = self._score(
            "The Very Best of Chic (2000)",
            n_tracks=13, artist="Chic", album="The Very Best of Chic",
        )
        self.assertGreater(real, va_under,
            f"real Chic best-of should outscore underscore-VA comp "
            f"(real={real}, va_under={va_under})")


if __name__ == '__main__':
    unittest.main()
