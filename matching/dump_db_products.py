#!/usr/bin/env python3
"""
dump_db_products.py
-------------------
Read-only CLI that snapshots the CURRENT product grouping stored in the
database (products + the store_products offers linked to them) into a JSON
file with the same shape that dump_clusters.py writes.

That lets diff_clusters.py compare "what the database holds together today"
against "what the matching pipeline would build":

    python3 -m matching.dump_db_products data/clusters/db.json
    python3 -m matching.dump_clusters    data/clusters/candidate.json
    python3 -m matching.diff_clusters    data/clusters/db.json \\
        data/clusters/candidate.json

Every product that has at least one offer becomes one cluster. Clusters use
the same id convention as dump_clusters.py (the lexicographically smallest
member key "<store>:<store_product_id>"), so a product whose offers match a
pipeline cluster exactly gets the same id on both sides.

Contains NO database writes: the connection is put in read-only mode, only
SELECT statements are run, and the connection is closed before the document
is built. The only thing written is the output JSON file.

Usage:
    python3 -m matching.dump_db_products data/clusters/db.json
"""

import argparse
import json
import os
from collections import defaultdict

# Only the connection helper is imported. dump_clusters is deliberately NOT
# imported: it pulls in the whole tier pipeline, which this DB-only tool does
# not need. The member-key format and JSON writer below mirror it instead.
from .load import get_connection


# ---------------------------------------------------------------------------
# SQL (SELECT only)
# ---------------------------------------------------------------------------

# One row per product. Products without offers are fetched too but dropped
# later, because a cluster with no members has no id and cannot be diffed.
_PRODUCTS_SQL = """
SELECT id,
       canonical_title,
       category,
       match_method,
       match_key,
       store_count,
       offer_count,
       needs_review,
       review_reason
FROM   products
"""

# One row per offer that is linked to a product. Unlinked offers are not
# part of any DB cluster, so they are left out (diff_clusters reports them
# as "added" when the candidate side does cluster them).
_OFFERS_SQL = """
SELECT product_id,
       store,
       store_product_id,
       title,
       current_price
FROM   store_products
WHERE  product_id IS NOT NULL
"""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _load_rows() -> tuple[list[dict], list[dict]]:
    """Open the DB, run the two SELECTs, and close the connection again.

    Returns (products, offers), each a list of plain dicts keyed by column
    name. The transaction is set read-only as an extra safety net, and the
    connection is closed before any processing starts so nothing downstream
    can touch the database.
    """
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            # get_connection() has already opened a transaction (its
            # statement_timeout SET pins the pooled backend), so psycopg's
            # conn.read_only cannot be used. Mark that open transaction
            # read-only instead; it is still before any query, which Postgres
            # requires. Any accidental write would now fail server-side.
            cur.execute("SET TRANSACTION READ ONLY")

            cur.execute(_PRODUCTS_SQL)
            cols = [d.name for d in cur.description]
            products = [dict(zip(cols, row)) for row in cur.fetchall()]

            cur.execute(_OFFERS_SQL)
            cols = [d.name for d in cur.description]
            offers = [dict(zip(cols, row)) for row in cur.fetchall()]
        # End the (read-only) transaction explicitly; there is nothing to commit.
        conn.rollback()
        return products, offers
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# Snapshot document construction
# ---------------------------------------------------------------------------

def _member_key(offer: dict) -> str:
    """Return the stable identity of an offer: '<store>:<store_product_id>'.

    Must stay identical to dump_clusters._member_key so member keys line up
    across the two snapshots.
    """
    return f"{offer['store']}:{offer['store_product_id']}"


def _cluster_record(product: dict, offers: list[dict]) -> dict:
    """Turn one product and its linked offers into a cluster JSON record.

    Carries every field dump_clusters writes (id, match_method, match_key,
    category, stores, members) plus DB-only extras (product_id,
    needs_review, review_reason) that diff_clusters simply ignores.
    Nullable text columns become "" so the record types match the pipeline
    snapshot, which diff_clusters formats as strings.
    """
    # Members sorted by key so output does not depend on SQL row order.
    members = sorted(
        (
            {
                "key": _member_key(o),
                "store": o["store"],
                "title": o["title"] or "",
                # current_price is a Decimal in the DB; JSON needs a float (or null).
                "price": float(o["current_price"]) if o["current_price"] is not None else None,
            }
            for o in offers
        ),
        key=lambda m: m["key"],
    )

    return {
        # Same id convention as dump_clusters: smallest member key.
        "id": members[0]["key"],
        # match_method is a Postgres enum; str() guards against adapters
        # that return a non-str wrapper type.
        "match_method": str(product["match_method"]) if product["match_method"] is not None else "",
        "match_key": product["match_key"] or "",
        "category": product["category"] or "",
        "stores": sorted({o["store"] for o in offers}),
        "members": members,
        # --- DB-only fields (not present in pipeline snapshots) ---
        # str() so UUID / bigint ids both serialize the same way.
        "product_id": str(product["id"]),
        "needs_review": bool(product["needs_review"]),
        "review_reason": product["review_reason"],
    }


def build_document(products: list[dict], offers: list[dict]) -> dict:
    """Group offers by product and return the snapshot document.

    The top-level shape (offer_count, cluster_count, clusters) is exactly
    what dump_clusters.build_document returns. offer_count counts only
    offers that ended up inside a cluster.
    """
    # product id -> offers linked to it.
    offers_by_product: dict = defaultdict(list)
    for o in offers:
        offers_by_product[o["product_id"]].append(o)

    records: list[dict] = []
    clustered_offers = 0
    for product in products:
        linked = offers_by_product.get(product["id"])
        # Products with no offers have no members and therefore no id.
        if not linked:
            continue
        records.append(_cluster_record(product, linked))
        clustered_offers += len(linked)

    # Clusters are ordered by their stable id, like dump_clusters.
    records.sort(key=lambda r: r["id"])

    return {
        "offer_count": clustered_offers,
        "cluster_count": len(records),
        "clusters": records,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_document(doc: dict, path: str) -> None:
    """Serialize the document deterministically and write it to *path*.

    Same settings as dump_clusters.write_document: sort_keys + indent=1 +
    ensure_ascii=False gives byte-identical output for identical input
    (Greek titles stay readable). The parent directory is created if missing.
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
        description="Read-only: snapshot the database's current products to JSON "
                    "in dump_clusters.py format.",
    )
    parser.add_argument(
        "output",
        help="Path of the JSON file to write (parent dirs are created).",
    )
    args = parser.parse_args()

    products, offers = _load_rows()
    doc = build_document(products, offers)
    write_document(doc, args.output)

    # One-line summary, identical across runs on the same DB state.
    multi_store = sum(1 for c in doc["clusters"] if len(c["stores"]) >= 2)
    print(
        f"products_with_offers={doc['cluster_count']} "
        f"offers={doc['offer_count']} "
        f"multi_store_products={multi_store}"
    )


if __name__ == "__main__":
    main()
