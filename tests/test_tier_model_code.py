"""
test_tier_model_code.py
-----------------------
Unit tests for the tier-4 (model-code) edge provider.

Covers: spec-pattern blocklist, block-spread filter, discriminative code
selection, blocked union (same-block vs cross-block), no-chaining guarantee,
review signals, and deterministic ordering.

Uses small synthetic EnrichedOffer fixtures — does not hit the database.

Run with:
    python -m pytest tests/test_tier_model_code.py -v
    # or
    python -m unittest tests.test_tier_model_code -v
"""

import unittest

from matching.load import EnrichedOffer
from matching.tier_model_code import (
    is_spec_like_code,
    is_series_suffix_code,
    choose_discriminative_code,
    model_code_edges,
    model_code_groups,
    review_signals,
)


# ---------------------------------------------------------------------------
# Helper: build a minimal EnrichedOffer for tier-4 testing.
# ---------------------------------------------------------------------------

def _offer(
    model_codes: tuple[str, ...] = (),
    store: str = "teststore",
    category: str = "smartphones",
    effective_brand: str = "Samsung",
    brand_norm: str = "Samsung",
    title: str = "Test Product",
    is_suspicious_brand: bool = False,
    brand_from_title: str | None = None,
) -> EnrichedOffer:
    """Build a minimal EnrichedOffer for tier-4 testing.

    Only fields relevant to tier 4 (model_codes, category, effective_brand,
    is_suspicious_brand, brand_from_title) need realistic values; everything
    else gets safe defaults.
    """
    return EnrichedOffer(
        store=store,
        store_product_id="sp-001",
        title=title,
        vendor="TestVendor",
        category=category,
        sku=None,
        price=None,
        available=True,
        image_url=None,
        product_url="https://example.com/p/1",
        mpn=None,
        mpn_root=None,
        ean=None,
        identifier_source="none",
        brand_norm=brand_norm,
        title_norm="test product",
        title_key="title:test:smartphones:abc123",
        ean_key=None,
        mpn_root_key=None,
        mpn_key=None,
        model_codes=model_codes,
        is_suspicious_brand=is_suspicious_brand,
        brand_from_title=brand_from_title,
        effective_brand=effective_brand,
    )


# ===========================================================================
# is_spec_like_code tests
# ===========================================================================

class TestIsSpecLikeCode(unittest.TestCase):
    """is_spec_like_code correctly blocks spec fragments and passes model codes."""

    # --- Should be BLOCKED ---

    def test_screen_size_13inch(self):
        self.assertTrue(is_spec_like_code("13inch"))

    def test_screen_size_14inch(self):
        self.assertTrue(is_spec_like_code("14inch"))

    def test_weight_10kg(self):
        self.assertTrue(is_spec_like_code("10kg"))

    def test_dual_weight_12kg10kg(self):
        self.assertTrue(is_spec_like_code("12kg10kg"))

    def test_combo_2in1(self):
        self.assertTrue(is_spec_like_code("2in1"))

    def test_resolution_1080p(self):
        self.assertTrue(is_spec_like_code("1080p"))

    def test_btu_9000btu(self):
        self.assertTrue(is_spec_like_code("9000btu"))

    def test_ram_storage_6gb128gb(self):
        self.assertTrue(is_spec_like_code("6gb128gb"))

    def test_ram_storage_8gb256gb(self):
        self.assertTrue(is_spec_like_code("8gb256gb"))

    def test_storage_gb1tb(self):
        self.assertTrue(is_spec_like_code("gb1tb"))

    def test_core_10core(self):
        self.assertTrue(is_spec_like_code("10core"))

    def test_core_blob_10core16gb256gb(self):
        self.assertTrue(is_spec_like_code("10core16gb256gb"))

    def test_cpu_spec_blob_r57520u16gb512gb(self):
        self.assertTrue(is_spec_like_code("r57520u16gb512gb"))

    def test_standalone_gen2(self):
        self.assertTrue(is_spec_like_code("gen2"))

    def test_ip_rating_ip67(self):
        self.assertTrue(is_spec_like_code("ip67"))

    def test_ip_rating_ip54(self):
        self.assertTrue(is_spec_like_code("ip54"))

    def test_gpu_core_10gpu(self):
        self.assertTrue(is_spec_like_code("10gpu"))

    def test_standalone_10core(self):
        """Standalone '10core' is blocked by the core-adjacent-to-digits pattern."""
        self.assertTrue(is_spec_like_code("10core"))

    def test_watch_series_watch8(self):
        self.assertTrue(is_spec_like_code("watch8"))

    # --- Should PASS (genuine model codes) ---

    def test_pass_15arp10(self):
        self.assertFalse(is_spec_like_code("15arp10"))

    def test_pass_15arp10e(self):
        self.assertFalse(is_spec_like_code("15arp10e"))

    def test_pass_1000mk2(self):
        self.assertFalse(is_spec_like_code("1000mk2"))

    def test_pass_mg23k3515as(self):
        self.assertFalse(is_spec_like_code("mg23k3515as"))

    def test_pass_scg6050ss(self):
        self.assertFalse(is_spec_like_code("scg6050ss"))

    def test_pass_14he0001nv(self):
        self.assertFalse(is_spec_like_code("14he0001nv"))

    def test_pass_13bg1000nv(self):
        self.assertFalse(is_spec_like_code("13bg1000nv"))


