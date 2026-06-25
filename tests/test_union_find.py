"""
test_union_find.py
------------------
Unit tests for the generic UnionFind data structure and the tier-2
(mpn_root) edge provider.

Uses small synthetic fixtures — does not hit the database.

Run with:
    python -m pytest tests/test_union_find.py -v
    # or
    python -m unittest tests.test_union_find -v
"""

import unittest

from matching.union_find import UnionFind
from matching.load import EnrichedOffer
from matching.tier_mpn_root import mpn_root_edges, mpn_root_groups


# ---------------------------------------------------------------------------
# Helper: build a minimal EnrichedOffer for testing.
# Only the fields relevant to tier-2 (mpn_root_key, store, category,
# brand_norm) need realistic values; everything else gets safe defaults.
# ---------------------------------------------------------------------------

def _offer(
    mpn_root_key: str | None = None,
    store: str = "teststore",
    category: str = "smartphones",
    brand_norm: str = "TestBrand",
    title: str = "Test Product",
    vendor: str | None = "TestVendor",
) -> EnrichedOffer:
    """Build a minimal EnrichedOffer with only the fields tier-2 cares about."""
    return EnrichedOffer(
        store=store,
        store_product_id="sp-001",
        title=title,
        vendor=vendor,
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
        title_key="title:testbrand:smartphones:abc123abc123",
        ean_key=None,
        mpn_root_key=mpn_root_key,
        mpn_key=None,
        model_codes=(),
        is_suspicious_brand=False,
        brand_from_title=None,
        effective_brand=brand_norm,
    )


# ===========================================================================
# UnionFind tests
# ===========================================================================

class TestUnionFindBasics(unittest.TestCase):
    """Basic union + find + connected operations."""

    def test_initial_singletons(self):
        """Each element starts in its own set."""
        uf = UnionFind(5)
        for i in range(5):
            self.assertEqual(uf.find(i), i)
        # No two distinct elements are connected.
        self.assertFalse(uf.connected(0, 1))

    def test_union_and_connected(self):
        """After union(a, b), a and b are connected."""
        uf = UnionFind(5)
        uf.union(1, 3)
        self.assertTrue(uf.connected(1, 3))
        self.assertFalse(uf.connected(0, 1))

    def test_transitive_union(self):
        """Union is transitive: union(0,1) + union(1,2) -> 0,1,2 connected."""
        uf = UnionFind(5)
        uf.union(0, 1)
        uf.union(1, 2)
        self.assertTrue(uf.connected(0, 2))

    def test_self_union_noop(self):
        """Union of an element with itself is a no-op."""
        uf = UnionFind(3)
        uf.union(1, 1)
        self.assertEqual(uf.find(1), 1)


class TestUnionFindPathCompression(unittest.TestCase):
    """Path compression produces stable, consistent roots."""

    def test_repeated_find_stable(self):
        """Repeated find() calls return the same root without corruption."""
        uf = UnionFind(10)
        uf.union(0, 1)
        uf.union(2, 3)
        uf.union(0, 3)
        # All four should share the same root.
        root = uf.find(0)
        for i in (1, 2, 3):
            self.assertEqual(uf.find(i), root)
        # Call find() again to confirm stability after path compression.
        for _ in range(3):
            for i in (0, 1, 2, 3):
                self.assertEqual(uf.find(i), root)

    def test_find_does_not_corrupt_other_sets(self):
        """Path compression on one set does not affect disjoint sets."""
        uf = UnionFind(6)
        uf.union(0, 1)
        uf.union(2, 3)
        # 4 and 5 are disjoint singletons.
        _ = uf.find(0)
        _ = uf.find(2)
        self.assertFalse(uf.connected(0, 2))
        self.assertEqual(uf.find(4), 4)
        self.assertEqual(uf.find(5), 5)


class TestUnionFindDeterministicTieBreak(unittest.TestCase):
    """Deterministic tie-breaking: smaller root index wins on equal size."""

    def test_equal_size_smaller_root_wins(self):
        """When two singletons merge, the smaller index becomes root."""
        uf = UnionFind(4)
        uf.union(3, 1)
        # Both are size 1 before union; index 1 < 3, so 1 is root.
        self.assertEqual(uf.find(3), 1)

    def test_larger_tree_wins(self):
        """The larger tree's root is preserved regardless of index values."""
        uf = UnionFind(5)
        uf.union(3, 4)  # size-1 tie -> root 3
        uf.union(0, 3)  # size-1 vs size-2 -> root 3 (larger tree)
        self.assertEqual(uf.find(0), 3)


class TestUnionFindGroups(unittest.TestCase):
    """groups() returns deterministic, correctly-partitioned clusters."""

    def test_all_singletons(self):
        """With no unions, groups() returns n singleton lists."""
        uf = UnionFind(3)
        g = uf.groups()
        self.assertEqual(g, [[0], [1], [2]])

    def test_two_clusters(self):
        """Two merged pairs produce two sorted clusters."""
        uf = UnionFind(5)
        uf.union(0, 2)
        uf.union(3, 4)
        g = uf.groups()
        # Clusters: {0,2}, {1}, {3,4} — sorted by smallest member.
        self.assertEqual(g, [[0, 2], [1], [3, 4]])

    def test_single_large_cluster(self):
        """Merging all elements produces one cluster with all indices."""
        uf = UnionFind(4)
        uf.union(0, 1)
        uf.union(2, 3)
        uf.union(0, 3)
        g = uf.groups()
        self.assertEqual(g, [[0, 1, 2, 3]])

    def test_groups_deterministic_across_calls(self):
        """Calling groups() twice gives identical results."""
        uf = UnionFind(6)
        uf.union(5, 0)
        uf.union(3, 4)
        g1 = uf.groups()
        g2 = uf.groups()
        self.assertEqual(g1, g2)


