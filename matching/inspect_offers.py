#!/usr/bin/env python3
"""
inspect_offers.py
-----------------
Strictly read-only CLI that loads all enriched offers and prints a
detailed statistics report to stdout.

Contains NO database writes. The default report (sections 1-6) contains
no matching logic. Optional flags add dry-run tier sections that compute
clusters in memory and print statistics — still read-only, no writes.

Safe to run repeatedly.

Usage:
    python -m matching.inspect_offers                  # default report
    python -m matching.inspect_offers --mpn-root       # + tier-2 dry-run
    python -m matching.inspect_offers --deterministic  # + tier 2+3 combined
    python -m matching.inspect_offers --model-codes    # + model-code trust analysis
    python -m matching.inspect_offers --tier4          # + tier 2+3+4 combined dry-run
    python -m matching.inspect_offers --final          # + full deterministic pipeline (T2+T3+T4+T5)
"""

import argparse
import re
from collections import Counter, defaultdict

# Support running both as a package module (-m matching.inspect_offers)
# and directly (python3 matching/inspect_offers.py).
try:
    from .load import EnrichedOffer, get_connection, load_offers
except ImportError:
    from load import EnrichedOffer, get_connection, load_offers


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _header(title: str) -> None:
    """Print a section header with a visual separator."""
    print()
    print("=" * 90)
    print(f"  {title}")
    print("=" * 90)


def _table(headers: list[str], rows: list[list], align: list[str] | None = None) -> None:
    """Print a simple aligned table.

    *align* is a list of '<' (left) or '>' (right) per column; defaults to
    left for all columns.
    """
    if not rows:
        print("  (no data)")
        return

    # Calculate column widths from headers and data.
    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val)))

    if align is None:
        align = ["<"] * len(headers)

    # Build format string for each column.
    parts = []
    for w, a in zip(widths, align):
        parts.append(f"{{:{a}{w}}}")
    fmt = "  ".join(parts)

    print("  " + fmt.format(*headers))
    print("  " + "  ".join("-" * w for w in widths))
    for row in rows:
        print("  " + fmt.format(*(str(v) for v in row)))


def _pct(part: int, total: int) -> str:
    """Format a percentage string, handling division by zero."""
    if total == 0:
        return "0.0%"
    return f"{100 * part / total:.1f}%"


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------

def section_totals(offers: list[EnrichedOffer]) -> dict[str, list[EnrichedOffer]]:
    """Section 1: Total offers loaded and per-store breakdown.

    Returns a dict of store -> offers for use by later sections.
    """
    _header("1. Total offers and per-store counts")

    by_store: dict[str, list[EnrichedOffer]] = defaultdict(list)
    for o in offers:
        by_store[o.store].append(o)

    print(f"\n  Total offers loaded: {len(offers)}\n")
    rows = [[store, len(items)] for store, items in sorted(by_store.items())]
    _table(["store", "count"], rows, align=["<", ">"])

    return dict(by_store)


def section_coverage(offers: list[EnrichedOffer],
                     by_store: dict[str, list[EnrichedOffer]]) -> None:
    """Section 2: Key-coverage rates per store.

    For each store (and a TOTAL row), shows the count and percentage of
    offers that have: ean_key, mpn_root_key, reliable mpn_key
    (identifier_source in {sku, api}), at least one model_code, title_key.
    """
    _header("2. Key coverage by store")

    # Reliable mpn_key: the mpn came from a structured source, not a
    # title regex or fallback. Only sku and api are considered reliable.
    reliable_sources = {"sku", "api"}

    headers = ["store", "total", "ean_key", "%", "mpn_root", "%",
               "mpn(rel)", "%", "model_cd", "%", "title_key", "%"]

    def _row(label: str, items: list[EnrichedOffer]) -> list:
        n = len(items)
        has_ean = sum(1 for o in items if o.ean_key)
        has_mr = sum(1 for o in items if o.mpn_root_key)
        has_mpn_rel = sum(1 for o in items
                         if o.mpn_key and o.identifier_source in reliable_sources)
        has_mc = sum(1 for o in items if o.model_codes)
        has_tk = sum(1 for o in items if o.title_key)
        return [label, n,
                has_ean, _pct(has_ean, n),
                has_mr, _pct(has_mr, n),
                has_mpn_rel, _pct(has_mpn_rel, n),
                has_mc, _pct(has_mc, n),
                has_tk, _pct(has_tk, n)]

    rows = [_row(store, items) for store, items in sorted(by_store.items())]
    rows.append(_row("TOTAL", offers))

    align_spec = ["<", ">", ">", ">", ">", ">", ">", ">", ">", ">", ">", ">"]
    _table(headers, rows, align=align_spec)


def section_block_distribution(offers: list[EnrichedOffer]) -> None:
    """Section 3: Block-size distribution for (category, effective_brand) blocks.

    A 'block' is the set of offers sharing the same (category, effective_brand)
    pair. The histogram shows how many blocks fall into each size bucket.
    These blocks define the working sets for the future title-matching tier.
    """
    _header("3. Block-size distribution (category, effective_brand)")

    # Count offers per (category, effective_brand) block.
    block_counts: Counter[tuple[str | None, str | None]] = Counter()
    for o in offers:
        block_counts[(o.category, o.effective_brand)] += 1

    num_blocks = len(block_counts)
    sizes = list(block_counts.values())

    print(f"\n  Total blocks: {num_blocks}")
    print()

    # Histogram buckets.
    buckets = [
        ("1", 1, 1),
        ("2-5", 2, 5),
        ("6-20", 6, 20),
        ("21-100", 21, 100),
        ("101-500", 101, 500),
        ("500+", 501, float("inf")),
    ]
    rows = []
    for label, lo, hi in buckets:
        count = sum(1 for s in sizes if lo <= s <= hi)
        rows.append([label, count, _pct(count, num_blocks)])
    _table(["block size", "blocks", "%"], rows, align=["<", ">", ">"])


def section_largest_blocks(offers: list[EnrichedOffer]) -> None:
    """Section 4: The 25 largest (category, effective_brand) blocks.

    These are the blocks with the most offers. Large blocks surface future
    O(n^2) risk in the fuzzy title-matching tier, where every pair of
    offers within a block may need to be compared.
    """
    _header("4. Top 25 largest (category, effective_brand) blocks")

    block_counts: Counter[tuple[str | None, str | None]] = Counter()
    for o in offers:
        block_counts[(o.category, o.effective_brand)] += 1

    # Sort by size descending, take top 25.
    top = block_counts.most_common(25)

    rows = []
    for (cat, brand), size in top:
        rows.append([cat or "(none)", brand or "(none)", size])
    _table(["category", "effective_brand", "size"], rows, align=["<", "<", ">"])


def section_suspicious_brands(offers: list[EnrichedOffer]) -> None:
    """Section 5: Suspicious-brand statistics.

    Shows how many offers have is_suspicious_brand=True, and of those,
    how many have a usable brand_from_title (recoverable) vs. those that
    would need manual review.
    """
    _header("5. Suspicious-brand stats")

    suspicious = [o for o in offers if o.is_suspicious_brand]
    recoverable = [o for o in suspicious if o.brand_from_title]
    unrecoverable = len(suspicious) - len(recoverable)

    print(f"\n  Total offers:               {len(offers)}")
    print(f"  Suspicious brand:           {len(suspicious)}  ({_pct(len(suspicious), len(offers))})")
    print(f"    Recoverable (title brand): {len(recoverable)}")
    print(f"    Needs review (no title brand): {unrecoverable}")

    # Show a few example suspicious brands for context.
    if suspicious:
        print()
        examples: Counter[str] = Counter()
        for o in suspicious:
            label = f"{o.brand_norm or '(empty)'} -> {o.brand_from_title or '(none)'}"
            examples[label] += 1
        print("  Top 15 suspicious brand mappings (vendor_brand -> title_brand):")
        for label, count in examples.most_common(15):
            print(f"    {count:>5}x  {label}")