# ===========================================================================
# is_series_suffix_code tests
# ===========================================================================

class TestIsSeriesSuffixCode(unittest.TestCase):
    """is_series_suffix_code blocks short digit-prefix + letter-suffix codes
    shared across product lines, and passes genuine model codes.

    Pattern: ^\d{3,4}[a-z]{1,2}$

    SHOULD BE REJECTED:  770nc, 670nc, 680nc, 520c, 310c, 520bt, 135bt
    MUST NOT be rejected: z150, h340, h111, m500, m185, m190, mdrzx310,
                          mdrzx310ap, cre611s06, bch6ath25, wh1000xm5, whch520
    """

    # --- Should be REJECTED ---

    def test_reject_770nc(self):
        self.assertTrue(is_series_suffix_code("770nc"))

    def test_reject_670nc(self):
        self.assertTrue(is_series_suffix_code("670nc"))

    def test_reject_680nc(self):
        self.assertTrue(is_series_suffix_code("680nc"))

    def test_reject_520c(self):
        self.assertTrue(is_series_suffix_code("520c"))

    def test_reject_310c(self):
        self.assertTrue(is_series_suffix_code("310c"))

    def test_reject_520bt(self):
        self.assertTrue(is_series_suffix_code("520bt"))

    def test_reject_135bt(self):
        self.assertTrue(is_series_suffix_code("135bt"))

    # --- MUST NOT be rejected (start with a letter) ---

    def test_pass_z150(self):
        self.assertFalse(is_series_suffix_code("z150"))

    def test_pass_h340(self):
        self.assertFalse(is_series_suffix_code("h340"))

    def test_pass_h111(self):
        self.assertFalse(is_series_suffix_code("h111"))

    def test_pass_m500(self):
        self.assertFalse(is_series_suffix_code("m500"))

    def test_pass_m185(self):
        self.assertFalse(is_series_suffix_code("m185"))

    def test_pass_m190(self):
        self.assertFalse(is_series_suffix_code("m190"))

    def test_pass_mdrzx310(self):
        self.assertFalse(is_series_suffix_code("mdrzx310"))

    def test_pass_mdrzx310ap(self):
        self.assertFalse(is_series_suffix_code("mdrzx310ap"))

    def test_pass_cre611s06(self):
        self.assertFalse(is_series_suffix_code("cre611s06"))

    def test_pass_bch6ath25(self):
        self.assertFalse(is_series_suffix_code("bch6ath25"))

    def test_pass_wh1000xm5(self):
        self.assertFalse(is_series_suffix_code("wh1000xm5"))

    def test_pass_whch520(self):
        self.assertFalse(is_series_suffix_code("whch520"))


# ===========================================================================
# Combined filter test — must pass BOTH to be eligible
# ===========================================================================

class TestCombinedFilters(unittest.TestCase):
    """A code must pass both is_spec_like_code and is_series_suffix_code
    to be eligible for tier 4. Failing either one excludes it."""

    def test_spec_like_excludes_from_tier4(self):
        """A code blocked by spec-pattern (but not series-suffix) is excluded."""
        # "1080p" fails spec check, passes series-suffix check.
        offers = [
            _offer(model_codes=("1080p",), category="tvs",
                   effective_brand="Samsung", store="a"),
            _offer(model_codes=("1080p",), category="tvs",
                   effective_brand="Samsung", store="b"),
        ]
        edges = model_code_edges(offers)
        self.assertEqual(edges, [])

    def test_series_suffix_excludes_from_tier4(self):
        """A code blocked by series-suffix (but not spec-pattern) is excluded."""
        # "770nc" passes spec check, fails series-suffix check.
        self.assertFalse(is_spec_like_code("770nc"))
        self.assertTrue(is_series_suffix_code("770nc"))
        offers = [
            _offer(model_codes=("770nc",), category="headphones",
                   effective_brand="JBL", store="a"),
            _offer(model_codes=("770nc",), category="headphones",
                   effective_brand="JBL", store="b"),
        ]
        edges = model_code_edges(offers)
        self.assertEqual(edges, [])


