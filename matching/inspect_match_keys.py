#!/usr/bin/env python3
"""
inspect_match_keys.py
---------------------
Read-only inspection CLI that assesses match_key COLLISION RISK before
building the canonical writer. This is a go/no-go gate.

Loads all enriched offers, builds the SAME deterministic clusters as
inspect_offers.py --final (tiers 2-5 in one UnionFind), then proposes a
match_key for every cluster and checks for collisions: cases where two
distinct clusters would receive the same match_key and be wrongly merged.

Contains NO database writes, NO file writes, NO matcher logic.
Only SELECT queries against the DB.

Usage:
    python -m matching.inspect_match_keys [--samples N]
"""

import argparse
import re
from collections import Counter, defaultdict

# Support running both as a package module and directly.
try:
    from .load import EnrichedOffer, get_connection, load_offers
    from .tier_mpn_root import mpn_root_edges
    from .tier_mpn import reliable_mpn_edges
    from .tier_model_code import model_code_edges, get_precomputed
    from .tier_title_key import title_key_edges
    from .union_find import UnionFind
except ImportError:
    from load import EnrichedOffer, get_connection, load_offers
    from tier_mpn_root import mpn_root_edges
    from tier_mpn import reliable_mpn_edges
    from tier_model_code import model_code_edges, get_precomputed
    from tier_title_key import title_key_edges
    from union_find import UnionFind


# ---------------------------------------------------------------------------
# Formatting helpers (same style as inspect_offers.py)
# ---------------------------------------------------------------------------

def _header(title: str) -> None:
    """Print a section header with a visual separator."""
    print()
    print("=" * 90)
    print(f"  {title}")
    print("=" * 90)


def _pct(part: int, total: int) -> str:
    if total == 0:
        return "0.0%"
    return f"{100 * part / total:.1f}%"


# ---------------------------------------------------------------------------
# Cluster construction — exact same pipeline as inspect_offers.py --final
# ---------------------------------------------------------------------------

def _build_full_pipeline(
    offers: list[EnrichedOffer],
) -> tuple[
    UnionFind,
    list[tuple[int, int]],   # t2 edges
    list[tuple[int, int]],   # t3 edges
    list[tuple[int, int]],   # t4 edges
    list[tuple[int, int]],   # t5 edges
]:
    """Apply all deterministic tiers (T2-T5) to a fresh UnionFind.

    Returns the UnionFind and every tier's edge list so the caller can
    determine which tiers contributed to each cluster.
    """
    n = len(offers)
    uf = UnionFind(n)

    t2 = mpn_root_edges(offers)
    for a, b in t2:
        uf.union(a, b)

    t3 = reliable_mpn_edges(offers)
    for a, b in t3:
        uf.union(a, b)

    t4 = model_code_edges(offers)
    for a, b in t4:
        uf.union(a, b)

    t5 = title_key_edges(offers)
    for a, b in t5:
        uf.union(a, b)

    return uf, t2, t3, t4, t5


# ---------------------------------------------------------------------------
# STEP 1: per-cluster contributing tiers + match_method
# ---------------------------------------------------------------------------
# For each cluster, determine which tiers contributed at least one edge
# whose BOTH endpoints lie inside that cluster.
# Priority: ean > mpn_root > mpn > model_code > title_key.
# Singletons (size == 1) -> "unmatched".

def _cluster_match_method(
    cluster: list[int],
    ean_pair_set: set[frozenset[int]],
    t2_pair_set: set[frozenset[int]],
    t3_pair_set: set[frozenset[int]],
    t4_pair_set: set[frozenset[int]],
    t5_pair_set: set[frozenset[int]],
) -> tuple[str, set[str]]:
    """Return (match_method, contributing_tiers) for a cluster.

    match_method is the highest-priority tier that contributed an edge.
    contributing_tiers is the full set of tiers that contributed.
    Singletons return ("unmatched", set()).
    """
    if len(cluster) < 2:
        return "unmatched", set()

    # Build all intra-cluster pairs.
    pairs: set[frozenset[int]] = set()
    for i in range(len(cluster)):
        for j in range(i + 1, len(cluster)):
            pairs.add(frozenset([cluster[i], cluster[j]]))

    contribs: set[str] = set()
    if pairs & ean_pair_set:
        contribs.add("ean")
    if pairs & t2_pair_set:
        contribs.add("mpn_root")
    if pairs & t3_pair_set:
        contribs.add("mpn")
    if pairs & t4_pair_set:
        contribs.add("model_code")
    if pairs & t5_pair_set:
        contribs.add("title")

    # Pick the highest-priority contributing tier as match_method.
    for method in ["ean", "mpn_root", "mpn", "model_code", "title"]:
        if method in contribs:
            return method, contribs

    # Should not happen for size >= 2 clusters, but guard.
    return "unknown", contribs


