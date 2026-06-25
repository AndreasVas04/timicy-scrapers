"""
tier_mpn_root.py
----------------
Tier 2 edge provider: groups offers by their mpn_root_key and returns
edges suitable for feeding into a shared UnionFind instance.

This module is a pure EDGE PROVIDER. It does NOT hold or mutate a
UnionFind instance — the caller applies the returned edges to the
shared DSU. This keeps the interface uniform across all deterministic
tiers (offers in, edges out).

Design note — cross-category and cross-brand unions:
    A shared mpn_root is a strong identity signal (same manufacturer
    part-number family). If two offers from different categories or
    different brands share an mpn_root_key, they ARE the same product
    (or the upstream data is wrong). We therefore do NOT block cross-
    category or cross-brand unions here. Any mixing within a resulting
    cluster is recorded for later review, not blocked.
"""

from collections import defaultdict

from .load import EnrichedOffer


def mpn_root_groups(offers: list[EnrichedOffer]) -> dict[str, list[int]]:
    """Group offer indices by their mpn_root_key.

    Offers whose mpn_root_key is None or empty are skipped entirely — they
    produce no groups and no edges.

    Returns a dict mapping each non-empty mpn_root_key to a sorted list of
    offer indices that share it. Keys are sorted for deterministic iteration.
    """
    groups: dict[str, list[int]] = defaultdict(list)
    for idx, offer in enumerate(offers):
        key = offer.mpn_root_key
        if key:  # Skip None and empty strings.
            groups[key].append(idx)

    # Sort member indices within each group and return with sorted keys
    # for deterministic output.
    return {k: sorted(groups[k]) for k in sorted(groups)}


def mpn_root_edges(offers: list[EnrichedOffer]) -> list[tuple[int, int]]:
    """Return edges linking offers that share an mpn_root_key.

    For each group of size >= 2, emits edges in a star pattern: every
    member is linked to the group's first (smallest-index) member. This
    is the minimum set of edges needed to union the whole group, and the
    star hub is deterministic (always the lowest index).

    Groups of size 1 emit no edges (nothing to merge).

    The returned edge list is deterministic: groups are iterated in sorted
    key order, and within each group the hub is the smallest index.
    """
    edges: list[tuple[int, int]] = []
    for _key, members in mpn_root_groups(offers).items():
        if len(members) < 2:
            continue
        # Star pattern: link every member to the first (smallest) index.
        hub = members[0]
        for member in members[1:]:
            edges.append((hub, member))
    return edges
