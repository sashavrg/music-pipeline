"""Tests for reconcile.py — the one gate / one writer.

Two layers (mirrors test_identity.py):
  - Pure-function tests (always run): the quality ladder, disc normalization,
    routing decisions, candidate-key resolution, content-hash body — these encode
    the red-team mitigations so CI enforces them without a DB.
  - Live-library/dup-plan invariants (skipped when the beets DB or dup-plan is
    absent): the gate must PARK the complementary fragments + the Genshin discs +
    the case-variant dirs (the exact critical cases the red-team surfaced) and
    only DROP genuine subsets.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import reconcile as R   # noqa: E402
from pipeline import identity as I    # noqa: E402


def Q(lossless=True, bd=16, sr=44100, br=900000, fail=False, mislabel=False):
    return {'lossless_all': lossless, 'min_bitdepth': bd, 'min_samplerate': sr,
            'min_bitrate': br, 'any_probe_fail': fail, 'any_mislabel': mislabel,
            'codec_set': [], 'n_files': 1, 'basis': 'test'}


def meta(**kw):
    m = R.ScanMeta(folder=kw.pop('folder', '/x'))
    for k, v in kw.items():
        setattr(m, k, v)
    return m


def ident(key, tier=2, reason=None):
    return I.Identity(key=key, tier=tier, confidence=0.9, review_reason=reason)


class TestNormDisc(unittest.TestCase):
    def test_collapse_0_and_1(self):
        self.assertEqual(R._norm_disc(0), 1)
        self.assertEqual(R._norm_disc(1), 1)
        self.assertEqual(R._norm_disc(None), 1)

    def test_keep_multidisc_distinct(self):
        self.assertEqual(R._norm_disc(2), 2)
        self.assertEqual(R._norm_disc(3), 3)


class TestStrictlyBetter(unittest.TestCase):
    full = {(1, n) for n in range(1, 11)}

    def test_lossless_beats_lossy(self):
        v = R.strictly_better(Q(lossless=True), Q(lossless=False), False, self.full, self.full)
        self.assertEqual(v, 'STRICTLY_BETTER')

    def test_lossy_loses_to_lossless(self):
        v = R.strictly_better(Q(lossless=False), Q(lossless=True), False, self.full, self.full)
        self.assertEqual(v, 'WORSE')

    def test_higher_bitdepth_wins(self):
        v = R.strictly_better(Q(bd=24, sr=96000), Q(bd=16, sr=44100), False, self.full, self.full)
        self.assertEqual(v, 'STRICTLY_BETTER')

    def test_lower_bitdepth_loses(self):
        v = R.strictly_better(Q(bd=16), Q(bd=24), False, self.full, self.full)
        self.assertEqual(v, 'WORSE')

    def test_fragment_never_better(self):
        # a single-track fragment vs a complete album -> WORSE regardless of codec
        frag = {(1, 1)}
        v = R.strictly_better(Q(bd=24, sr=192000), Q(bd=16), True, frag, self.full)
        self.assertEqual(v, 'WORSE')

    def test_missing_track_is_worse(self):
        missing = self.full - {(1, 5)}
        v = R.strictly_better(Q(bd=24), Q(bd=16), False, missing, self.full)
        self.assertEqual(v, 'WORSE')

    def test_candidate_corruption_vetoes(self):
        v = R.strictly_better(Q(fail=True), Q(), False, self.full, self.full)
        self.assertEqual(v, 'AMBIGUOUS')

    def test_candidate_mislabel_vetoes(self):
        v = R.strictly_better(Q(mislabel=True), Q(), False, self.full, self.full)
        self.assertEqual(v, 'AMBIGUOUS')

    def test_proven_corrupt_library_replaced(self):
        # library is corrupt/mislabeled, candidate is clean + complete -> upgrade
        v = R.strictly_better(Q(bd=16), Q(mislabel=True), False, self.full, self.full)
        self.assertEqual(v, 'STRICTLY_BETTER')

    def test_equal_is_equal(self):
        v = R.strictly_better(Q(), Q(), False, self.full, self.full)
        self.assertEqual(v, 'EQUAL')

    def test_more_tracks_same_codec_better(self):
        more = self.full | {(1, 11), (1, 12)}
        v = R.strictly_better(Q(), Q(), False, more, self.full)
        self.assertEqual(v, 'STRICTLY_BETTER')

    def test_tradeoff_is_ambiguous(self):
        # candidate is divergent (neither subset) -> needs eyes
        a = {(1, 1), (1, 2), (1, 99)}
        b = {(1, 1), (1, 2), (1, 50)}
        v = R.strictly_better(Q(), Q(), False, a, b)
        self.assertEqual(v, 'AMBIGUOUS')


class TestChBody(unittest.TestCase):
    def test_matches_identity_tier2_key(self):
        # _ch_body(artist,album) must equal base_identity's tier-2 key for a no-MBID row
        row = I.AlbumRow(-1, 'Koushik', 'Out My Window', 2008, '', '', [])
        base = I.base_identity(row)
        self.assertEqual(base.tier, 2)
        self.assertEqual(R._ch_body('Koushik', 'Out My Window'), base.key)

    def test_va_body_uses_sentinel(self):
        row = I.AlbumRow(-1, 'Various Artists', 'Bear Bile', 2015, '', '', [])
        self.assertEqual(R._ch_body('Various Artists', 'Bear Bile'), I.base_identity(row).key)


class TestRoute(unittest.TestCase):
    def setUp(self):
        self.row = I.AlbumRow(-1, 'Artist', 'Album', 2000, '', '', [])

    def test_tier3_parks(self):
        r, _ = R.route(ident('review:x', tier=3, reason='empty'), None, meta(),
                       None, self.row, set(), False)
        self.assertEqual(r, 'PARK')

    def test_fragment_with_family_parks(self):
        r, why = R.route(ident('ch:abc'), {'album_ids': [10], 'keys': ['ch:abc'], 'final_key': 'ch:abc'},
                         meta(is_fragment=True), 'WORSE', self.row, set(), False)
        self.assertEqual(r, 'PARK')
        self.assertIn('fragment', why)

    def test_orphan_fragment_parks(self):
        r, _ = R.route(ident('ch:abc'), None, meta(is_fragment=True), None, self.row, set(), False)
        self.assertEqual(r, 'PARK')

    def test_new_when_absent(self):
        r, _ = R.route(ident('ch:abc'), None, meta(), None, self.row, set(), False)
        self.assertEqual(r, 'NEW')

    def test_multi_id_family_parks(self):
        fam = {'album_ids': [10, 11], 'keys': ['ch:abc'], 'final_key': 'ch:abc'}
        r, why = R.route(ident('ch:abc'), fam, meta(), 'STRICTLY_BETTER', self.row, set(), False)
        self.assertEqual(r, 'PARK')
        self.assertIn('multi-id', why)

    def test_upgrade_when_strictly_better(self):
        fam = {'album_ids': [10], 'keys': ['ch:abc'], 'final_key': 'ch:abc'}
        r, _ = R.route(ident('ch:abc'), fam, meta(), 'STRICTLY_BETTER', self.row, set(), False)
        self.assertEqual(r, 'UPGRADE')

    def test_duplicate_when_worse_or_equal(self):
        fam = {'album_ids': [10], 'keys': ['ch:abc'], 'final_key': 'ch:abc'}
        for vd in ('WORSE', 'EQUAL'):
            r, _ = R.route(ident('ch:abc'), fam, meta(), vd, self.row, set(), False)
            self.assertEqual(r, 'DUPLICATE')

    def test_ambiguous_quality_parks(self):
        fam = {'album_ids': [10], 'keys': ['ch:abc'], 'final_key': 'ch:abc'}
        r, _ = R.route(ident('ch:abc'), fam, meta(), 'AMBIGUOUS', self.row, set(), False)
        self.assertEqual(r, 'PARK')

    def test_reserved_key_intra_run_dup(self):
        r, why = R.route(ident('ch:abc'), None, meta(), None, self.row, {'ch:abc'}, False)
        self.assertEqual(r, 'DUPLICATE')
        self.assertIn('intra-run', why)

    def test_tier2_collab_artist_upgrade_parks(self):
        # destructive op on a tier-2 collaboration-credit key needs MBID -> PARK
        collab = I.AlbumRow(-1, 'Artist A & Artist B', 'Album', 2000, '', '', [])
        fam = {'album_ids': [10], 'keys': ['ch:abc'], 'final_key': 'ch:abc'}
        r, why = R.route(ident('ch:abc'), fam, meta(), 'STRICTLY_BETTER', collab, set(), False)
        self.assertEqual(r, 'PARK')

    def test_synthesized_name_parks_without_trust(self):
        r, _ = R.route(ident('ch:abc'), None, meta(synthesized_name=True, reason='no-album-tag'),
                       None, self.row, set(), False)
        self.assertEqual(r, 'PARK')

    def test_tier2_near_dup_parks(self):
        # a tier-2 NEW with a near-named library album -> PARK (possible edition variant)
        r, why = R.route(ident('ch:abc', tier=2), None, meta(), None, self.row, set(), False,
                         near_dup_id=3810)
        self.assertEqual(r, 'PARK')
        self.assertIn('near-duplicate', why)

    def test_tier1_new_not_blocked_by_near_dup(self):
        # tier-1 (MBID-backed) NEW is trustworthy; near_dup guard does not park it
        r, _ = R.route(ident('mbrg:x', tier=1), None, meta(), None, self.row, set(), False,
                       near_dup_id=3810)
        self.assertEqual(r, 'NEW')


class TestNearDup(unittest.TestCase):
    def test_substring_album_same_artist_matches(self):
        # Pat Metheny case: candidate 'The Road To You' vs library full subtitle
        lib = [I.AlbumRow(3810, 'Pat Metheny Group', 'The Road to You: Recorded Live in Europe',
                          1993, '', '', [])]
        cand = I.AlbumRow(-1, 'Pat Metheny Group', 'The Road To You', 1993, '', '', [])
        self.assertEqual(R._near_dup_in_library(cand, lib), 3810)

    def test_different_album_same_artist_no_match(self):
        # Melvins case: 'Stoner Witch' is genuinely not 'Houdini'
        lib = [I.AlbumRow(2992, 'Melvins', 'Houdini', 1993, '', '', [])]
        cand = I.AlbumRow(-1, 'Melvins', 'Stoner Witch', 1994, '', '', [])
        self.assertIsNone(R._near_dup_in_library(cand, lib))

    def test_different_artist_no_match(self):
        lib = [I.AlbumRow(100, 'Other Band', 'The Road to You: Live', 1993, '', '', [])]
        cand = I.AlbumRow(-1, 'Pat Metheny Group', 'The Road To You', 1993, '', '', [])
        self.assertIsNone(R._near_dup_in_library(cand, lib))


class TestResolveCandidateKeys(unittest.TestCase):
    def test_cross_tier_converges_to_library_tier1(self):
        # candidate has no MBID (tier-2 ch:) but library copy is tier-1 (rg) with same
        # content-hash body -> candidate must resolve into the tier-1 family, NOT NEW.
        rg = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        lib = [I.AlbumRow(100, 'Vulfpeck', 'Hill Climber', 2018, '', rg, [])]
        cand = I.AlbumRow(-1, 'Vulfpeck', 'Hill Climber', 2018, '', '', [])
        idn, fam = R.resolve_candidate_keys(cand, lib)
        self.assertIsNotNone(fam)
        self.assertIn(100, fam['album_ids'])
        self.assertTrue(idn.key.startswith('mbrg:'))

    def test_absent_is_new(self):
        lib = [I.AlbumRow(100, 'Someone', 'Else', 2000, '', '', [])]
        cand = I.AlbumRow(-1, 'Nobody', 'Nowhere', 2000, '', '', [])
        idn, fam = R.resolve_candidate_keys(cand, lib)
        self.assertIsNone(fam)

    def test_va_vs_named_do_not_match(self):
        lib = [I.AlbumRow(100, 'Rivet', 'Bear Bile', 2015, '', '', [])]
        cand = I.AlbumRow(-1, 'Various Artists', 'Bear Bile', 2015, '', '', [])
        idn, fam = R.resolve_candidate_keys(cand, lib)
        self.assertIsNone(fam)


class TestLiveDupPlanInvariant(unittest.TestCase):
    """The dup-plan gate must never DROP a complementary fragment, a distinct disc,
    or a case-variant sibling. Skipped when the DB or dup-plan is absent."""
    PLAN = '/root/pipeline_dup_plan.json'

    def setUp(self):
        if not (os.path.exists(I.BEETS_DB) and os.path.exists(self.PLAN)):
            self.skipTest('beets DB or dup-plan not present')
        self.results = R.build_dup_plan(self.PLAN, I.BEETS_DB)

    def _route(self, keep, drop):
        for e in self.results:
            if e.get('keep_id') == keep and e.get('drop_id') == drop:
                return e['route']
        return None

    def test_genshin_discs_never_dropped(self):
        # 3669 (disc 2) keep; 3666 (disc 1) / 3667 (disc 3) are DISTINCT discs
        self.assertNotEqual(self._route(3669, 3666), 'DUPPLAN_DROP')
        self.assertNotEqual(self._route(3669, 3667), 'DUPPLAN_DROP')

    def test_complementary_fragments_park(self):
        # 3866 (track 3 'Candidate') fills a gap in 3875; 3380 (track 2) fills 3546
        self.assertEqual(self._route(3875, 3866), 'PARK')
        self.assertEqual(self._route(3546, 3380), 'PARK')

    def test_architects_case_variant_not_dropped(self):
        self.assertNotEqual(self._route(3876, 95), 'DUPPLAN_DROP')

    def test_genuine_subset_drops(self):
        # 3857 (track 1) is fully contained in 3807 (12 tracks): a genuine subset must
        # route DROP, or DUPPLAN_DONE if already removed (executed 2026-06-14) — but
        # NEVER PARK (never misclassified as split-album/distinct-disc).
        self.assertIn(self._route(3807, 3857), ('DUPPLAN_DROP', 'DUPPLAN_DONE'))

    def test_no_drop_touches_commingled_files(self):
        # every DROP must be backed by files disjoint from the keeper on disk
        for e in self.results:
            if e['route'] == 'DUPPLAN_DROP':
                self.assertIn('files disjoint', e['reason'])


class TestExecuteWiring(unittest.TestCase):
    """Execute path is wired but must be safe-by-construction: the CFG_OVERLAY uses
    the valid 'skip' value, and UPGRADE never auto-swaps (parks to review)."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix='recon-test-')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cfg_overlay_uses_skip_not_no(self):
        from pathlib import Path
        p = R._write_cfg_overlay(Path(self.tmp))
        txt = open(p).read()
        self.assertIn('duplicate_action: skip', txt)
        self.assertNotIn('duplicate_action: no', txt)
        self.assertIn('move: yes', txt)

    def test_dispatch_has_core_routes(self):
        self.assertEqual(set(R._DISPATCH), {'NEW', 'DUPLICATE', 'UPGRADE'})

    def test_graveyard_move_handles_directory(self):
        # do_duplicate/do_park move whole FOLDERS — graveyard_move must not try to
        # sha256 a directory (the bug that would crash on the first duplicate).
        from pathlib import Path
        src = Path(self.tmp) / 'srcfolder'
        src.mkdir()
        (src / 'a.flac').write_bytes(b'x' * 16)
        (src / 'b.flac').write_bytes(b'y' * 16)
        dst = Path(self.tmp) / 'grave' / 'srcfolder'
        R.graveyard_move(os.fsencode(str(src)), os.fsencode(str(dst)), R.Journal(Path(self.tmp) / 'j.jsonl'))
        self.assertFalse(src.exists())
        self.assertTrue((dst / 'a.flac').exists() and (dst / 'b.flac').exists())

    def test_upgrade_parks_not_swaps(self):
        # do_upgrade must NOT perform the irreversible swap unattended — it parks.
        from pathlib import Path
        rd = Path(self.tmp)
        ctx = R.RunContext('/nonexistent.db', rd, R.Journal(rd / 'j.jsonl'), read_only_source=True)
        entry = {'candidate': {'path': '/x/some album'}, 'identity': {'key': 'ch:abc'},
                 'route_reason': 'in-library; strictly better'}
        res = R.do_upgrade(entry, ctx)
        self.assertEqual(res['route'], 'PARK')
        self.assertFalse(res.get('moved'))  # read_only_source -> no move either


if __name__ == '__main__':
    unittest.main()