# ---------------------------------------------------------------------------
# STEP 2: propose match_key per cluster
# ---------------------------------------------------------------------------
# Deterministic match_key proposal following the LOCKED logic from the spec.
#
# WHY title-based keys for multi-color clusters:
# Color-specific EAN and Apple part numbers change if a color variant sells
# out or a new color is added. Using them as the sticky identity would break
# re-identification on warm runs. The title_key is color-invariant — it
# hashes the normalized title AFTER color stripping — making it stable
# across color changes. So clusters that span multiple color variants
# (detected by title_key tier contribution or multiple distinct EAN/mpn_root
# values) always use the title-based key.

def _most_common_tiebreak(values: list[str], tiebreak: str = "lex") -> str:
    """Return the most frequent value; tie-break by shortest then lexicographic.

    If tiebreak == "lex", ties are broken: shortest string first, then
    alphabetically.
    """
    counts = Counter(values)
    return sorted(
        counts.keys(),
        key=lambda v: (-counts[v], len(v), v),
    )[0]


def propose_match_key(
    cluster: list[int],
    offers: list[EnrichedOffer],
    contributing_tiers: set[str],
    chosen_model_codes: dict[int, str | None],
) -> tuple[str, str]:
    """Return (match_key, key_kind) for a cluster.

    The logic determines whether the cluster is "title-stable" (needs a
    color-invariant title-based key) or "identifier-tight" (can use a
    specific identifier as the key).
    """
    # Collect distinct non-null identifiers from cluster members.
    mpn_roots = {offers[i].mpn_root for i in cluster if offers[i].mpn_root}
    eans = {offers[i].ean for i in cluster if offers[i].ean}

    # is_title_stable: true if title_key tier contributed (multi-color cluster),
    # or if the cluster spans multiple distinct mpn_roots or EANs (meaning
    # different color-specific part numbers are grouped together).
    is_title_stable = (
        "title" in contributing_tiers
        or len(mpn_roots) > 1
        or len(eans) > 1
    )

    if is_title_stable:
        return _title_based_key(cluster, offers)

    # Tight identifier-only cluster. Try identifiers in priority order.
    if len(eans) == 1:
        ean_val = next(iter(eans))
        return f"ean|{ean_val.strip().lower()}", "ean"

    if len(mpn_roots) == 1:
        mpn_root_val = next(iter(mpn_roots))
        eb = _representative_brand(cluster, offers)
        return f"mpnroot|{eb}|{mpn_root_val.strip().lower()}", "mpn_root"

    # Check for a single reliable MPN across the cluster.
    reliable_mpns = {
        offers[i].mpn for i in cluster
        if offers[i].mpn and offers[i].identifier_source in ("sku", "api")
    }
    if len(reliable_mpns) == 1:
        mpn_val = next(iter(reliable_mpns))
        eb = _representative_brand(cluster, offers)
        return f"mpn|{eb}|{mpn_val.strip().lower()}", "mpn"

    # Check for a discriminative model code shared in the cluster.
    codes_in_cluster = {
        chosen_model_codes[i] for i in cluster
        if chosen_model_codes.get(i)
    }
    if len(codes_in_cluster) == 1:
        code_val = next(iter(codes_in_cluster))
        eb = _representative_brand(cluster, offers)
        return f"model|{eb}|{code_val.strip().lower()}", "model_code"

    # Fallback: use the title-based key even for identifier clusters where
    # no single shared identifier exists.
    return _title_based_key(cluster, offers)


def _title_based_key(
    cluster: list[int],
    offers: list[EnrichedOffer],
) -> tuple[str, str]:
    """Build a title-based match_key from the most common title_key in the cluster."""
    # Pick representative title_key: most frequent, tie-break shortest then lex.
    title_keys = [offers[i].title_key for i in cluster if offers[i].title_key]
    if not title_keys:
        # Very rare fallback: no title_key at all. Use store_product_id of first member.
        return f"singleton|{offers[cluster[0]].store}|{offers[cluster[0]].store_product_id}", "singleton"

    rep_tk = _most_common_tiebreak(title_keys)

    # Category and brand: most common, tie-break lexicographic.
    cats = [offers[i].category or "" for i in cluster]
    rep_cat = _most_common_tiebreak(cats)

    eb = _representative_brand(cluster, offers)

    return f"title|{rep_cat}|{eb}|{rep_tk}", "title"