def section_model_code_frequency(offers: list[EnrichedOffer]) -> None:
    """Section 6: Model-code frequency distribution.

    Counts how many distinct offers each model code appears in. Reports:
      - Overall distribution (codes appearing in 1, 2-5, 6-20, 21+ offers).
      - Top 30 highest-frequency codes — candidates for a future frequency-
        based trust filter (non-discriminative codes to ignore in the
        model-code tier).

    Does NOT implement the filter; only reports.
    """
    _header("6. Model-code frequency distribution")

    # Count how many offers contain each model code.
    code_freq: Counter[str] = Counter()
    for o in offers:
        for code in o.model_codes:
            code_freq[code] += 1

    total_codes = len(code_freq)
    print(f"\n  Distinct model codes found: {total_codes}")

    if total_codes == 0:
        return

    # Distribution buckets.
    print()
    buckets = [
        ("1 offer", 1, 1),
        ("2-5 offers", 2, 5),
        ("6-20 offers", 6, 20),
        ("21+ offers", 21, float("inf")),
    ]
    rows = []
    for label, lo, hi in buckets:
        count = sum(1 for freq in code_freq.values() if lo <= freq <= hi)
        rows.append([label, count, _pct(count, total_codes)])
    _table(["appears in", "codes", "%"], rows, align=["<", ">", ">"])

    # Top 30 highest-frequency model codes.
    print()
    print("  Top 30 highest-frequency model codes:")
    print("  (candidates for future non-discriminative code filter)")
    print()
    top30 = code_freq.most_common(30)
    top_rows = [[code, freq] for code, freq in top30]
    _table(["model_code", "offers"], top_rows, align=["<", ">"])


# ---------------------------------------------------------------------------
# Section 7: mpn_root tier-2 dry-run (behind --mpn-root flag)
# ---------------------------------------------------------------------------

def section_mpn_root_tier(offers: list[EnrichedOffer]) -> None:
    """Tier-2 dry-run: compute mpn_root clusters in memory and print stats.

    This is strictly read-only — it builds a UnionFind in memory, applies
    tier-2 edges, and reports what the resulting clusters look like. No
    database writes of any kind.
    """
    try:
        from .tier_mpn_root import mpn_root_edges, mpn_root_groups
        from .union_find import UnionFind
    except ImportError:
        from tier_mpn_root import mpn_root_edges, mpn_root_groups
        from union_find import UnionFind

    _header("7. Tier-2 (mpn_root) dry-run")

    # -- Basic coverage stats --
    groups = mpn_root_groups(offers)
    has_key = sum(1 for o in offers if o.mpn_root_key)
    groups_ge2 = {k: v for k, v in groups.items() if len(v) >= 2}
    offers_in_ge2 = sum(len(v) for v in groups_ge2.values())

    print(f"\n  Offers with non-empty mpn_root_key: {has_key}")
    print(f"  Distinct mpn_root groups (any size): {len(groups)}")
    print(f"  Groups with size >= 2:               {len(groups_ge2)}")
    print(f"  Offers in size >= 2 groups:           {offers_in_ge2}")

    # -- Build DSU and apply tier-2 edges --
    edges = mpn_root_edges(offers)
    uf = UnionFind(len(offers))
    for a, b in edges:
        uf.union(a, b)

    all_clusters = uf.groups()
    clusters_ge2 = [c for c in all_clusters if len(c) >= 2]
    singletons = len(all_clusters) - len(clusters_ge2)

    print(f"\n  Clusters after tier 2 (size >= 2):   {len(clusters_ge2)}")
    print(f"  Singletons remaining:                {singletons}")

    # -- Top 25 largest clusters --
    print()
    sorted_by_size = sorted(clusters_ge2, key=len, reverse=True)
    top25 = sorted_by_size[:25]
    rows = []
    for i, cluster in enumerate(top25, 1):
        rows.append([i, len(cluster)])
    _table(["rank", "size"], rows, align=[">", ">"])

    # -- Multi-store, cross-category, cross-brand counts --
    multi_store = 0
    cross_category = 0
    cross_brand = 0
    for cluster in clusters_ge2:
        stores = {offers[i].store for i in cluster}
        cats = {offers[i].category for i in cluster}
        brands = {offers[i].brand_norm for i in cluster}
        if len(stores) >= 2:
            multi_store += 1
        if len(cats) >= 2:
            cross_category += 1
        if len(brands) >= 2:
            cross_brand += 1

    print(f"\n  Multi-store clusters (>= 2 stores):    {multi_store}")
    print(f"  Cross-category clusters (>= 2 cats):   {cross_category}")
    print(f"  Cross-brand clusters (>= 2 brands):    {cross_brand}")

    # -- Sample clusters for manual review --
    # Pick a mix: some large, some multi-store, some cross-category/brand.
    _header("7b. Sample clusters for manual review")

    # Collect interesting clusters, then fill with the largest if needed.
    sample_indices: list[int] = []
    seen_indices: set[int] = set()

    def _add(idx: int) -> None:
        if idx not in seen_indices and idx < len(clusters_ge2):
            seen_indices.add(idx)
            sample_indices.append(idx)

    # Index clusters_ge2 by position in sorted_by_size for easy lookup.
    # Grab the top 5 largest.
    for i in range(min(5, len(sorted_by_size))):
        _add(i)

    # Grab some multi-store, cross-category, cross-brand clusters.
    for ci, cluster in enumerate(sorted_by_size):
        if len(sample_indices) >= 15:
            break
        stores = {offers[i].store for i in cluster}
        cats = {offers[i].category for i in cluster}
        brands = {offers[i].brand_norm for i in cluster}
        if len(stores) >= 2 or len(cats) >= 2 or len(brands) >= 2:
            _add(ci)

    # Fill remaining slots with largest clusters.
    for i in range(len(sorted_by_size)):
        if len(sample_indices) >= 15:
            break
        _add(i)

    if not sample_indices:
        print("  (no clusters to sample)")
    else:
        for rank in sample_indices:
            cluster = sorted_by_size[rank]
            stores = {offers[i].store for i in cluster}
            cats = {offers[i].category for i in cluster}
            brands = {offers[i].brand_norm for i in cluster}
            tags = []
            if len(stores) >= 2:
                tags.append("multi-store")
            if len(cats) >= 2:
                tags.append("cross-category")
            if len(brands) >= 2:
                tags.append("cross-brand")
            tag_str = f"  [{', '.join(tags)}]" if tags else ""

            print(f"\n  --- Cluster (size {len(cluster)}){tag_str} ---")
            for idx in cluster[:10]:  # Cap at 10 members per cluster for readability.
                o = offers[idx]
                print(f"    store={o.store:<12s} cat={str(o.category):<20s} "
                      f"brand={str(o.brand_norm):<15s} mpn_root={str(o.mpn_root):<15s}")
                print(f"      title: {o.title[:100]}")
                print(f"      url:   {o.product_url[:100]}")
            if len(cluster) > 10:
                print(f"    ... and {len(cluster) - 10} more members")


# ---------------------------------------------------------------------------
# Section 8: combined tier 2 + tier 3 dry-run (behind --deterministic flag)
# ---------------------------------------------------------------------------

def _print_cluster_sample(
    label: str,
    offers: list[EnrichedOffer],
    clusters_sorted: list[list[int]],
    max_samples: int = 15,
) -> None:
    """Print a sample of clusters for manual review.

    Picks a mix of large, multi-store, and cross-category/brand clusters,
    filling remaining slots with the largest clusters.
    """
    _header(f"{label}. Sample clusters for manual review")

    sample_indices: list[int] = []
    seen: set[int] = set()

    def _add(idx: int) -> None:
        if idx not in seen and idx < len(clusters_sorted):
            seen.add(idx)
            sample_indices.append(idx)

    # Grab the top 5 largest.
    for i in range(min(5, len(clusters_sorted))):
        _add(i)

    # Grab multi-store, cross-category, cross-brand clusters.
    for ci, cluster in enumerate(clusters_sorted):
        if len(sample_indices) >= max_samples:
            break
        stores = {offers[i].store for i in cluster}
        cats = {offers[i].category for i in cluster}
        brands = {offers[i].brand_norm for i in cluster}
        if len(stores) >= 2 or len(cats) >= 2 or len(brands) >= 2:
            _add(ci)

    # Fill remaining slots with largest.
    for i in range(len(clusters_sorted)):
        if len(sample_indices) >= max_samples:
            break
        _add(i)

    if not sample_indices:
        print("  (no clusters to sample)")
        return

    for rank in sample_indices:
        cluster = clusters_sorted[rank]
        stores = {offers[i].store for i in cluster}
        cats = {offers[i].category for i in cluster}
        brands = {offers[i].brand_norm for i in cluster}
        tags = []
        if len(stores) >= 2:
            tags.append("multi-store")
        if len(cats) >= 2:
            tags.append("cross-category")
        if len(brands) >= 2:
            tags.append("cross-brand")
        tag_str = f"  [{', '.join(tags)}]" if tags else ""

        print(f"\n  --- Cluster (size {len(cluster)}){tag_str} ---")
        for idx in cluster[:10]:
            o = offers[idx]
            print(f"    store={o.store:<12s} cat={str(o.category):<20s} "
                  f"brand={str(o.brand_norm):<15s} "
                  f"mpn={str(o.mpn):<20s} mpn_root={str(o.mpn_root):<15s} "
                  f"src={o.identifier_source}")
            print(f"      title: {o.title[:100]}")
            print(f"      url:   {o.product_url[:100]}")
        if len(cluster) > 10:
            print(f"    ... and {len(cluster) - 10} more members")