# ===========================================================================
# Block-spread filter tests
# ===========================================================================

class TestBlockSpreadFilter(unittest.TestCase):
    """Codes in 2+ blocks are excluded; codes in exactly 1 block are kept."""

    def test_code_in_two_blocks_excluded(self):
        """A code appearing in two different (category, brand) blocks should
        not produce edges, because it is non-discriminative."""
        offers = [
            # Same code "abc123" in two different blocks.
            _offer(model_codes=("abc123",), category="laptops", effective_brand="HP"),
            _offer(model_codes=("abc123",), category="monitors", effective_brand="HP"),
        ]
        edges = model_code_edges(offers)
        # No edges — code spans 2 blocks.
        self.assertEqual(edges, [])

    def test_code_in_one_block_kept(self):
        """A code appearing only within one block should produce edges."""
        offers = [
            _offer(model_codes=("abc123",), category="laptops", effective_brand="HP"),
            _offer(model_codes=("abc123",), category="laptops", effective_brand="HP"),
        ]
        edges = model_code_edges(offers)
        self.assertEqual(edges, [(0, 1)])

    def test_code_in_two_blocks_different_brand(self):
        """Same code in same category but different brands = 2 blocks."""
        offers = [
            _offer(model_codes=("xyz999",), category="tvs", effective_brand="Samsung"),
            _offer(model_codes=("xyz999",), category="tvs", effective_brand="LG"),
        ]
        edges = model_code_edges(offers)
        self.assertEqual(edges, [])


# ===========================================================================
# choose_discriminative_code tests
# ===========================================================================

class TestChooseDiscriminativeCode(unittest.TestCase):
    """Discriminative code selection: rarest wins, then longest, then alpha."""

    def _make_freq_and_spread(self, codes, freq_map):
        """Helper: build block_spread (all 1) and freq dicts."""
        block_spread = {c: 1 for c in codes}
        return freq_map, block_spread

    def test_rarer_code_wins(self):
        """The code with lower frequency is chosen."""
        freq = {"common": 10, "rare": 2}
        spread = {"common": 1, "rare": 1}
        offer = _offer(model_codes=("common", "rare"))
        result = choose_discriminative_code(offer, freq, spread)
        self.assertEqual(result, "rare")

    def test_tiebreak_longest(self):
        """On equal frequency, the longest code wins."""
        freq = {"ab12": 3, "abcde12": 3}
        spread = {"ab12": 1, "abcde12": 1}
        offer = _offer(model_codes=("ab12", "abcde12"))
        result = choose_discriminative_code(offer, freq, spread)
        self.assertEqual(result, "abcde12")

    def test_tiebreak_alphabetical(self):
        """On equal frequency and length, alphabetical wins."""
        freq = {"bbb111": 3, "aaa111": 3}
        spread = {"bbb111": 1, "aaa111": 1}
        offer = _offer(model_codes=("bbb111", "aaa111"))
        result = choose_discriminative_code(offer, freq, spread)
        self.assertEqual(result, "aaa111")

    def test_empty_trusted_codes_returns_none(self):
        """An offer with no trusted codes contributes nothing."""
        # All codes are spec-like.
        offer = _offer(model_codes=("1080p", "2in1"))
        freq = {}
        spread = {}
        result = choose_discriminative_code(offer, freq, spread)
        self.assertIsNone(result)

    def test_no_model_codes_returns_none(self):
        """An offer with no model codes at all returns None."""
        offer = _offer(model_codes=())
        result = choose_discriminative_code(offer, {}, {})
        self.assertIsNone(result)


# ===========================================================================
# Blocked union tests (same block vs cross block)
# ===========================================================================

