#!/usr/bin/env python3
"""
dump_clusters.py
----------------
Read-only CLI that snapshots the clusters produced by the current matching
pipeline into a JSON file, so that two versions of the pipeline can be
compared with diff_clusters.py before any tier change is committed.

Loads all enriched offers (SELECT only), builds the SAME deterministic
clusters as inspect_match_keys.py (tiers 2-5 in one UnionFind), attaches a
match_method and match_key to every cluster, and writes a fully sorted JSON
document. Two runs against the same database state produce byte-for-byte
identical files.

Contains NO database writes. The only thing written is the output JSON file.

Usage:
    python3 -m matching.dump_clusters data/clusters/baseline.json
"""

import argparse
import json
import os
from collections import defaultdict

# Cluster construction and per-cluster attribution are imported from
# inspect_match_keys so this snapshot always reflects the real pipeline;
# nothing here re-implements tier logic.
from .inspect_match_keys import (
    _build_full_pipeline,
    _cluster_match_method,
    _most_common_tiebreak,
    propose_match_key,
)
from .load import EnrichedOffer, get_connection, load_offers
from .tier_model_code import get_precomputed


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _load_all_offers() -> list[EnrichedOffer]:
    """Open the DB, SELECT every offer, and close the connection again.

    The connection is closed before any clustering starts, so the rest of
    the script cannot touch the database even by accident.
    """
    conn = None
    try:
        conn = get_connection()
        return load_offers(conn)
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# Tier pair sets (used to attribute a match_method to each cluster)
# ---------------------------------------------------------------------------

def _build_pair_sets(
    offers: list[EnrichedOffer],
    t2_edges: list[tuple[int, int]],
    t3_edges: list[tuple[int, int]],
    t4_edges: list[tuple[int, int]],
    t5_edges: list[tuple[int, int]],
) -> tuple[
    set[frozenset[int]],  # ean pairs
    set[frozenset[int]],  # t2 mpn_root pairs
    set[frozenset[int]],  # t3 mpn pairs
    set[frozenset[int]],  # t4 model_code pairs
    set[frozenset[int]],  # t5 title_key pairs
]:
    """Build the per-tier pair sets consumed by _cluster_match_method().

    This mirrors the construction inside inspect_match_keys.main() exactly
    (that code is inline in main(), so it cannot be imported). If main()
    changes how pair sets are built, update this function to match.
    """
    # EAN tier (tier 1): offers sharing the same ean_key are implicitly
    # connected. Each group is expressed as a star of pairs anchored on its
    # first member, just like main() does.
    ean_groups: dict[str, list[int]] = defaultdict(list)
    for idx, o in enumerate(offers):
        if o.ean_key:
            ean_groups[o.ean_key].append(idx)
    ean_pair_set: set[frozenset[int]] = set()
    for members in ean_groups.values():
        if len(members) >= 2:
            for m in members[1:]:
                ean_pair_set.add(frozenset([members[0], m]))

    # Tiers 2-5: every edge the tier emitted becomes an unordered pair.
    t2_pair_set = {frozenset(e) for e in t2_edges}
    t3_pair_set = {frozenset(e) for e in t3_edges}
    t4_pair_set = {frozenset(e) for e in t4_edges}
    t5_pair_set = {frozenset(e) for e in t5_edges}

    return ean_pair_set, t2_pair_set, t3_pair_set, t4_pair_set, t5_pair_set


# ---------------------------------------------------------------------------
# Snapshot document construction
# ---------------------------------------------------------------------------

def _member_key(offer: EnrichedOffer) -> str:
    """Return the stable identity of an offer: '<store>:<store_product_id>'."""
    return f"{offer.store}:{offer.store_product_id}"


def _cluster_record(
    cluster: list[int],
    offers: list[EnrichedOffer],
    match_method: str,
    match_key: str,
) -> dict:
    """Turn one cluster (a list of offer indices) into its JSON record.

    Everything inside the record is sorted so the output does not depend on
    offer index order or on set iteration order.
    """
    members = sorted(
        (
            {
                "key": _member_key(offers[i]),
                "store": offers[i].store,
                "title": offers[i].title,
                # price is a Decimal in the DB; JSON needs a float (or null).
                "price": float(offers[i].price) if offers[i].price is not None else None,
            }
            for i in cluster
        ),
        key=lambda m: m["key"],
    )

    # Representative category: most common non-empty category, tie-broken
    # shortest-then-lexicographic (same helper the match_key logic uses).
    categories = [offers[i].category for i in cluster if offers[i].category]
    category = _most_common_tiebreak(categories) if categories else ""

    return {
        # The smallest member key is a cluster id that stays the same across
        # runs and pipeline versions as long as that member stays in it.
        "id": members[0]["key"],
        "match_method": match_method,
        "match_key": match_key,
        "category": category,
        "stores": sorted({offers[i].store for i in cluster}),
        "members": members,
    }


def build_document(offers: list[EnrichedOffer]) -> dict:
    """Run the full clustering pipeline and return the snapshot document."""
    # Clusters: the exact tier 2-5 pipeline from inspect_match_keys.
    uf, t2_edges, t3_edges, t4_edges, t5_edges = _build_full_pipeline(offers)
    all_clusters = uf.groups()

    # Inputs needed to attribute match_method and propose match_key.
    pair_sets = _build_pair_sets(offers, t2_edges, t3_edges, t4_edges, t5_edges)
    _block_spread, _freq, chosen_model_codes = get_precomputed(offers)

    records: list[dict] = []
    for cluster in all_clusters:
        method, contribs = _cluster_match_method(cluster, *pair_sets)
        match_key, _kind = propose_match_key(
            cluster, offers, contribs, chosen_model_codes,
        )
        records.append(_cluster_record(cluster, offers, method, match_key))

    # Clusters are ordered by their stable id, not by offer index.
    records.sort(key=lambda r: r["id"])

    return {
        "offer_count": len(offers),
        "cluster_count": len(records),
        "clusters": records,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_document(doc: dict, path: str) -> None:
    """Serialize the document deterministically and write it to *path*.

    sort_keys + fixed indent + ensure_ascii=False gives byte-identical output
    for identical input (Greek titles are kept readable, not \\u-escaped).
    The parent directory is created if it does not exist yet.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    text = json.dumps(doc, sort_keys=True, indent=1, ensure_ascii=False)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.write("\n")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only: snapshot the current pipeline's clusters to JSON.",
    )
    parser.add_argument(
        "output",
        help="Path of the JSON file to write (parent dirs are created).",
    )
    args = parser.parse_args()

    offers = _load_all_offers()
    doc = build_document(offers)
    write_document(doc, args.output)

    # One-line summary, identical across runs on the same DB state.
    multi_store = sum(1 for c in doc["clusters"] if len(c["stores"]) >= 2)
    print(
        f"offers={doc['offer_count']} "
        f"clusters={doc['cluster_count']} "
        f"multi_store_clusters={multi_store}"
    )


if __name__ == "__main__":
    main()