def section_deterministic_tiers(offers: list[EnrichedOffer]) -> None:
    """Combined tier 2 + tier 3 dry-run on the SAME UnionFind.

    Applies tier-2 edges first, snapshots the state, then applies tier-3
    edges, and reports both the per-tier and combined statistics. This
    shows how the two deterministic tiers interact and what the tier-3
    delta looks like.

    Strictly read-only — builds a UnionFind in memory, no DB writes.
    """
    try:
        from .tier_mpn import reliable_mpn_edges, reliable_mpn_groups
        from .tier_mpn_root import mpn_root_edges, mpn_root_groups
        from .union_find import UnionFind
    except ImportError:
        from tier_mpn import reliable_mpn_edges, reliable_mpn_groups
        from tier_mpn_root import mpn_root_edges, mpn_root_groups
        from union_find import UnionFind

    n = len(offers)

    # ==================================================================
    # Tier 2 stats
    # ==================================================================
    _header("8a. Tier-2 (mpn_root) stats")

    t2_groups = mpn_root_groups(offers)
    t2_has_key = sum(1 for o in offers if o.mpn_root_key)
    t2_groups_ge2 = {k: v for k, v in t2_groups.items() if len(v) >= 2}
    t2_offers_in_ge2 = sum(len(v) for v in t2_groups_ge2.values())

    print(f"\n  Offers with non-empty mpn_root_key: {t2_has_key}")
    print(f"  Distinct mpn_root groups (any size): {len(t2_groups)}")
    print(f"  Groups with size >= 2:               {len(t2_groups_ge2)}")
    print(f"  Offers in size >= 2 groups:           {t2_offers_in_ge2}")

    # Build DSU and apply tier-2 edges.
    t2_edges = mpn_root_edges(offers)
    uf = UnionFind(n)
    for a, b in t2_edges:
        uf.union(a, b)

    # Snapshot post-tier-2 state so we can compute the tier-3 delta.
    # Record which root each offer belongs to after tier 2.
    post_t2_root = [uf.find(i) for i in range(n)]
    post_t2_clusters = uf.groups()
    post_t2_ge2 = [c for c in post_t2_clusters if len(c) >= 2]
    post_t2_singletons_set = {c[0] for c in post_t2_clusters if len(c) == 1}

    print(f"\n  Non-trivial clusters after tier 2:   {len(post_t2_ge2)}")
    print(f"  Singletons after tier 2:             {len(post_t2_singletons_set)}")

    # ==================================================================
    # Tier 3 stats
    # ==================================================================
    _header("8b. Tier-3 (reliable MPN) stats")

    # Reliability gate breakdown by identifier_source.
    source_counts: Counter = Counter()
    for o in offers:
        source_counts[o.identifier_source] += 1
    reliable_sources = {"sku", "api"}
    reliable_count = sum(
        1 for o in offers
        if o.identifier_source in reliable_sources and o.mpn_key
    )

    print(f"\n  identifier_source distribution:")
    for src in sorted(source_counts):
        print(f"    {src:<15s}  {source_counts[src]:>6}")
    print(f"\n  Offers passing reliability gate (source + mpn_key): {reliable_count}")

    t3_groups = reliable_mpn_groups(offers)
    t3_groups_ge2 = {k: v for k, v in t3_groups.items() if len(v) >= 2}
    t3_offers_in_ge2 = sum(len(v) for v in t3_groups_ge2.values())

    print(f"  Distinct reliable-mpn groups (any size): {len(t3_groups)}")
    print(f"  Groups with size >= 2:                   {len(t3_groups_ge2)}")
    print(f"  Offers in size >= 2 groups:               {t3_offers_in_ge2}")

    # Apply tier-3 edges to the SAME UnionFind (on top of tier 2).
    t3_edges = reliable_mpn_edges(offers)
    for a, b in t3_edges:
        uf.union(a, b)

    # ==================================================================
    # Combined tier 2 + tier 3 cluster stats
    # ==================================================================
    _header("8c. Combined tier 2 + tier 3 cluster stats")

    combined_clusters = uf.groups()
    combined_ge2 = [c for c in combined_clusters if len(c) >= 2]
    combined_singletons = len(combined_clusters) - len(combined_ge2)

    print(f"\n  Non-trivial clusters (size >= 2):   {len(combined_ge2)}")
    print(f"  Singletons remaining:               {combined_singletons}")

    # Top 25 largest clusters.
    print()
    sorted_by_size = sorted(combined_ge2, key=len, reverse=True)
    top25 = sorted_by_size[:25]
    rows = [[i, len(c)] for i, c in enumerate(top25, 1)]
    _table(["rank", "size"], rows, align=[">", ">"])

    # ==================================================================
    # Tier-3 delta: what did tier 3 add beyond tier 2?
    # ==================================================================
    _header("8d. Tier-3 delta (what tier 3 added beyond tier 2)")

    # A cluster is NEW if all its members were singletons after tier 2.
    # A cluster GREW if it existed as non-trivial after tier 2 and gained
    # members from tier 3.
    new_clusters = 0
    grew_clusters = 0
    for cluster in combined_ge2:
        # Check if any member was in a non-trivial cluster after tier 2.
        member_roots_t2 = {post_t2_root[i] for i in cluster}
        was_any_nontrivial = any(r not in post_t2_singletons_set for r in member_roots_t2)
        if not was_any_nontrivial:
            # All members were singletons after tier 2 -> new cluster.
            new_clusters += 1
        else:
            # At least one member was in a non-trivial tier-2 cluster.
            # Check if the combined cluster is larger than what tier 2 had.
            # A simple check: if the combined cluster has members from
            # different tier-2 roots, it grew (merged tier-2 clusters or
            # added singletons).
            t2_nontrivial_roots = {r for r in member_roots_t2 if r not in post_t2_singletons_set}
            t2_singleton_members = {i for i in cluster if post_t2_root[i] in post_t2_singletons_set}
            if len(t2_nontrivial_roots) > 1 or t2_singleton_members:
                grew_clusters += 1

    print(f"\n  New non-trivial clusters (all members were singletons after T2): {new_clusters}")
    print(f"  Existing T2 clusters that grew (gained members or merged):       {grew_clusters}")

    # ==================================================================
    # Cross-dimension stats
    # ==================================================================
    multi_store = 0
    cross_category = 0
    cross_brand = 0
    for cluster in combined_ge2:
        stores = {offers[i].store for i in cluster}
        cats = {offers[i].category for i in cluster}
        brands = {offers[i].brand_norm for i in cluster}
        if len(stores) >= 2:
            multi_store += 1
        if len(cats) >= 2:
            cross_category += 1
        if len(brands) >= 2:
            cross_brand += 1

    print(f"\n  Multi-store clusters (>= 2 stores):    {multi_store}")
    print(f"  Cross-category clusters (>= 2 cats):   {cross_category}")
    print(f"  Cross-brand clusters (>= 2 brands):    {cross_brand}")

    # ==================================================================
    # Sample clusters
    # ==================================================================
    _print_cluster_sample("8e", offers, sorted_by_size)