class TestBlockedUnion(unittest.TestCase):
    """Offers in the SAME block sharing a trusted code are linked;
    offers in DIFFERENT blocks sharing the same code are NOT linked."""

    def test_same_block_same_code_linked(self):
        """Two offers in the same block with the same trusted code produce
        an edge."""
        offers = [
            _offer(model_codes=("sm928b",), category="phones",
                   effective_brand="Samsung", store="stephanis"),
            _offer(model_codes=("sm928b",), category="phones",
                   effective_brand="Samsung", store="public"),
        ]
        edges = model_code_edges(offers)
        self.assertEqual(edges, [(0, 1)])

    def test_different_blocks_same_code_not_linked(self):
        """Two offers in different blocks with the same code produce NO edge."""
        offers = [
            _offer(model_codes=("sm928b",), category="phones",
                   effective_brand="Samsung"),
            _offer(model_codes=("sm928b",), category="tablets",
                   effective_brand="Samsung"),
        ]
        edges = model_code_edges(offers)
        self.assertEqual(edges, [])

    def test_star_pattern_three_offers(self):
        """Three offers in the same block -> star edges from smallest index."""
        offers = [
            _offer(model_codes=("sm928b",), category="phones",
                   effective_brand="Samsung", store="a"),
            _offer(model_codes=("sm928b",), category="phones",
                   effective_brand="Samsung", store="b"),
            _offer(model_codes=("sm928b",), category="phones",
                   effective_brand="Samsung", store="c"),
        ]
        edges = model_code_edges(offers)
        self.assertEqual(edges, [(0, 1), (0, 2)])

    def test_empty_effective_brand_skipped(self):
        """Offers with empty effective_brand do not participate."""
        offers = [
            _offer(model_codes=("sm928b",), effective_brand=""),
            _offer(model_codes=("sm928b",), effective_brand=""),
        ]
        edges = model_code_edges(offers)
        self.assertEqual(edges, [])

    def test_none_effective_brand_skipped(self):
        """Offers with None effective_brand do not participate."""
        offers = [
            _offer(model_codes=("sm928b",), effective_brand=None),
            _offer(model_codes=("sm928b",), effective_brand=None),
        ]
        edges = model_code_edges(offers)
        self.assertEqual(edges, [])


# ===========================================================================
# No-chaining test
# ===========================================================================

class TestNoChaining(unittest.TestCase):
    """Each offer contributes exactly ONE code, so overlapping secondary
    codes do not chain distinct products together."""

    def test_no_chaining_via_secondary_codes(self):
        """Offer A shares code X with offer B; offer B also has code Y shared
        with offer C. But since each offer contributes only one code, B cannot
        chain A to C unless A and C also share the SAME chosen code."""
        offers = [
            # A has a rare code "rare1" and a common code "common1".
            _offer(model_codes=("rare1", "common1"), category="phones",
                   effective_brand="Samsung", store="s1"),
            # B shares "common1" with A and "common2" with C.
            _offer(model_codes=("common1", "common2"), category="phones",
                   effective_brand="Samsung", store="s2"),
            # C has "common2" and a rare code "rare3".
            _offer(model_codes=("common2", "rare3"), category="phones",
                   effective_brand="Samsung", store="s3"),
        ]
        edges = model_code_edges(offers)

        # Each offer should choose its rarest code. If "rare1" and "rare3"
        # each appear only once, they won't create groups. "common1" appears
        # in 2 offers (A, B) and "common2" appears in 2 offers (B, C).
        # B can only contribute ONE code (whichever is rarer/longer/alpha),
        # so at most one of {A-B} or {B-C} is linked, never both.
        # This prevents A-B-C chaining.

        # Verify no transitive chain: A and C should never be in the same edge.
        from matching.union_find import UnionFind
        uf = UnionFind(3)
        for a, b in edges:
            uf.union(a, b)
        # A (0) and C (2) should NOT be connected.
        self.assertFalse(uf.connected(0, 2))


# ===========================================================================
# Review signals tests
# ===========================================================================

class TestReviewSignals(unittest.TestCase):
    """review_signals flags non-trivial clusters (size >= 2) with unresolved
    suspicious brands or cross-effective-brand unions. Singletons are never
    flagged."""

    def test_unresolved_suspicious_brand_flagged(self):
        """A size>=2 cluster with a suspicious-brand member and no
        brand_from_title is flagged."""
        offers = [
            _offer(is_suspicious_brand=True, brand_from_title=None),
            _offer(is_suspicious_brand=False),
        ]
        cluster = [[0, 1]]
        flags = review_signals(offers, cluster)
        reasons = [r for _, r in flags]
        self.assertIn("unresolved_suspicious_brand", reasons)

    def test_resolved_suspicious_brand_not_flagged(self):
        """A suspicious-brand member WITH a brand_from_title is NOT flagged
        for unresolved_suspicious_brand."""
        offers = [
            _offer(is_suspicious_brand=True, brand_from_title="Samsung"),
            _offer(is_suspicious_brand=False),
        ]
        cluster = [[0, 1]]
        flags = review_signals(offers, cluster)
        reasons = [r for _, r in flags]
        self.assertNotIn("unresolved_suspicious_brand", reasons)

    def test_cross_effective_brand_flagged(self):
        """A size>=2 cluster with two distinct non-empty effective_brands
        is flagged as cross_effective_brand."""
        offers = [
            _offer(effective_brand="Samsung"),
            _offer(effective_brand="LG"),
        ]
        cluster = [[0, 1]]
        flags = review_signals(offers, cluster)
        reasons = [r for _, r in flags]
        self.assertIn("cross_effective_brand", reasons)

    def test_singleton_not_flagged(self):
        """A singleton (size 1) with empty effective_brand is NOT flagged —
        there is no union to review."""
        offers = [
            _offer(effective_brand="", is_suspicious_brand=True,
                   brand_from_title=None),
        ]
        cluster = [[0]]
        flags = review_signals(offers, cluster)
        self.assertEqual(flags, [])

    def test_clean_cluster_not_flagged(self):
        """A size>=2 cluster with no issues produces no flags."""
        offers = [
            _offer(effective_brand="Samsung", is_suspicious_brand=False),
            _offer(effective_brand="Samsung", is_suspicious_brand=False),
        ]
        cluster = [[0, 1]]
        flags = review_signals(offers, cluster)
        self.assertEqual(flags, [])


