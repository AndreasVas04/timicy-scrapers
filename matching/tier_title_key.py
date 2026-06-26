"""
tier_title_key.py
-----------------
Tier 5 edge provider: groups offers by their exact (category, effective_brand,
title_key) triple and returns edges for a shared UnionFind.

This is a deterministic, exact-match tier — NOT fuzzy. Two offers union only
when all three components of the block key are identical and non-empty. This
captures color/variant differences (e.g. "iPhone 15 Blue" vs "iPhone 15 Red")
whose normalized titles differ only in color tokens that the title_key hash
ignores.

This module is a pure EDGE PROVIDER following the same interface as the other
tier modules (offers in, edges out). It does NOT hold or mutate a UnionFind
instance.

Gating rules (strict):
  - Offers with empty/None effective_brand are skipped (no edges).
  - Offers with empty/None title_key are skipped (no edges).
  - Only offers sharing ALL THREE of (category, effective_brand, title_key)
    produce edges between them.
"""

from collections import defaultdict

from .load import EnrichedOffer


def title_key_groups(
    offers: list[EnrichedOffer],
) -> dict[tuple[str | None, str | None, str], list[int]]:
    """Group offer indices by (category, effective_brand, title_key).

    Only offers with non-empty effective_brand AND non-empty title_key
    participate. All others are silently skipped — they produce no groups
    and no edges.

    Returns a dict mapping each (category, effective_brand, title_key) triple
    to a sorted list of offer indices. Dict keys are sorted for deterministic
    iteration order.
    """
    groups: dict[tuple[str | None, str | None, str], list[int]] = defaultdict(list)
    for idx, offer in enumerate(offers):
        # Gate: both effective_brand and title_key must be non-empty.
        if not offer.effective_brand:
            continue
        if not offer.title_key:
            continue
        key = (offer.category, offer.effective_brand, offer.title_key)
        groups[key].append(idx)

    # Sort member indices within each group and return with sorted keys
    # for deterministic output.
    return {k: sorted(groups[k]) for k in sorted(groups)}


def title_key_edges(offers: list[EnrichedOffer]) -> list[tuple[int, int]]:
    """Return edges linking offers that share (category, effective_brand, title_key).

    For each group of size >= 2, emits edges in a star pattern: every member
    is linked to the group's first (smallest-index) member. This is the
    minimum set of edges to union the whole group, and the star hub is
    deterministic (always the lowest index).

    Groups of size 1 emit no edges (nothing to merge).

    The returned edge list is deterministic: groups are iterated in sorted
    key order, and within each group the hub is the smallest index.
    """
    edges: list[tuple[int, int]] = []
    for _key, members in title_key_groups(offers).items():
        if len(members) < 2:
            continue
        # Star pattern: link every member to the first (smallest) index.
        hub = members[0]
        for member in members[1:]:
            edges.append((hub, member))
    return edges