# ---------------------------------------------------------------------------
# Section 9: model-code trust analysis (behind --model-codes flag)
# ---------------------------------------------------------------------------
# Strictly read-only, analysis-only. Does NOT create edges, does NOT touch
# any UnionFind/DSU, and does NOT write to the database. Its only purpose
# is to surface the real model-code data so we can choose safe trust
# thresholds before building the tier-4 edge provider.

# Regex for space-separated code pattern scan (section 9.7).
# Matches a short letter prefix (2-5 chars) followed by whitespace and an
# alphanumeric token containing at least one digit (length >= 3).
# Example: "SWK 2511BK", "SCG 6050SS".
_RE_SPACE_CODE = re.compile(
    r"\b([A-Za-z]{2,5})\s+([A-Za-z0-9]{3,})\b"
)


def section_model_codes(offers: list[EnrichedOffer]) -> None:
    """Full model-code trust analysis for tier-4 planning.

    Nine subsections covering coverage, frequency, block spread, noise
    candidates, good-code examples, space-separated patterns, cross-store
    bridging potential, and brand+category eligibility.
    """
    n_total = len(offers)

    # -- Precompute shared data structures used across multiple sections --

    # offer_block: maps each offer index to its (category, effective_brand) block.
    offer_block: list[tuple[str | None, str | None]] = [
        (o.category, o.effective_brand) for o in offers
    ]

    # code_to_offer_indices: maps each model code to the set of offer indices
    # that contain it.
    code_to_indices: dict[str, list[int]] = defaultdict(list)
    for idx, o in enumerate(offers):
        for code in o.model_codes:
            code_to_indices[code].append(idx)

    # code_offer_count: how many offers each code appears in.
    code_offer_count: dict[str, int] = {
        code: len(indices) for code, indices in code_to_indices.items()
    }

    # code_block_count: how many distinct (category, effective_brand) blocks
    # each code appears in.
    code_block_count: dict[str, int] = {}
    for code, indices in code_to_indices.items():
        blocks = {offer_block[i] for i in indices}
        code_block_count[code] = len(blocks)

    # ======================================================================
    # 9.1 COVERAGE
    # ======================================================================
    _header("9.1 Model-code coverage")

    has_code = [o for o in offers if o.model_codes]
    print(f"\n  Offers with >= 1 model code: {len(has_code)} / {n_total} "
          f"({_pct(len(has_code), n_total)})")

    # By store.
    print("\n  By store:")
    by_store: dict[str, list[EnrichedOffer]] = defaultdict(list)
    for o in offers:
        by_store[o.store].append(o)
    rows = []
    for store in sorted(by_store):
        items = by_store[store]
        cnt = sum(1 for o in items if o.model_codes)
        rows.append([store, len(items), cnt, _pct(cnt, len(items))])
    _table(["store", "total", "has_code", "%"], rows,
           align=["<", ">", ">", ">"])

    # By category.
    print("\n  By category:")
    by_cat: dict[str | None, list[EnrichedOffer]] = defaultdict(list)
    for o in offers:
        by_cat[o.category].append(o)
    rows = []
    for cat in sorted(by_cat, key=lambda c: c or ""):
        items = by_cat[cat]
        cnt = sum(1 for o in items if o.model_codes)
        rows.append([cat or "(none)", len(items), cnt, _pct(cnt, len(items))])
    _table(["category", "total", "has_code", "%"], rows,
           align=["<", ">", ">", ">"])

    # By brand (top 20).
    print("\n  By brand (top 20 by offer count):")
    by_brand: dict[str, list[EnrichedOffer]] = defaultdict(list)
    for o in offers:
        by_brand[o.brand_norm or "(empty)"].append(o)
    brand_sorted = sorted(by_brand.items(), key=lambda kv: -len(kv[1]))[:20]
    rows = []
    for brand, items in brand_sorted:
        cnt = sum(1 for o in items if o.model_codes)
        rows.append([brand, len(items), cnt, _pct(cnt, len(items))])
    _table(["brand", "total", "has_code", "%"], rows,
           align=["<", ">", ">", ">"])

    # ======================================================================
    # 9.2 CODES-PER-OFFER DISTRIBUTION
    # ======================================================================
    _header("9.2 Codes-per-offer distribution")

    code_counts = [len(o.model_codes) for o in offers]
    buckets = [
        ("0", 0, 0),
        ("1", 1, 1),
        ("2", 2, 2),
        ("3", 3, 3),
        ("4+", 4, float("inf")),
    ]
    rows = []
    for label, lo, hi in buckets:
        cnt = sum(1 for c in code_counts if lo <= c <= hi)
        rows.append([label, cnt, _pct(cnt, n_total)])
    _table(["codes", "offers", "%"], rows, align=["<", ">", ">"])

    with_codes = [c for c in code_counts if c > 0]
    if with_codes:
        mean_codes = sum(with_codes) / len(with_codes)
        max_codes = max(with_codes)
        print(f"\n  Among offers with >= 1 code: mean={mean_codes:.2f}, max={max_codes}")

    # ======================================================================
    # 9.3 GLOBAL FREQUENCY DISTRIBUTION (offer-count per code)
    # ======================================================================
    _header("9.3 Global frequency distribution (offer-count per code)")

    total_codes = len(code_offer_count)
    print(f"\n  Total distinct model codes: {total_codes}")
    if total_codes == 0:
        return

    freq_values = list(code_offer_count.values())
    buckets = [
        ("1 offer", 1, 1),
        ("2-5", 2, 5),
        ("6-20", 6, 20),
        ("21-50", 21, 50),
        ("51-200", 51, 200),
        ("200+", 201, float("inf")),
    ]
    rows = []
    for label, lo, hi in buckets:
        cnt = sum(1 for f in freq_values if lo <= f <= hi)
        rows.append([label, cnt, _pct(cnt, total_codes)])
    _table(["appears in", "codes", "%"], rows, align=["<", ">", ">"])

    # ======================================================================
    # 9.4 BLOCK-SPREAD DISTRIBUTION
    # ======================================================================
    # A code confined to ONE (category, effective_brand) block is likely a
    # real model code for that product family. A code spread across MANY
    # blocks is likely a non-discriminative spec fragment (e.g. "8gb128gb",
    # "2in1") — these are what the trust filter should target.
    _header("9.4 Block-spread distribution (codes by # of blocks)")

    spread_values = list(code_block_count.values())
    buckets = [
        ("1 block", 1, 1),
        ("2 blocks", 2, 2),
        ("3-5", 3, 5),
        ("6-10", 6, 10),
        ("11+", 11, float("inf")),
    ]
    rows = []
    for label, lo, hi in buckets:
        cnt = sum(1 for s in spread_values if lo <= s <= hi)
        rows.append([label, cnt, _pct(cnt, total_codes)])
    _table(["spread", "codes", "%"], rows, align=["<", ">", ">"])

    # ======================================================================
    # 9.5 HIGH-FREQUENCY / HIGH-SPREAD NOISY CANDIDATES
    # ======================================================================
    _header("9.5 Top 50 noisy candidates (by block-spread, tie-break offer count)")

    # Sort codes by block-spread descending, then offer count descending.
    ranked = sorted(
        code_offer_count.keys(),
        key=lambda c: (code_block_count[c], code_offer_count[c]),
        reverse=True,
    )
    top50 = ranked[:50]

    print()
    for code in top50:
        oc = code_offer_count[code]
        bc = code_block_count[code]
        # Grab 2-3 example titles.
        example_indices = code_to_indices[code][:3]
        examples = [offers[i].title[:80] for i in example_indices]
        print(f"  {code:<20s}  offers={oc:<4d}  blocks={bc:<3d}")
        for ex in examples:
            print(f"      {ex}")

    # ======================================================================
    # 9.6 GOOD-CODE EXAMPLES
    # ======================================================================
    _header("9.6 Good-code examples (1 block, 2-5 offers)")

    # Codes confined to exactly one block and appearing in 2-5 offers are
    # plausible real shared model codes — the kind tier 4 should trust.
    good_codes = [
        code for code in code_offer_count
        if code_block_count[code] == 1 and 2 <= code_offer_count[code] <= 5
    ]
    # Sort for deterministic output.
    good_codes.sort()

    print(f"\n  Total good codes (1 block, 2-5 offers): {len(good_codes)}")

    # Sample 30.
    sample = good_codes[:30]
    bridges = 0
    for code in sample:
        indices = code_to_indices[code]
        stores = {offers[i].store for i in indices}
        is_bridge = len(stores) >= 2
        if is_bridge:
            bridges += 1
        tag = " [BRIDGE]" if is_bridge else ""
        print(f"\n  {code} ({code_offer_count[code]} offers){tag}:")
        for i in indices:
            o = offers[i]
            print(f"    store={o.store:<12s} {o.title[:90]}")

    total_bridges = sum(
        1 for code in good_codes
        if len({offers[i].store for i in code_to_indices[code]}) >= 2
    )
    print(f"\n  Of {len(good_codes)} good codes, {total_bridges} bridge >= 2 stores.")

    # ======================================================================
    # 9.7 SPACE-SEPARATED CODE PATTERN (join-heuristic feasibility)
    # ======================================================================
    # Scans raw offer.title for adjacent token pairs like "SWK 2511BK" where
    # the joined form "swk2511bk" is not already in model_codes. This tells
    # us whether the deferred join heuristic is worth building.
    # Does NOT apply any joining; just reports candidates.
    _header("9.7 Space-separated code patterns (join-heuristic feasibility)")

    candidates: list[tuple[int, str, str, str]] = []  # (offer_idx, prefix, suffix, joined)
    offers_with_pattern = 0

    for idx, o in enumerate(offers):
        found_any = False
        existing_codes = set(o.model_codes)
        for m in _RE_SPACE_CODE.finditer(o.title):
            prefix = m.group(1)
            suffix = m.group(2)
            # The suffix must contain at least one digit to look like a code.
            if not any(ch.isdigit() for ch in suffix):
                continue
            joined = (prefix + suffix).lower()
            # Only report if the joined form is NOT already extracted.
            if joined not in existing_codes:
                candidates.append((idx, prefix, suffix, joined))
                found_any = True
        if found_any:
            offers_with_pattern += 1

    print(f"\n  Offers with >= 1 space-separated code pattern: {offers_with_pattern}")
    print(f"  Total candidate pairs found: {len(candidates)}")

    # Sample 30.
    print()
    for offer_idx, prefix, suffix, joined in candidates[:30]:
        o = offers[offer_idx]
        print(f"  store={o.store:<12s} pair=\"{prefix} {suffix}\" -> \"{joined}\"")
        print(f"    title: {o.title[:100]}")

    # ======================================================================
    # 9.8 CROSS-STORE BRIDGING POTENTIAL (the tier-4 payoff metric)
    # ======================================================================
    _header("9.8 Cross-store bridging potential")

    # Of codes appearing in >= 2 offers, how many span >= 2 distinct stores?
    codes_ge2 = {c for c, n in code_offer_count.items() if n >= 2}
    cross_store_codes = []
    for code in sorted(codes_ge2):
        stores = {offers[i].store for i in code_to_indices[code]}
        if len(stores) >= 2:
            cross_store_codes.append(code)

    print(f"\n  Codes in >= 2 offers: {len(codes_ge2)}")
    print(f"  Of those, spanning >= 2 stores: {len(cross_store_codes)}")

    # Specifically count Stephanis <-> Public bridges (EN <-> EL).
    # This is the primary cross-language bridge tier 4 exists for.
    step_pub_codes = []
    for code in cross_store_codes:
        stores = {offers[i].store for i in code_to_indices[code]}
        if "stephanis" in stores and "public" in stores:
            step_pub_codes.append(code)

    print(f"  Stephanis <-> Public shared codes: {len(step_pub_codes)}")

    # Sample 20 Stephanis <-> Public pairs.
    print()
    print("  Sample Stephanis <-> Public shared codes:")
    for code in step_pub_codes[:20]:
        indices = code_to_indices[code]
        step_titles = [offers[i].title for i in indices if offers[i].store == "stephanis"]
        pub_titles = [offers[i].title for i in indices if offers[i].store == "public"]
        print(f"\n  code: {code}")
        if step_titles:
            print(f"    stephanis: {step_titles[0][:100]}")
        if pub_titles:
            print(f"    public:    {pub_titles[0][:100]}")

    # ======================================================================
    # 9.9 BRAND+CATEGORY ELIGIBILITY
    # ======================================================================
    # For each (category, effective_brand) block, count how many codes are
    # shared by >= 2 offers WITHIN that block (intra-block matchable codes).
    # This gives a rough upper bound on tier-4-eligible offers.
    _header("9.9 Brand+category eligibility (intra-block matchable codes)")

    # Build block -> list of offer indices.
    block_to_indices: dict[tuple[str | None, str | None], list[int]] = defaultdict(list)
    for idx, o in enumerate(offers):
        block_to_indices[offer_block[idx]].append(idx)

    # For each block, find codes shared by >= 2 offers within that block.
    block_stats: list[tuple[tuple[str | None, str | None], int, int, int]] = []
    grand_total_eligible_offers = 0

    for block_key, indices in block_to_indices.items():
        # Count codes within this block.
        block_code_counts: Counter[str] = Counter()
        offers_with_code = 0
        for i in indices:
            o = offers[i]
            if o.model_codes:
                offers_with_code += 1
            for code in o.model_codes:
                block_code_counts[code] += 1

        # Intra-block matchable codes: shared by >= 2 offers in this block.
        matchable = {c for c, n in block_code_counts.items() if n >= 2}
        n_matchable = len(matchable)

        if n_matchable > 0:
            # Count offers that have at least one matchable code.
            eligible = sum(
                1 for i in indices
                if any(c in matchable for c in offers[i].model_codes)
            )
            grand_total_eligible_offers += eligible
        else:
            eligible = 0

        block_stats.append((block_key, len(indices), offers_with_code, n_matchable))

    # Sort by matchable codes descending, top 25.
    block_stats.sort(key=lambda x: x[3], reverse=True)
    print(f"\n  Grand total offers in blocks with >= 1 shared code: "
          f"{grand_total_eligible_offers}")
    print()

    rows = []
    for (cat, brand), total, with_code, matchable in block_stats[:25]:
        rows.append([cat or "(none)", brand or "(none)", total, with_code, matchable])
    _table(
        ["category", "brand", "offers", "has_code", "matchable_codes"],
        rows,
        align=["<", "<", ">", ">", ">"],
    )


