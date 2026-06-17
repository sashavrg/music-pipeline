"""Tests for identity.py — the single release-identity resolver + oracle.

Two layers:
  - Pure-function + synthetic-fixture tests (always run; encode the invariants
    derived from the live-library red-team so CI enforces them without a DB).
  - Live-library invariant (skipped when the beets DB is absent, e.g. in CI):
    running over the real library must produce ZERO false-merges.

The synthetic fixtures mirror real validated cases (see memory: music-pipeline-rethink).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import identity as I  # noqa: E402


def A(aid, artist, album, year=2000, mbid='', rg='', items=None):
    return I.AlbumRow(aid, artist, album, year, mbid, rg, items or [])


def trk(disc, track, title):
    return {'disc': disc, 'track': track, 'title': title, 'path': f'/x/{disc}-{track}.flac'}


class TestNormalize(unittest.TestCase):
    def test_trailing_year_stripped(self):
        self.assertEqual(I.normalize('Summer of Love (2024)', 'album'), 'summer of love')

    def test_edition_tokens_preserved(self):
        # the load-bearing case: clean_name would have stripped these and false-merged
        self.assertNotEqual(I.normalize('Travelling Without Moving (Remastered)', 'album'),
                            I.normalize('Travelling Without Moving', 'album'))
        self.assertNotEqual(I.normalize('Random Access Memories (10th Anniversary Edition)', 'album'),
                            I.normalize('Random Access Memories', 'album'))

    def test_volume_tokens_distinct(self):
        self.assertNotEqual(I.normalize('Adventure Time, Vol. 1', 'album'),
                            I.normalize('Adventure Time, Vol. 2', 'album'))

    def test_no_leetspeak_folding(self):
        self.assertEqual(I.normalize('Gent1e $oul', 'artist'), 'gent1e $oul')

    def test_format_tag_stripped(self):
        self.assertEqual(I.normalize('Discovery [FLAC]', 'album'), 'discovery')
        self.assertEqual(I.normalize('Nacre [WEB FLAC 16 44]', 'album'), 'nacre')

    def test_case_and_dash_fold(self):
        self.assertEqual(I.normalize('Sonic Excess In Its Purest Form', 'album'),
                         I.normalize('Sonic Excess in Its Purest Form', 'album'))

    def test_artist_primary_credit(self):
        # band-internal 'And' kept; trailing feat dropped
        self.assertEqual(I.normalize('Malcolm McLaren And The Bootzilla Orchestra', 'artist'),
                         'malcolm mclaren and the bootzilla orchestra')
        self.assertEqual(I.normalize('Artist feat. Someone', 'artist'), 'artist')

    def test_track_scope_strips_feat_remaster(self):
        self.assertEqual(I.normalize('So Hot (feat. X)', 'track'), 'sohot')
        self.assertEqual(I.normalize('Lua (2014 Remaster)', 'track'), 'lua')

    def test_va_detection(self):
        self.assertTrue(I.is_va('Various Artists'))
        self.assertTrue(I.is_va('VA'))
        self.assertTrue(I.is_va(''))
        self.assertFalse(I.is_va('Rivet'))


class TestIdentityResolution(unittest.TestCase):
    def keys(self, albums):
        return I.build_index(albums)['by_album']

    def test_year_not_in_hash(self):
        # Coil: same release, year 2019 vs 1992 -> must merge
        by = self.keys([A(3661, 'Coil', 'Stolen and Contaminated Songs', 2019),
                        A(3840, 'Coil', 'Stolen and Contaminated Songs', 1992)])
        self.assertEqual(by[3661], by[3840])

    def test_titles_not_in_hash_complementary_fragments(self):
        # Koushik: keeper missing track2, fragment IS track2 -> not subsets, must merge
        by = self.keys([
            A(3546, 'Koushik', 'Out My Window', items=[trk(0, 1, 'a'), trk(0, 3, 'c')]),
            A(3380, 'Koushik', 'Out My Window', items=[trk(0, 2, 'Be With')]),
        ])
        self.assertEqual(by[3546], by[3380])

    def test_non_uuid_mbid_ignored(self):
        # Astrix: mb_albumid '8186719' is a Discogs int, not UUID -> fall to content-hash
        by = self.keys([A(1944, 'Astrix', 'He.Art', mbid='8186719'),
                        A(3834, 'Astrix', 'He.art')])
        self.assertEqual(by[1944], by[3834])
        self.assertTrue(by[1944].startswith('ch:'))

    def test_rg_dominates_album_id(self):
        # Vulfpeck Hill Climber: different mb_albumid, SAME release-group -> merge
        rg = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        by = self.keys([
            A(3624, 'Vulfpeck', 'Hill Climber', mbid='11111111-1111-1111-1111-111111111111', rg=rg),
            A(3626, 'Vulfpeck', 'Hill Climber', mbid='22222222-2222-2222-2222-222222222222', rg=rg),
        ])
        self.assertEqual(by[3624], by[3626])
        self.assertTrue(by[3624].startswith('mbrg:'))

    def test_convergence_bare_attaches_to_mbid(self):
        # Title Fight: full copy has UUID, fragment is bare -> bare attaches to tier-1
        by = self.keys([
            A(3586, 'Title Fight', 'Shed', mbid='9fc4104a-1111-2222-3333-444444444444'),
            A(3589, 'Title Fight', 'Shed'),
        ])
        self.assertEqual(by[3586], by[3589])
        self.assertTrue(by[3586].startswith('mbid:'))

    def test_disc_split_genshin(self):
        # three album_ids, discs 1/2/3 -> three DISTINCT identities
        ch = 'HOYO-MiX'
        by = self.keys([
            A(3666, ch, 'Genshin', items=[trk(1, 1, 'a'), trk(1, 2, 'b')]),
            A(3669, ch, 'Genshin', items=[trk(2, 1, 'c'), trk(2, 2, 'd')]),
            A(3667, ch, 'Genshin', items=[trk(3, 1, 'e'), trk(3, 2, 'f')]),
        ])
        self.assertEqual(len({by[3666], by[3669], by[3667]}), 3)

    def test_multidisc_single_release_not_split(self):
        # one album_id spanning discs 1-4 with no competing sibling -> ONE key
        by = self.keys([A(71, 'Aphex Twin', 'SAW II',
                          items=[trk(1, 1, 'a'), trk(2, 1, 'b'), trk(3, 1, 'c'), trk(4, 1, 'd')])])
        self.assertEqual(len(set(by.values())), 1)

    def test_disc0_fragments_not_split(self):
        # ML Buch: keeper disc0, fragment disc1 -> both 'unknown' disc, must merge
        by = self.keys([
            A(3073, 'ML Buch', 'Skinned', items=[trk(0, 1, 'a'), trk(0, 2, 'b')]),
            A(3814, 'ML Buch', 'Skinned', items=[trk(1, 7, 'sap')]),
        ])
        self.assertEqual(by[3073], by[3814])

    def test_must_not_merge_different_albums(self):
        by = self.keys([A(3807, 'Adventure Time', 'Adventure Time, Vol. 1'),
                        A(3870, 'Adventure Time', 'Adventure Time, Vol. 2')])
        self.assertNotEqual(by[3807], by[3870])

    def test_va_vs_named_artist_same_title_differ(self):
        # Bear Bile: VA comp vs Rivet's album -> artist preserved, must differ
        by = self.keys([A(1830, 'Various Artists', 'Bear Bile'),
                        A(2630, 'Rivet', 'Bear Bile')])
        self.assertNotEqual(by[1830], by[2630])

    def test_empty_album_parks_to_review(self):
        ident = I.base_identity(A(999, 'Someone', '[FLAC]'))
        self.assertEqual(ident.tier, 3)
        self.assertTrue(ident.key.startswith('review:'))


class TestLiveLibraryInvariant(unittest.TestCase):
    """The critical safety property over the REAL library: zero false-merges.
    Skipped when the beets DB is absent (CI)."""

    def setUp(self):
        if not os.path.exists(I.BEETS_DB):
            self.skipTest('beets DB not present (CI)')

    def test_no_false_merge_in_live_library(self):
        idx = I.build_index(I.load_albums())
        rows = {a.album_id: a for a in I.load_albums()}
        from collections import defaultdict
        groups = defaultdict(list)
        for aid, key in idx['by_album'].items():
            groups[key].append(aid)
        offenders = []
        for key, aids in groups.items():
            if len(aids) < 2 or key.startswith(('mbid:', 'mbrg:')):
                continue  # MBID merges across editions are legitimate
            sigs = {(I.VA_SENTINEL if I.is_va(rows[a].albumartist)
                     else I.normalize(rows[a].albumartist, 'artist'),
                     I.normalize(rows[a].album, 'album')) for a in aids}
            if len(sigs) > 1:
                offenders.append((key, aids))
        self.assertEqual(offenders, [], f"content-hash false-merges: {offenders}")


if __name__ == '__main__':
    unittest.main()
