"""
tier_mpn.py
-----------
Tier 3 edge provider: groups offers by their mpn_key (brand + full MPN),
but ONLY when the MPN comes from a reliable source.

This module is a pure EDGE PROVIDER. It does NOT hold or mutate a
UnionFind instance — the caller applies the returned edges to the shared
DSU. This mirrors tier 2's interface exactly (offers in, edges out).

Reliability gate
-----------------
Both conditions must hold for an offer to participate in tier 3:

  1. identifier_source in {"sku", "api"}
     These are structured, store-provided identifiers. Sources like
     "title_regex" and "none" are excluded because those MPNs are
     extracted from free-text titles and are significantly noisier —
     they are handled later by the model-code tier and the fuzzy title
     tier, which have their own safeguards.

  2. mpn_key is present and non-empty.

An offer failing either condition produces NO edges and joins NO group.
This means an unreliable offer is never pulled into a tier-3 cluster
even if it happens to share an mpn_key with a reliable offer.

Cross-category and cross-brand unions
--------------------------------------
A shared reliable MPN is a strong identity signal (same manufacturer
part number from a trusted data source). Cross-category or cross-brand
unions are allowed here. Any mixing within a resulting cluster is
recorded for later review, not blocked.
"""

from collections import defaultdict

from .load import EnrichedOffer


# The only identifier sources considered reliable for tier-3 matching.
# "title_regex" and "none" are excluded — those MPNs are noisy.
_RELIABLE_SOURCES = {"sku", "api"}


def reliable_mpn_groups(offers: list[EnrichedOffer]) -> dict[str, list[int]]:
    """Group offer indices by mpn_key, filtering to reliable sources only.

    An offer is included only if:
      - its identifier_source is in {"sku", "api"}, AND
      - its mpn_key is present and non-empty.

    Returns a dict mapping each qualifying mpn_key to a sorted list of
    offer indices. Keys are sorted for deterministic iteration.
    """
    groups: dict[str, list[int]] = defaultdict(list)
    for idx, offer in enumerate(offers):
        # Reliability gate: both conditions must hold.
        if offer.identifier_source not in _RELIABLE_SOURCES:
            continue
        key = offer.mpn_key
        if not key:  # Skip None and empty strings.
            continue
        groups[key].append(idx)

    # Sort member indices within each group and return with sorted keys
    # for deterministic output.
    return {k: sorted(groups[k]) for k in sorted(groups)}


def reliable_mpn_edges(offers: list[EnrichedOffer]) -> list[tuple[int, int]]:
    """Return edges linking offers that share a reliable mpn_key.

    For each group of size >= 2, emits edges in a star pattern: every
    member is linked to the group's first (smallest-index) member. This
    is the minimum set of edges needed to union the whole group, and the
    star hub is deterministic (always the lowest index).

    Groups of size 1 emit no edges (nothing to merge).

    The returned edge list is deterministic: groups are iterated in sorted
    key order, and within each group the hub is the smallest index.
    """
    edges: list[tuple[int, int]] = []
    for _key, members in reliable_mpn_groups(offers).items():
        if len(members) < 2:
            continue
        # Star pattern: link every member to the first (smallest) index.
        hub = members[0]
        for member in members[1:]:
            edges.append((hub, member))
    return edges