# ===========================================================================
# Tier-2 (mpn_root) tests
# ===========================================================================

class TestMpnRootGroups(unittest.TestCase):
    """mpn_root_groups correctly groups offers by mpn_root_key."""

    def test_basic_grouping(self):
        """Offers with the same mpn_root_key are grouped together."""
        offers = [
            _offer(mpn_root_key="mpnroot:samsung:SM-S928"),   # 0
            _offer(mpn_root_key="mpnroot:samsung:SM-S928"),   # 1
            _offer(mpn_root_key="mpnroot:apple:IPHONE16"),    # 2
        ]
        groups = mpn_root_groups(offers)
        self.assertEqual(groups["mpnroot:samsung:SM-S928"], [0, 1])
        self.assertEqual(groups["mpnroot:apple:IPHONE16"], [2])

    def test_none_key_skipped(self):
        """Offers with None mpn_root_key produce no groups."""
        offers = [
            _offer(mpn_root_key=None),      # 0
            _offer(mpn_root_key=""),         # 1 (empty string also skipped)
            _offer(mpn_root_key="mpnroot:samsung:SM-S928"),  # 2
        ]
        groups = mpn_root_groups(offers)
        # Only one group should exist.
        self.assertEqual(len(groups), 1)
        self.assertIn("mpnroot:samsung:SM-S928", groups)
        self.assertEqual(groups["mpnroot:samsung:SM-S928"], [2])

    def test_empty_offers(self):
        """An empty offer list produces no groups."""
        self.assertEqual(mpn_root_groups([]), {})

    def test_deterministic_output(self):
        """Same input produces identical group output on repeated calls."""
        offers = [
            _offer(mpn_root_key="mpnroot:b:Z"),
            _offer(mpn_root_key="mpnroot:a:A"),
            _offer(mpn_root_key="mpnroot:b:Z"),
        ]
        g1 = mpn_root_groups(offers)
        g2 = mpn_root_groups(offers)
        self.assertEqual(g1, g2)
        # Keys should be sorted.
        self.assertEqual(list(g1.keys()), sorted(g1.keys()))


class TestMpnRootEdges(unittest.TestCase):
    """mpn_root_edges returns correct star-pattern edges."""

    def test_group_of_two(self):
        """A group of 2 produces one edge."""
        offers = [
            _offer(mpn_root_key="mpnroot:samsung:SM-S928"),
            _offer(mpn_root_key="mpnroot:samsung:SM-S928"),
        ]
        edges = mpn_root_edges(offers)
        self.assertEqual(edges, [(0, 1)])

    def test_group_of_three_star(self):
        """A group of 3 produces star edges from the smallest index."""
        offers = [
            _offer(mpn_root_key="mpnroot:samsung:SM-S928"),
            _offer(mpn_root_key="mpnroot:samsung:SM-S928"),
            _offer(mpn_root_key="mpnroot:samsung:SM-S928"),
        ]
        edges = mpn_root_edges(offers)
        # Hub is index 0 (smallest); edges: 0-1, 0-2.
        self.assertEqual(edges, [(0, 1), (0, 2)])

    def test_singleton_no_edges(self):
        """A group with only one member produces no edges."""
        offers = [
            _offer(mpn_root_key="mpnroot:samsung:SM-S928"),
            _offer(mpn_root_key="mpnroot:apple:IPHONE16"),
        ]
        edges = mpn_root_edges(offers)
        # Both are singletons in their respective groups.
        self.assertEqual(edges, [])

    def test_none_keys_no_edges(self):
        """Offers with None/empty mpn_root_key produce no edges."""
        offers = [
            _offer(mpn_root_key=None),
            _offer(mpn_root_key=None),
            _offer(mpn_root_key=""),
        ]
        edges = mpn_root_edges(offers)
        self.assertEqual(edges, [])

    def test_no_accidental_union_on_empty(self):
        """Offers with empty mpn_root_key must NOT get unioned together.

        This is a critical safety check: empty/None keys must never produce
        edges, or unrelated offers would be falsely merged.
        """
        offers = [
            _offer(mpn_root_key=None, title="Product A"),
            _offer(mpn_root_key=None, title="Product B"),
            _offer(mpn_root_key="", title="Product C"),
        ]
        edges = mpn_root_edges(offers)
        self.assertEqual(edges, [])
        # Verify via groups too.
        groups = mpn_root_groups(offers)
        self.assertEqual(len(groups), 0)

    def test_edges_applied_to_union_find(self):
        """Edges from mpn_root_edges correctly union offers in a UnionFind."""
        offers = [
            _offer(mpn_root_key="mpnroot:samsung:SM-S928"),  # 0
            _offer(mpn_root_key=None),                        # 1 (no key)
            _offer(mpn_root_key="mpnroot:samsung:SM-S928"),  # 2
            _offer(mpn_root_key="mpnroot:apple:IPHONE16"),   # 3
        ]
        edges = mpn_root_edges(offers)
        uf = UnionFind(len(offers))
        for a, b in edges:
            uf.union(a, b)

        # 0 and 2 should be connected (same mpn_root).
        self.assertTrue(uf.connected(0, 2))
        # 1 and 3 should remain isolated.
        self.assertFalse(uf.connected(0, 1))
        self.assertFalse(uf.connected(0, 3))
        self.assertFalse(uf.connected(1, 3))


if __name__ == "__main__":
    unittest.main()