# ---------------------------------------------------------------------------
# Section 10: tier 2 + 3 + 4 combined dry-run (behind --tier4 flag)
# ---------------------------------------------------------------------------

def section_tier4(offers: list[EnrichedOffer]) -> None:
    """Combined tier 2 + 3 + 4 dry-run on the SAME UnionFind.

    Applies tiers in order, snapshots state between tier 3 and 4 for delta
    computation, then prints a comprehensive report covering spec filtering,
    block-spread, trusted-code universe, tier-4 groups, combined cluster
    stats, delta analysis, cross-dimension counts, bridging, review signals,
    and sample clusters.

    Strictly read-only — builds a UnionFind in memory, no DB writes.
    """
    try:
        from .tier_mpn_root import mpn_root_edges
        from .tier_mpn import reliable_mpn_edges
        from .union_find import UnionFind
        from .tier_model_code import (
            model_code_edges, model_code_groups, get_precomputed,
            get_spec_blocked_codes, get_series_suffix_blocked_codes,
            is_spec_like_code, is_series_suffix_code, review_signals,
        )
    except ImportError:
        from tier_mpn_root import mpn_root_edges
        from tier_mpn import reliable_mpn_edges
        from union_find import UnionFind
        from tier_model_code import (
            model_code_edges, model_code_groups, get_precomputed,
            get_spec_blocked_codes, get_series_suffix_blocked_codes,
            is_spec_like_code, is_series_suffix_code, review_signals,
        )

    n = len(offers)

    # ==================================================================
    # Apply tiers 2 and 3
    # ==================================================================
    uf = UnionFind(n)
    t2_edges = mpn_root_edges(offers)
    for a, b in t2_edges:
        uf.union(a, b)
    t3_edges = reliable_mpn_edges(offers)
    for a, b in t3_edges:
        uf.union(a, b)

    # Snapshot post-T2+T3 state for delta computation.
    post_t23_root = [uf.find(i) for i in range(n)]
    post_t23_clusters = uf.groups()
    post_t23_ge2 = [c for c in post_t23_clusters if len(c) >= 2]
    post_t23_singletons_set = {c[0] for c in post_t23_clusters if len(c) == 1}

    # ==================================================================
    # Rejected codes: spec-pattern blocklist and series-suffix guard
    # ==================================================================
    _header("10a. Rejected codes")

    # (a) Codes rejected as spec-like (screen sizes, RAM/storage, BTU, etc.)
    spec_blocked = get_spec_blocked_codes(offers)
    print(f"\n  (a) Codes rejected as spec-like: {len(spec_blocked)}")
    print(f"  Examples (up to 20):")
    for code in sorted(spec_blocked)[:20]:
        print(f"    {code}")

    # (b) Codes rejected as series-suffix (digit-prefix + short letter suffix
    #     like 770nc, 520bt — shared across product lines).
    suffix_blocked = get_series_suffix_blocked_codes(offers)
    print(f"\n  (b) Codes rejected as series-suffix: {len(suffix_blocked)}")
    print(f"  Examples (up to 20):")
    for code in sorted(suffix_blocked)[:20]:
        print(f"    {code}")

    # ==================================================================
    # Block-spread stats
    # ==================================================================
    _header("10b. Block-spread filter stats")

    block_spread, freq, chosen = get_precomputed(offers)

    # Codes ignored for appearing in >= 2 blocks.
    multi_block = {c: s for c, s in block_spread.items() if s >= 2}
    print(f"\n  Codes ignored (appear in >= 2 blocks): {len(multi_block)}")
    if multi_block:
        print(f"\n  These codes with their block counts:")
        for code in sorted(multi_block, key=lambda c: (-multi_block[c], c)):
            print(f"    {code:<30s}  blocks={multi_block[code]}")

    # ==================================================================
    # Trusted-code universe
    # ==================================================================
    _header("10c. Trusted-code universe")

    distinct_trusted = len(freq)
    offers_with_code = sum(1 for c in chosen.values() if c is not None)
    print(f"\n  Distinct trusted codes (post spec+spread filter): {distinct_trusted}")
    print(f"  Offers contributing a chosen code: {offers_with_code} / {n}"
          f" ({_pct(offers_with_code, n)})")

    # ==================================================================
    # Tier-4 group stats
    # ==================================================================
    _header("10d. Tier-4 group stats")

    t4_groups = model_code_groups(offers)
    # Count (block, code) groups of size >= 2 and total participating offers.
    groups_ge2_count = 0
    offers_in_groups = 0
    for _block, code_groups in t4_groups.items():
        for _code, members in code_groups.items():
            if len(members) >= 2:
                groups_ge2_count += 1
                offers_in_groups += len(members)

    print(f"\n  (block, code) groups with size >= 2: {groups_ge2_count}")
    print(f"  Offers participating in those groups: {offers_in_groups}")

    # ==================================================================
    # Apply tier-4 edges
    # ==================================================================
    t4_edges = model_code_edges(offers)
    for a, b in t4_edges:
        uf.union(a, b)

    # ==================================================================
    # Combined cluster stats (T2+T3+T4)
    # ==================================================================
    _header("10e. Combined cluster stats (T2+T3+T4)")

    combined_clusters = uf.groups()
    combined_ge2 = [c for c in combined_clusters if len(c) >= 2]
    combined_singletons = len(combined_clusters) - len(combined_ge2)

    print(f"\n  Non-trivial clusters (size >= 2):   {len(combined_ge2)}")
    print(f"  Singletons remaining:               {combined_singletons}")

    # Top 25 largest clusters.
    print()
    sorted_by_size = sorted(combined_ge2, key=len, reverse=True)
    top25 = sorted_by_size[:25]
    rows = [[i, len(c)] for i, c in enumerate(top25, 1)]
    _table(["rank", "size"], rows, align=[">", ">"])

    # ==================================================================
    # Tier-4 delta vs post-T2+T3 snapshot
    # ==================================================================
    _header("10f. Tier-4 delta (what tier 4 added beyond T2+T3)")

    new_clusters = 0
    grew_clusters = 0
    for cluster in combined_ge2:
        member_roots_t23 = {post_t23_root[i] for i in cluster}
        was_any_nontrivial = any(
            r not in post_t23_singletons_set for r in member_roots_t23
        )
        if not was_any_nontrivial:
            # All members were singletons after T2+T3 -> new cluster.
            new_clusters += 1
        else:
            t23_nontrivial_roots = {
                r for r in member_roots_t23 if r not in post_t23_singletons_set
            }
            t23_singleton_members = {
                i for i in cluster if post_t23_root[i] in post_t23_singletons_set
            }
            if len(t23_nontrivial_roots) > 1 or t23_singleton_members:
                grew_clusters += 1

    print(f"\n  New non-trivial clusters (all members were singletons after T2+T3): "
          f"{new_clusters}")
    print(f"  Existing T2+T3 clusters that grew (gained members or merged):       "
          f"{grew_clusters}")

    # ==================================================================
    # Cross-dimension summary stats
    # ==================================================================
    multi_store = 0
    cross_category_clusters: list[list[int]] = []
    cross_brand_clusters: list[list[int]] = []
    for cluster in combined_ge2:
        stores = {offers[i].store for i in cluster}
        cats = {offers[i].category for i in cluster}
        brands = {offers[i].effective_brand for i in cluster
                  if offers[i].effective_brand}
        if len(stores) >= 2:
            multi_store += 1
        if len(cats) >= 2:
            cross_category_clusters.append(cluster)
        if len(brands) >= 2:
            cross_brand_clusters.append(cluster)

    print(f"\n  Multi-store clusters (>= 2 stores):    {multi_store}")
    print(f"  Cross-category clusters (>= 2 cats):   {len(cross_category_clusters)}")
    print(f"  Cross-brand clusters (>= 2 brands):    {len(cross_brand_clusters)}")

    # Build a lookup for chosen codes per offer index for annotation.
    _bs, _fr, chosen_map = get_precomputed(offers)

    # Helper to print full cluster member detail.
    def _print_full_cluster(cluster: list[int]) -> None:
        for idx in cluster:
            o = offers[idx]
            code = chosen_map.get(idx, "")
            code_label = f"  [code: {code}]" if code else ""
            print(f"    store={o.store:<12s} cat={str(o.category):<20s} "
                  f"brand_norm={str(o.brand_norm):<15s} "
                  f"eff_brand={str(o.effective_brand):<15s} "
                  f"vendor={str(o.vendor):<20s}{code_label}")
            print(f"      title: {o.title[:120]}")
            print(f"      url:   {o.product_url[:120]}")

    # ==================================================================
    # Cross-brand clusters — print ALL in full detail for review
    # ==================================================================
    _header("10g. Cross-brand clusters (ALL, full detail)")

    if not cross_brand_clusters:
        print("\n  0 cross-brand clusters. (Good — no false cross-brand merges.)")
    else:
        print(f"\n  {len(cross_brand_clusters)} cross-brand cluster(s):")
        for cluster in cross_brand_clusters:
            brands = sorted({offers[i].brand_norm for i in cluster})
            print(f"\n  --- Cluster (size {len(cluster)}) brands={brands} ---")
            _print_full_cluster(cluster)

    # ==================================================================
    # Cross-category clusters — print ALL in full detail for review
    # ==================================================================
    _header("10h. Cross-category clusters (ALL, full detail)")

    if not cross_category_clusters:
        print("\n  0 cross-category clusters. (Good — no false cross-category merges.)")
    else:
        print(f"\n  {len(cross_category_clusters)} cross-category cluster(s):")
        for cluster in cross_category_clusters:
            cats = sorted({str(offers[i].category) for i in cluster})
            print(f"\n  --- Cluster (size {len(cluster)}) categories={cats} ---")
            _print_full_cluster(cluster)

    # ==================================================================
    # Largest tier-4 clusters — top 25 by size, full detail for top 10
    # ==================================================================
    _header("10i. Largest tier-4 clusters (top 25, full detail for top 10)")

    print()
    top25 = sorted_by_size[:25]
    rows = [[i, len(c)] for i, c in enumerate(top25, 1)]
    _table(["rank", "size"], rows, align=[">", ">"])

    # Full member detail for the top 10 so we can verify no spec/series
    # mega-cluster survived.
    for rank, cluster in enumerate(sorted_by_size[:10], 1):
        stores = {offers[i].store for i in cluster}
        tags = []
        if len(stores) >= 2:
            tags.append("multi-store")
        tag_str = f"  [{', '.join(tags)}]" if tags else ""
        print(f"\n  --- #{rank} (size {len(cluster)}){tag_str} ---")
        _print_full_cluster(cluster)

    # ==================================================================
    # Stephanis <-> Public bridges from tier-4 specifically
    # ==================================================================
    _header("10j. Stephanis <-> Public bridges (tier-4 clusters)")

    step_pub_bridges = 0
    step_pub_bridge_clusters: list[list[int]] = []
    for cluster in combined_ge2:
        stores = {offers[i].store for i in cluster}
        if "stephanis" in stores and "public" in stores:
            stephanis_roots = {post_t23_root[i] for i in cluster
                               if offers[i].store == "stephanis"}
            public_roots = {post_t23_root[i] for i in cluster
                            if offers[i].store == "public"}
            # If stephanis and public had different roots in T2+T3, tier 4
            # created this bridge.
            if not stephanis_roots & public_roots:
                step_pub_bridges += 1
                step_pub_bridge_clusters.append(cluster)

    total_step_pub = sum(
        1 for c in combined_ge2
        if {"stephanis", "public"} <= {offers[i].store for i in c}
    )
    print(f"\n  Total Stephanis <-> Public clusters (T2+T3+T4): {total_step_pub}")
    print(f"  Of those, newly bridged by tier 4:               {step_pub_bridges}")

    # ==================================================================
    # Review-signal stats (clusters of size >= 2 only)
    # ==================================================================
    _header("10k. Review signals")

    signals = review_signals(offers, combined_ge2)

    suspicious_flags = [(c, r) for c, r in signals
                        if r == "unresolved_suspicious_brand"]
    cross_brand_flags = [(c, r) for c, r in signals
                         if r == "cross_effective_brand"]

    print(f"\n  Clusters flagged for unresolved suspicious brand: "
          f"{len(suspicious_flags)}")
    print(f"  Clusters flagged for cross-effective-brand union:  "
          f"{len(cross_brand_flags)}")

    # Diagnostic (informational, not a review flag): offers with empty
    # effective_brand that remain singletons and could not enter tier 4.
    empty_eb_indices = [i for i, o in enumerate(offers) if not o.effective_brand]
    member_set = set()
    for c in combined_ge2:
        member_set.update(c)
    unmatchable = [i for i in empty_eb_indices if i not in member_set]
    unmatchable_with_code = sum(
        1 for i in unmatchable if chosen_map.get(i) is not None
    )
    print(f"\n  Diagnostic (not a review flag):")
    print(f"    Unmatchable offers (empty effective_brand, remain singletons): "
          f"{len(unmatchable)}")
    print(f"    Of those, with >= 1 model code: {unmatchable_with_code}")

    sample_flags = signals[:15]
    if sample_flags:
        print(f"\n  Sample flagged clusters (up to 15):")
        for cluster, reason in sample_flags:
            print(f"\n    reason: {reason}, size: {len(cluster)}")
            for idx in cluster:
                o = offers[idx]
                code = chosen_map.get(idx, "")
                code_label = code if code else "(none)"
                print(f"      store={o.store:<12s} "
                      f"vendor={str(o.vendor):<20s} "
                      f"brand_norm={str(o.brand_norm):<15s} "
                      f"eff_brand={str(o.effective_brand):<15s} "
                      f"suspicious={o.is_suspicious_brand} "
                      f"bft={o.brand_from_title or '(none)'} "
                      f"code={code_label}")
                print(f"        title: {o.title[:120]}")
                print(f"        url:   {o.product_url[:120]}")

    # ==================================================================
    # Sample ~20 tier-4 clusters for manual review
    # ==================================================================
    _header("10l. Sample tier-4 clusters for manual review")

    # Prefer Stephanis <-> Public bridges, then largest clusters.
    sample_clusters: list[list[int]] = []
    seen: set[int] = set()

    # First add bridge clusters.
    for ci, cluster in enumerate(step_pub_bridge_clusters):
        if len(sample_clusters) >= 10:
            break
        idx_in_sorted = next(
            (j for j, c in enumerate(sorted_by_size) if c == cluster), None
        )
        if idx_in_sorted is not None and idx_in_sorted not in seen:
            seen.add(idx_in_sorted)
            sample_clusters.append(cluster)

    # Fill remaining with largest combined clusters.
    for i, cluster in enumerate(sorted_by_size):
        if len(sample_clusters) >= 20:
            break
        if i not in seen:
            seen.add(i)
            sample_clusters.append(cluster)

    if not sample_clusters:
        print("  (no clusters to sample)")
    else:
        for cluster in sample_clusters:
            stores = {offers[i].store for i in cluster}
            tags = []
            if "stephanis" in stores and "public" in stores:
                tags.append("step<->pub bridge")
            if len(stores) >= 2:
                tags.append("multi-store")
            tag_str = f"  [{', '.join(tags)}]" if tags else ""

            print(f"\n  --- Cluster (size {len(cluster)}){tag_str} ---")
            for idx in cluster[:10]:
                o = offers[idx]
                code = chosen_map.get(idx, "")
                code_label = f"  [code: {code}]" if code else ""
                print(f"    store={o.store:<12s} cat={str(o.category):<20s} "
                      f"brand={str(o.effective_brand):<15s}{code_label}")
                print(f"      title: {o.title[:100]}")
                print(f"      url:   {o.product_url[:100]}")
            if len(cluster) > 10:
                print(f"    ... and {len(cluster) - 10} more members")