def _representative_brand(cluster: list[int], offers: list[EnrichedOffer]) -> str:
    """Pick the representative effective_brand for a cluster.

    Most common non-empty brand; tie-break by shortest then lexicographic.
    Falls back to empty string if all brands are empty.
    """
    brands = [offers[i].effective_brand for i in cluster if offers[i].effective_brand]
    if not brands:
        return ""
    return _most_common_tiebreak(brands).lower()


# ---------------------------------------------------------------------------
# Language detection heuristic for STEP 5
# ---------------------------------------------------------------------------

# Simple heuristic: if the title contains Greek Unicode characters, it's EL.
_GREEK_RE = re.compile(r"[\u0370-\u03FF\u1F00-\u1FFF]")


def _detect_lang(title: str) -> str:
    """Return 'EL' if title contains Greek characters, else 'EN'."""
    return "EL" if _GREEK_RE.search(title) else "EN"


# ---------------------------------------------------------------------------
# MAIN REPORT
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only match_key collision risk assessment.",
    )
    parser.add_argument(
        "--samples", type=int, default=15,
        help="Max sample count for collision, multi-color, and title reports.",
    )
    args = parser.parse_args()
    max_samples = args.samples

    # =====================================================================
    # Load offers
    # =====================================================================
    conn = None
    try:
        conn = get_connection()
        offers = load_offers(conn)
    finally:
        if conn is not None:
            # Keep conn open for STEP 6 warm-run simulation.
            pass

    print(f"Loaded {len(offers)} offers.")

    # =====================================================================
    # Build clusters — exact same pipeline as inspect_offers.py --final
    # =====================================================================
    uf, t2_edges, t3_edges, t4_edges, t5_edges = _build_full_pipeline(offers)
    all_clusters = uf.groups()

    # =====================================================================
    # Build tier-edge pair sets for method attribution
    # =====================================================================
    # EAN tier (tier 1): offers sharing the same ean_key are implicitly
    # connected. Build explicit pairs.
    ean_groups: dict[str, list[int]] = defaultdict(list)
    for idx, o in enumerate(offers):
        if o.ean_key:
            ean_groups[o.ean_key].append(idx)
    ean_pair_set: set[frozenset[int]] = set()
    for members in ean_groups.values():
        if len(members) >= 2:
            for m in members[1:]:
                ean_pair_set.add(frozenset([members[0], m]))

    t2_pair_set = {frozenset(e) for e in t2_edges}
    t3_pair_set = {frozenset(e) for e in t3_edges}
    t4_pair_set = {frozenset(e) for e in t4_edges}
    t5_pair_set = {frozenset(e) for e in t5_edges}

    # Get the discriminative model code chosen per offer (for match_key proposals).
    _bs, _fr, chosen_model_codes = get_precomputed(offers)

    # =====================================================================
    # STEP 1 + 2: compute match_method and propose match_key for every cluster
    # =====================================================================
    _header("STEP 1+2: match_method + match_key proposals")

    # Per-cluster results.
    cluster_methods: list[str] = []           # match_method per cluster
    cluster_keys: list[tuple[str, str]] = []  # (match_key, key_kind) per cluster
    cluster_contribs: list[set[str]] = []     # contributing tiers per cluster

    for cluster in all_clusters:
        method, contribs = _cluster_match_method(
            cluster, ean_pair_set, t2_pair_set, t3_pair_set,
            t4_pair_set, t5_pair_set,
        )
        cluster_methods.append(method)
        cluster_contribs.append(contribs)

        key, kind = propose_match_key(cluster, offers, contribs, chosen_model_codes)
        cluster_keys.append((key, kind))

    # =====================================================================
    # STEP 3: COLLISION ANALYSIS
    # =====================================================================
    _header("STEP 3: Collision analysis (go/no-go)")

    total_clusters = len(all_clusters)
    total_offers = len(offers)

    # Group clusters by proposed match_key.
    key_to_cluster_indices: dict[str, list[int]] = defaultdict(list)
    for ci, (key, _kind) in enumerate(cluster_keys):
        key_to_cluster_indices[key].append(ci)

    total_distinct_keys = len(key_to_cluster_indices)

    # Collisions: one match_key -> >= 2 distinct clusters.
    collisions: dict[str, list[int]] = {
        key: cis for key, cis in key_to_cluster_indices.items()
        if len(cis) >= 2
    }
    n_colliding_keys = len(collisions)
    n_colliding_clusters = sum(len(cis) for cis in collisions.values())

    print(f"\n  Total offers loaded:         {total_offers}")
    print(f"  Total clusters (incl singletons): {total_clusters}")
    print(f"  Total distinct match_keys:   {total_distinct_keys}")
    print(f"  Colliding match_keys:        {n_colliding_keys}")
    print(f"  Clusters involved in collisions: {n_colliding_clusters}")

    # match_method distribution.
    method_counts = Counter(cluster_methods)
    print(f"\n  match_method distribution:")
    for method in ["ean", "mpn_root", "mpn", "model_code", "title", "unmatched", "unknown"]:
        cnt = method_counts.get(method, 0)
        if cnt > 0:
            print(f"    {method:<15s} {cnt:>6d}  ({_pct(cnt, total_clusters)})")

    # key_kind distribution.
    kind_counts = Counter(kind for _key, kind in cluster_keys)
    print(f"\n  key_kind distribution:")
    for kind in ["ean", "mpn_root", "mpn", "model_code", "title", "singleton"]:
        cnt = kind_counts.get(kind, 0)
        if cnt > 0:
            print(f"    {kind:<15s} {cnt:>6d}  ({_pct(cnt, total_clusters)})")

    # Print collision samples.
    if n_colliding_keys == 0:
        print(f"\n  ** NO COLLISIONS — go/no-go: GO **")
    else:
        print(f"\n  ** {n_colliding_keys} COLLISIONS FOUND — go/no-go: REVIEW NEEDED **")
        _header("Collision samples")
        shown = 0
        for key, cis in sorted(collisions.items()):
            if shown >= max_samples:
                break
            shown += 1
            print(f"\n  match_key: {key}")
            print(f"  clusters sharing this key: {len(cis)}")
            for ci in cis:
                cluster = all_clusters[ci]
                print(f"    cluster size={len(cluster)}, "
                      f"method={cluster_methods[ci]}, "
                      f"kind={cluster_keys[ci][1]}")
                for idx in cluster[:4]:
                    o = offers[idx]
                    code = chosen_model_codes.get(idx, "")
                    print(f"      {o.store:<12s} | {str(o.category):<18s} | "
                          f"{str(o.effective_brand):<15s} | "
                          f"{o.title[:60]:<60s} | "
                          f"ean={o.ean or '-':<15s} | "
                          f"mpn_root={o.mpn_root or '-':<15s} | "
                          f"mpn={o.mpn or '-':<15s} | "
                          f"code={code or '-'}")
                if len(cluster) > 4:
                    print(f"      ... and {len(cluster) - 4} more")

    # =====================================================================
    # STEP 4: title-stable / multi-color report
    # =====================================================================
    _header("STEP 4: Title-stable multi-color clusters")

    # Clusters that are title-stable AND contain >1 distinct mpn_root or ean.
    multi_color_samples: list[int] = []
    for ci, cluster in enumerate(all_clusters):
        if len(cluster) < 2:
            continue
        if cluster_keys[ci][1] != "title":
            continue
        mpn_roots = {offers[i].mpn_root for i in cluster if offers[i].mpn_root}
        eans = {offers[i].ean for i in cluster if offers[i].ean}
        if len(mpn_roots) > 1 or len(eans) > 1:
            multi_color_samples.append(ci)

    print(f"\n  Multi-color title-stable clusters: {len(multi_color_samples)}")

    shown = 0
    for ci in multi_color_samples:
        if shown >= max_samples:
            break
        shown += 1
        cluster = all_clusters[ci]
        key, kind = cluster_keys[ci]
        mpn_roots = {offers[i].mpn_root for i in cluster if offers[i].mpn_root}
        eans = {offers[i].ean for i in cluster if offers[i].ean}
        stores = {offers[i].store for i in cluster}
        print(f"\n  match_key: {key}")
        print(f"  members: {len(cluster)}, stores: {sorted(stores)}")
        print(f"  distinct mpn_roots: {len(mpn_roots)} -> {sorted(mpn_roots)[:6]}"
              f"{'...' if len(mpn_roots) > 6 else ''}")
        print(f"  distinct eans: {len(eans)} -> {sorted(eans)[:6]}"
              f"{'...' if len(eans) > 6 else ''}")

    # =====================================================================
    # STEP 5: representative title preview
    # =====================================================================
    _header("STEP 5: Representative title preview (multi-store clusters)")

    # Sample multi-store clusters.
    multi_store_cis: list[int] = []
    for ci, cluster in enumerate(all_clusters):
        if len(cluster) < 2:
            continue
        stores = {offers[i].store for i in cluster}
        if len(stores) >= 2:
            multi_store_cis.append(ci)

    print(f"\n  Multi-store clusters: {len(multi_store_cis)}")
    print(f"  Sampling up to {max_samples}:\n")

    el_only_count = 0
    shown = 0
    for ci in multi_store_cis:
        if shown >= max_samples:
            break
        shown += 1
        cluster = all_clusters[ci]
        key, kind = cluster_keys[ci]

        # Gather candidate titles with store and language.
        candidates: list[tuple[str, str, str]] = []  # (store, lang, title)
        for idx in cluster:
            o = offers[idx]
            lang = _detect_lang(o.title)
            candidates.append((o.store, lang, o.title))

        # EN-preference rule: prefer EN titles; if none, pick any.
        en_titles = [(s, t) for s, lang, t in candidates if lang == "EN"]
        if en_titles:
            chosen_title = en_titles[0][1]
            chosen_store = en_titles[0][0]
            flag = ""
        else:
            chosen_title = candidates[0][2]
            chosen_store = candidates[0][0]
            flag = " ** EL-only **"
            el_only_count += 1

        print(f"  match_key: {key}")
        print(f"  chosen title ({chosen_store}, {_detect_lang(chosen_title)}): "
              f"{chosen_title[:100]}{flag}")
        # Show all candidate languages.
        lang_counts = Counter(lang for _, lang, _ in candidates)
        print(f"  candidate languages: {dict(lang_counts)}")
        print()

    # Count total EL-only multi-store clusters.
    total_el_only = 0
    for ci in multi_store_cis:
        cluster = all_clusters[ci]
        langs = {_detect_lang(offers[i].title) for i in cluster}
        if langs == {"EL"}:
            total_el_only += 1

    print(f"  Total multi-store clusters with EL-only titles: {total_el_only}")

    # =====================================================================
    # STEP 6: warm-run lookup simulation (read-only)
    # =====================================================================
    _header("STEP 6: Warm-run lookup simulation")

    # Check if the products table exists and has any rows with match_key set.
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM products WHERE match_key IS NOT NULL"
        )
        existing_count = cur.fetchone()[0]
    except Exception:
        # Table might not exist, or connection closed, etc.
        existing_count = 0
        print("\n  Could not query products table (may not exist yet).")

    if existing_count == 0:
        print(f"\n  Products table has 0 rows with match_key set.")
        print(f"  This is a COLD START — all {total_clusters} clusters would create new canonicals.")
        print(f"  No ambiguous re-identification possible on cold start.")
    else:
        # Warm run: check each proposed match_key against existing products.
        print(f"\n  Products table has {existing_count} rows with match_key set.")

        # Load all existing match_keys.
        cur.execute("SELECT match_key FROM products WHERE match_key IS NOT NULL")
        existing_keys = {row[0] for row in cur.fetchall()}

        would_attach = 0
        would_create = 0
        for key, _kind in cluster_keys:
            if key in existing_keys:
                would_attach += 1
            else:
                would_create += 1

        print(f"  would_attach_existing: {would_attach}")
        print(f"  would_create_new:      {would_create}")

        # Check for ambiguous re-identification: one proposed cluster matches
        # >1 existing canonical.
        # (This can only happen if the products table has duplicate match_keys,
        # which should not happen, but check anyway.)
        cur.execute(
            "SELECT match_key, COUNT(*) FROM products "
            "WHERE match_key IS NOT NULL GROUP BY match_key HAVING COUNT(*) > 1"
        )
        dupes = cur.fetchall()
        if dupes:
            print(f"\n  WARNING: {len(dupes)} existing match_keys appear >1 time in products!")
            for mk, cnt in dupes[:10]:
                print(f"    {mk}: {cnt} rows")
        else:
            print(f"  No duplicate match_keys in existing products table.")

    # Close connection.
    if conn is not None:
        conn.close()

    print()
    print("Done. This script performed read-only queries only.")


if __name__ == "__main__":
    main()
