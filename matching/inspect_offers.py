#!/usr/bin/env python3
"""
inspect_offers.py
-----------------
Strictly read-only CLI that loads all enriched offers and prints a
detailed statistics report to stdout.

Contains NO database writes. The default report (sections 1-6) contains
no matching logic. The --mpn-root flag adds a tier-2 dry-run section that
computes mpn_root clusters in memory and prints statistics — still
read-only, no writes.

Safe to run repeatedly.

Usage:
    python -m matching.inspect_offers             # default report
    python -m matching.inspect_offers --mpn-root  # also run tier-2 dry-run
"""

import argparse
from collections import Counter, defaultdict

from .load import EnrichedOffer, get_connection, load_offers


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
    from .tier_mpn_root import mpn_root_edges, mpn_root_groups
    from .union_find import UnionFind

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

    # Optional tier-2 dry-run section.
    if args.mpn_root:
        section_mpn_root_tier(offers)

    print()
    print("Done. This script performed read-only queries only.")


if __name__ == "__main__":
    main()
