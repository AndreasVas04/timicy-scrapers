"""
test_tier_title_key.py
----------------------
Unit tests for the tier-5 (title_key) edge provider.

Covers: same-group merging, cross-brand isolation, cross-category isolation,
empty effective_brand skip, empty title_key skip, star-pattern edges,
and deterministic ordering.

Uses small synthetic EnrichedOffer fixtures — no DB, no network.

Run with:
    python -m pytest tests/test_tier_title_key.py -v
"""

import unittest

from matching.load import EnrichedOffer
from matching.tier_title_key import title_key_edges, title_key_groups


# ---------------------------------------------------------------------------
# Helper: build a minimal EnrichedOffer for tier-5 testing.
# ---------------------------------------------------------------------------

def _offer(
    title_key: str | None = "title:samsung:smartphones:abc123",
    category: str = "smartphones",
    effective_brand: str = "Samsung",
    store: str = "teststore",
    store_product_id: str = "sp-001",
) -> EnrichedOffer:
    """Build a minimal EnrichedOffer for tier-5 (title_key) testing.

    Only fields relevant to tier 5 (title_key, category, effective_brand)
    need realistic values; everything else gets safe defaults.
    """
    return EnrichedOffer(
        store=store,
        store_product_id=store_product_id,
        title="Test Product",
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
        brand_norm="Samsung",
        title_norm="test product",
        title_key=title_key,
        ean_key=None,
        mpn_root_key=None,
        mpn_key=None,
        model_codes=(),
        is_suspicious_brand=False,
        brand_from_title=None,
        effective_brand=effective_brand,
    )


# ===========================================================================
# title_key_edges tests
# ===========================================================================

class TestTitleKeyEdges(unittest.TestCase):
    """Test the edge-provider interface of tier 5."""

    def test_two_offers_same_group_one_edge(self):
        """Two offers sharing (category, effective_brand, title_key) -> one edge."""
        offers = [
            _offer(title_key="title:samsung:smartphones:aaa", store="store_a"),
            _offer(title_key="title:samsung:smartphones:aaa", store="store_b"),
        ]
        edges = title_key_edges(offers)
        self.assertEqual(edges, [(0, 1)])

    def test_different_effective_brand_no_edge(self):
        """Same title_key but different effective_brand -> no edge."""
        offers = [
            _offer(title_key="title:x:smartphones:aaa", effective_brand="Samsung"),
            _offer(title_key="title:x:smartphones:aaa", effective_brand="Apple"),
        ]
        edges = title_key_edges(offers)
        self.assertEqual(edges, [])

    def test_different_category_no_edge(self):
        """Same title_key but different category -> no edge."""
        offers = [
            _offer(title_key="title:samsung:smartphones:aaa", category="smartphones"),
            _offer(title_key="title:samsung:smartphones:aaa", category="tablets"),
        ]
        edges = title_key_edges(offers)
        self.assertEqual(edges, [])

    def test_empty_effective_brand_skipped(self):
        """Offers with empty/None effective_brand produce no edges."""
        offers = [
            _offer(title_key="title:x:smartphones:aaa", effective_brand=""),
            _offer(title_key="title:x:smartphones:aaa", effective_brand=""),
        ]
        edges = title_key_edges(offers)
        self.assertEqual(edges, [])

        # Also test None.
        offers_none = [
            _offer(title_key="title:x:smartphones:aaa", effective_brand=None),
            _offer(title_key="title:x:smartphones:aaa", effective_brand="Samsung"),
        ]
        edges_none = title_key_edges(offers_none)
        self.assertEqual(edges_none, [])

    def test_empty_title_key_skipped(self):
        """Offers with empty/None title_key produce no edges."""
        offers = [
            _offer(title_key=None, effective_brand="Samsung"),
            _offer(title_key=None, effective_brand="Samsung"),
        ]
        edges = title_key_edges(offers)
        self.assertEqual(edges, [])

        # Also test empty string.
        offers_empty = [
            _offer(title_key="", effective_brand="Samsung"),
            _offer(title_key="", effective_brand="Samsung"),
        ]
        edges_empty = title_key_edges(offers_empty)
        self.assertEqual(edges_empty, [])

    def test_star_pattern_three_offers(self):
        """Three offers in same group -> two edges from anchor (star), not chain."""
        offers = [
            _offer(title_key="title:samsung:smartphones:aaa", store="s1",
                   store_product_id="sp-1"),
            _offer(title_key="title:samsung:smartphones:aaa", store="s2",
                   store_product_id="sp-2"),
            _offer(title_key="title:samsung:smartphones:aaa", store="s3",
                   store_product_id="sp-3"),
        ]
        edges = title_key_edges(offers)
        # Star from index 0 to 1 and 0 to 2.
        self.assertEqual(edges, [(0, 1), (0, 2)])

    def test_deterministic_ordering(self):
        """Same offers in different input order -> identical edge list."""
        offer_a = _offer(title_key="title:samsung:smartphones:aaa", store="s_a",
                         store_product_id="sp-a")
        offer_b = _offer(title_key="title:samsung:smartphones:aaa", store="s_b",
                         store_product_id="sp-b")
        offer_c = _offer(title_key="title:samsung:smartphones:bbb", store="s_c",
                         store_product_id="sp-c",
                         effective_brand="Apple", category="tablets")

        # Order 1: [a, b, c]
        edges_1 = title_key_edges([offer_a, offer_b, offer_c])
        # Order 2: [b, a, c] — indices change but the EDGE LIST must be
        # deterministic relative to input positions.
        edges_2 = title_key_edges([offer_b, offer_a, offer_c])

        # In order 1, a=0, b=1 -> edge (0,1).
        self.assertEqual(edges_1, [(0, 1)])
        # In order 2, b=0, a=1 -> edge (0,1) — same structure.
        self.assertEqual(edges_2, [(0, 1)])

    def test_singleton_group_no_edge(self):
        """A group of size 1 emits no edges."""
        offers = [
            _offer(title_key="title:samsung:smartphones:aaa"),
        ]
        edges = title_key_edges(offers)
        self.assertEqual(edges, [])


# ===========================================================================
# title_key_groups tests
# ===========================================================================

class TestTitleKeyGroups(unittest.TestCase):
    """Test the grouping function."""

    def test_groups_sorted_keys(self):
        """Groups dict has sorted keys for deterministic iteration."""
        offers = [
            _offer(title_key="title:z:smartphones:zzz"),
            _offer(title_key="title:a:smartphones:aaa"),
        ]
        groups = title_key_groups(offers)
        keys = list(groups.keys())
        self.assertEqual(keys, sorted(keys))

    def test_groups_sorted_members(self):
        """Member indices within each group are sorted."""
        offers = [
            _offer(title_key="title:samsung:smartphones:aaa", store="s1"),
            _offer(title_key="title:samsung:smartphones:aaa", store="s2"),
        ]
        groups = title_key_groups(offers)
        for members in groups.values():
            self.assertEqual(members, sorted(members))


if __name__ == "__main__":
    unittest.main()