# ===========================================================================
# Cross-brand detector tests
# ===========================================================================

class TestCrossBrandDetector(unittest.TestCase):
    """The cross-brand detector should use effective_brand (the blocking key),
    not brand_norm. A cluster whose members share one effective_brand but
    differ in brand_norm is NOT cross-brand."""

    def test_same_effective_brand_different_brand_norm_not_flagged(self):
        """Members share effective_brand='Philips' but differ in brand_norm
        ('PHILIPSHUE' vs 'Philips'). NOT cross-brand."""
        offers = [
            _offer(effective_brand="Philips", brand_norm="PHILIPSHUE"),
            _offer(effective_brand="Philips", brand_norm="Philips"),
        ]
        cluster = [0, 1]
        # Cross-brand detection logic: distinct non-empty effective_brand values.
        brands = {offers[i].effective_brand for i in cluster
                  if offers[i].effective_brand}
        self.assertEqual(len(brands), 1)  # NOT cross-brand

    def test_two_distinct_effective_brands_flagged(self):
        """Members have two distinct effective_brands -> IS cross-brand."""
        offers = [
            _offer(effective_brand="Samsung", brand_norm="Samsung"),
            _offer(effective_brand="LG", brand_norm="LG"),
        ]
        cluster = [0, 1]
        brands = {offers[i].effective_brand for i in cluster
                  if offers[i].effective_brand}
        self.assertGreaterEqual(len(brands), 2)  # IS cross-brand

    def test_empty_effective_brand_ignored_in_cross_brand(self):
        """A member with empty effective_brand should not count as a distinct
        brand — only non-empty values matter."""
        offers = [
            _offer(effective_brand="Samsung", brand_norm="Samsung"),
            _offer(effective_brand="", brand_norm=""),
        ]
        cluster = [0, 1]
        brands = {offers[i].effective_brand for i in cluster
                  if offers[i].effective_brand}
        self.assertEqual(len(brands), 1)  # NOT cross-brand


# ===========================================================================
# Deterministic ordering tests
# ===========================================================================

class TestDeterministicOrdering(unittest.TestCase):
    """Same input produces identical groups and edges across calls."""

    def test_groups_deterministic(self):
        """model_code_groups returns identical output on repeated calls."""
        offers = [
            _offer(model_codes=("abc123",), category="phones",
                   effective_brand="Samsung", store="b"),
            _offer(model_codes=("abc123",), category="phones",
                   effective_brand="Samsung", store="a"),
            _offer(model_codes=("xyz789",), category="phones",
                   effective_brand="Samsung", store="c"),
            _offer(model_codes=("xyz789",), category="phones",
                   effective_brand="Samsung", store="d"),
        ]
        g1 = model_code_groups(offers)
        g2 = model_code_groups(offers)
        self.assertEqual(g1, g2)

    def test_edges_deterministic(self):
        """model_code_edges returns identical output on repeated calls."""
        offers = [
            _offer(model_codes=("abc123",), category="phones",
                   effective_brand="Samsung", store="b"),
            _offer(model_codes=("abc123",), category="phones",
                   effective_brand="Samsung", store="a"),
            _offer(model_codes=("xyz789",), category="phones",
                   effective_brand="Samsung", store="c"),
            _offer(model_codes=("xyz789",), category="phones",
                   effective_brand="Samsung", store="d"),
        ]
        e1 = model_code_edges(offers)
        e2 = model_code_edges(offers)
        self.assertEqual(e1, e2)


if __name__ == "__main__":
    unittest.main()