# ---------------------------------------------------------------------------
# Section 11: full deterministic pipeline (behind --final flag)
# ---------------------------------------------------------------------------
# Applies ALL deterministic tiers (T2 mpn_root + T3 reliable_mpn + T4
# model_code + T5 title_key) in a single UnionFind, then reports final
# cluster statistics, title_key contribution, cross-brand/cross-category
# checks, multi-store coverage, and method breakdown.

def section_final(offers: list[EnrichedOffer]) -> None:
    """Full deterministic pipeline: T2+T3+T4+T5 in one UnionFind.

    Strictly read-only — builds clusters in memory, prints stats, no DB writes.
    """
    try:
        from .tier_mpn_root import mpn_root_edges
        from .tier_mpn import reliable_mpn_edges
        from .tier_model_code import model_code_edges
        from .tier_title_key import title_key_edges
        from .union_find import UnionFind
    except ImportError:
        from tier_mpn_root import mpn_root_edges
        from tier_mpn import reliable_mpn_edges
        from tier_model_code import model_code_edges
        from tier_title_key import title_key_edges
        from union_find import UnionFind

    n = len(offers)

    # ==================================================================
    # Apply tiers 2, 3, and 4 — snapshot before title_key for delta
    # ==================================================================
    uf = UnionFind(n)

    t2_edges = mpn_root_edges(offers)
    for a, b in t2_edges:
        uf.union(a, b)

    t3_edges = reliable_mpn_edges(offers)
    for a, b in t3_edges:
        uf.union(a, b)

    t4_edges = model_code_edges(offers)
    for a, b in t4_edges:
        uf.union(a, b)

    # Snapshot state after T2+T3+T4, before T5 (title_key).
    pre_t5_root = [uf.find(i) for i in range(n)]
    pre_t5_clusters = uf.groups()
    pre_t5_ge2 = [c for c in pre_t5_clusters if len(c) >= 2]
    pre_t5_singletons_set = {c[0] for c in pre_t5_clusters if len(c) == 1}

    # ==================================================================
    # Apply tier 5 (title_key)
    # ==================================================================
    t5_edges = title_key_edges(offers)
    for a, b in t5_edges:
        uf.union(a, b)

    # ==================================================================
    # Final cluster stats
    # ==================================================================
    _header("11a. Final cluster stats (T2+T3+T4+T5)")

    final_clusters = uf.groups()
    final_ge2 = [c for c in final_clusters if len(c) >= 2]
    final_singletons = len(final_clusters) - len(final_ge2)

    print(f"\n  Non-trivial clusters (size >= 2):   {len(final_ge2)}")
    print(f"  Singletons remaining:               {final_singletons}")
    print(f"  Total clusters:                     {len(final_clusters)}")

    # ==================================================================
    # Title_key contribution (T5 delta vs T2+T3+T4)
    # ==================================================================
    _header("11b. Title_key contribution (T5 delta)")

    # Count how many pre-T5 clusters were bridged by title_key edges.
    # A final cluster "bridges" pre-T5 clusters if it spans >= 2 distinct
    # pre-T5 roots.
    bridging_final_clusters = 0
    bridged_pre_t5_roots: set[int] = set()
    for cluster in final_ge2:
        roots = {pre_t5_root[i] for i in cluster}
        if len(roots) >= 2:
            bridging_final_clusters += 1
            bridged_pre_t5_roots.update(roots)

    print(f"\n  Final clusters that bridge >= 2 pre-T5 clusters: "
          f"{bridging_final_clusters}")
    print(f"  Distinct pre-T5 clusters merged by title_key:    "
          f"{len(bridged_pre_t5_roots)}")
    print(f"  Title_key edges emitted: {len(t5_edges)}")

    # ==================================================================
    # Cross-brand clusters (expect 0)
    # ==================================================================
    _header("11c. Cross-brand clusters (by effective_brand)")

    cross_brand_clusters: list[list[int]] = []
    for cluster in final_ge2:
        brands = {offers[i].effective_brand for i in cluster
                  if offers[i].effective_brand}
        if len(brands) >= 2:
            cross_brand_clusters.append(cluster)

    print(f"\n  Cross-brand clusters: {len(cross_brand_clusters)}")

    if cross_brand_clusters:
        for cluster in cross_brand_clusters:
            brands = sorted({offers[i].effective_brand for i in cluster
                             if offers[i].effective_brand})
            print(f"\n  --- Cluster (size {len(cluster)}) brands={brands} ---")
            for idx in cluster:
                o = offers[idx]
                print(f"    store={o.store:<12s} cat={str(o.category):<20s} "
                      f"eff_brand={str(o.effective_brand):<15s}")
                print(f"      title: {o.title[:120]}")
    else:
        print("  (Good — no false cross-brand merges.)")

    # ==================================================================
    # Cross-category clusters (expect ~4 legit Apple cases)
    # ==================================================================
    _header("11d. Cross-category clusters")

    cross_category_clusters: list[list[int]] = []
    for cluster in final_ge2:
        cats = {offers[i].category for i in cluster}
        if len(cats) >= 2:
            cross_category_clusters.append(cluster)

    print(f"\n  Cross-category clusters: {len(cross_category_clusters)}")

    # Print ALL with full titles so the user can verify they are legit.
    for cluster in cross_category_clusters:
        cats = sorted({str(offers[i].category) for i in cluster})
        brands = sorted({str(offers[i].effective_brand) for i in cluster
                         if offers[i].effective_brand})
        print(f"\n  --- Cluster (size {len(cluster)}) categories={cats} "
              f"brands={brands} ---")
        for idx in cluster:
            o = offers[idx]
            print(f"    store={o.store:<12s} cat={str(o.category):<20s} "
                  f"eff_brand={str(o.effective_brand):<15s}")
            print(f"      title: {o.title[:120]}")
            print(f"      url:   {o.product_url[:120]}")

    # ==================================================================
    # Multi-store coverage and largest cluster
    # ==================================================================
    _header("11e. Multi-store coverage")

    multi_store = sum(
        1 for c in final_ge2
        if len({offers[i].store for i in c}) >= 2
    )
    largest_size = max(len(c) for c in final_ge2) if final_ge2 else 0

    print(f"\n  Multi-store clusters (>= 2 stores):  {multi_store}")
    print(f"  Largest cluster size:                {largest_size}")

    # ==================================================================
    # Method breakdown: label each final cluster by strongest contributor
    # ==================================================================
    # Priority: ean > mpn_root > mpn > model_code > title_key.
    # For each cluster, check whether ANY edge from a given tier contributed
    # to its formation. The highest-priority tier with at least one edge
    # inside the cluster determines its label.
    _header("11f. Method breakdown (strongest contributing tier per cluster)")

    # Build sets of offer-index pairs connected by each tier, for quick lookup.
    # We store frozensets of pairs so lookup is O(1).
    ean_pairs: set[frozenset[int]] = set()
    t2_pair_set: set[frozenset[int]] = {frozenset(e) for e in t2_edges}
    t3_pair_set: set[frozenset[int]] = {frozenset(e) for e in t3_edges}
    t4_pair_set: set[frozenset[int]] = {frozenset(e) for e in t4_edges}
    t5_pair_set: set[frozenset[int]] = {frozenset(e) for e in t5_edges}

    # EAN tier (tier 1): offers sharing the same ean_key are implicitly
    # connected. Build explicit pairs for clusters where ean_key matches.
    ean_groups: dict[str, list[int]] = defaultdict(list)
    for idx, o in enumerate(offers):
        if o.ean_key:
            ean_groups[o.ean_key].append(idx)
    for _key, members in ean_groups.items():
        if len(members) >= 2:
            for m in members[1:]:
                ean_pairs.add(frozenset([members[0], m]))

    method_counts: Counter = Counter()

    for cluster in final_ge2:
        # Build all index pairs within the cluster to check against tier edges.
        # For large clusters this is O(n^2), but largest is ~30 so it's fine.
        cluster_pairs: set[frozenset[int]] = set()
        cluster_list = list(cluster)
        for i in range(len(cluster_list)):
            for j in range(i + 1, len(cluster_list)):
                cluster_pairs.add(frozenset([cluster_list[i], cluster_list[j]]))

        # Check tiers in priority order — first match wins.
        if cluster_pairs & ean_pairs:
            method_counts["ean"] += 1
        elif cluster_pairs & t2_pair_set:
            method_counts["mpn_root"] += 1
        elif cluster_pairs & t3_pair_set:
            method_counts["mpn"] += 1
        elif cluster_pairs & t4_pair_set:
            method_counts["model_code"] += 1
        elif cluster_pairs & t5_pair_set:
            method_counts["title_key"] += 1
        else:
            # Should not happen — every cluster with size >= 2 must have
            # at least one edge from some tier.
            method_counts["unknown"] += 1

    print(f"\n  Method labeling: each cluster is tagged by its highest-priority")
    print(f"  contributing tier (ean > mpn_root > mpn > model_code > title_key).\n")

    rows = []
    for method in ["ean", "mpn_root", "mpn", "model_code", "title_key", "unknown"]:
        cnt = method_counts.get(method, 0)
        rows.append([method, cnt, _pct(cnt, len(final_ge2))])
    _table(["method", "clusters", "%"], rows, align=["<", ">", ">"])

    print(f"\n  Total non-trivial clusters: {len(final_ge2)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Load all enriched offers and print the statistics report."""
    parser = argparse.ArgumentParser(
        description="Read-only offer statistics report.",
    )
    parser.add_argument(
        "--mpn-root",
        action="store_true",
        help="Also run the tier-2 (mpn_root) dry-run clustering section.",
    )
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help="Run combined tier 2 + tier 3 dry-run clustering.",
    )
    parser.add_argument(
        "--model-codes",
        action="store_true",
        help="Run model-code trust analysis for tier-4 planning.",
    )
    parser.add_argument(
        "--tier4",
        action="store_true",
        help="Run combined tier 2+3+4 dry-run clustering.",
    )
    parser.add_argument(
        "--final",
        action="store_true",
        help="Run full deterministic pipeline (T2+T3+T4+T5 title_key).",
    )
    args = parser.parse_args()

    conn = None
    try:
        conn = get_connection()
        offers = load_offers(conn)
    finally:
        if conn is not None:
            conn.close()

    # Run the default report sections.
    by_store = section_totals(offers)
    section_coverage(offers, by_store)
    section_block_distribution(offers)
    section_largest_blocks(offers)
    section_suspicious_brands(offers)
    section_model_code_frequency(offers)

    # Optional tier dry-run sections.
    if args.mpn_root:
        section_mpn_root_tier(offers)
    if args.deterministic:
        section_deterministic_tiers(offers)
    if args.model_codes:
        section_model_codes(offers)
    if args.tier4:
        section_tier4(offers)
    if args.final:
        section_final(offers)

    print()
    print("Done. This script performed read-only queries only.")


if __name__ == "__main__":
    main()
