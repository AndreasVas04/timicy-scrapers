#!/usr/bin/env python3
"""
writer.py
---------
Canonical product writer.  Persists matched clusters as canonical `products`
rows and links every store_products offer back to its canonical.

Workflow:
  1. Load all enriched offers (read-only SELECT).
  2. Build deterministic clusters using the exact same tier pipeline as
     inspect_match_keys.py (tiers 2-5 via UnionFind).
  3. Propose a match_key and match_method per cluster (reusing the locked
     logic from inspect_match_keys).
  4. Select representative fields for each canonical (brand, title, etc.).
  5. Resolve identity: attach to existing canonicals, merge collisions, or
     create new rows.
  6. Execute all SQL inside a SINGLE transaction, then either COMMIT
     (--write) or ROLLBACK (default dry-run).

Usage:
    python -m matching.writer              # dry-run (rolls back)
    python -m matching.writer --write      # commits
    python -m matching.writer --samples 20
"""

import argparse
import re
from collections import Counter, defaultdict

from .inspect_match_keys import (
    _build_full_pipeline,
    _cluster_match_method,
    _detect_lang,
    _most_common_tiebreak,
    propose_match_key,
)
from .load import EnrichedOffer, get_connection, load_offers
from .tier_model_code import get_precomputed


# ---------------------------------------------------------------------------
# Formatting helpers
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
# STEP 1: Build clusters and compute per-cluster match_method + match_key.
#
# Reuses the exact same pipeline as inspect_match_keys.py so that the
# clusters and proposed keys are byte-for-byte identical.
# ---------------------------------------------------------------------------

def _build_clusters_and_proposals(
    offers: list[EnrichedOffer],
) -> tuple[
    list[list[int]],            # all_clusters (each is a list of offer indices)
    list[str],                  # cluster_methods (match_method per cluster)
    list[tuple[str, str]],      # cluster_keys ((match_key, key_kind) per cluster)
    list[set[str]],             # cluster_contribs (contributing tiers per cluster)
]:
    """Build deterministic clusters and propose match_key/match_method for each.

    This mirrors the exact logic from inspect_match_keys.py main() so that
    the writer produces the same clusters/keys as the approved inspection.
    """
    # Run the same tier-2 through tier-5 pipeline to build the UnionFind.
    uf, t2_edges, t3_edges, t4_edges, t5_edges = _build_full_pipeline(offers)
    all_clusters = uf.groups()

    # Build EAN tier (tier 1) pair set for match_method attribution.
    # EAN edges are implicit: offers sharing the same ean_key are connected.
    ean_groups: dict[str, list[int]] = defaultdict(list)
    for idx, o in enumerate(offers):
        if o.ean_key:
            ean_groups[o.ean_key].append(idx)
    ean_pair_set: set[frozenset[int]] = set()
    for members in ean_groups.values():
        if len(members) >= 2:
            for m in members[1:]:
                ean_pair_set.add(frozenset([members[0], m]))

    # Convert edge lists to frozenset pair sets for method attribution.
    t2_pair_set = {frozenset(e) for e in t2_edges}
    t3_pair_set = {frozenset(e) for e in t3_edges}
    t4_pair_set = {frozenset(e) for e in t4_edges}
    t5_pair_set = {frozenset(e) for e in t5_edges}

    # Get the discriminative model code chosen per offer (needed by
    # propose_match_key for the model_code key kind).
    _bs, _fr, chosen_model_codes = get_precomputed(offers)

    # Compute match_method (highest-priority contributing tier) and
    # match_key proposal for every cluster.
    cluster_methods: list[str] = []
    cluster_keys: list[tuple[str, str]] = []
    cluster_contribs: list[set[str]] = []

    for cluster in all_clusters:
        method, contribs = _cluster_match_method(
            cluster, ean_pair_set, t2_pair_set, t3_pair_set,
            t4_pair_set, t5_pair_set,
        )
        cluster_methods.append(method)
        cluster_contribs.append(contribs)

        key, kind = propose_match_key(cluster, offers, contribs, chosen_model_codes)
        cluster_keys.append((key, kind))

    return all_clusters, cluster_methods, cluster_keys, cluster_contribs


# ---------------------------------------------------------------------------
# STEP 2: Representative field selection (deterministic).
#
# For each cluster, pick the canonical brand, category, title, image, etc.
# These become the products row fields.
# ---------------------------------------------------------------------------

# Greek-character regex for EN/EL language detection (same as inspect_match_keys).
_GREEK_RE = re.compile(r"[\u0370-\u03FF\u1F00-\u1FFF]")


def _is_en(title: str) -> bool:
    """Return True if the title looks English (no Greek characters)."""
    return not _GREEK_RE.search(title)


def _select_representative(
    cluster: list[int],
    offers: list[EnrichedOffer],
    match_method: str,
    match_key: str,
    contributing_tiers: set[str],
) -> dict:
    """Select representative fields for a canonical product row.

    Deterministic rules:
      - brand:  most common non-empty effective_brand, tiebreak shortest/lex.
      - category: majority category, tiebreak shortest/lex.  If members span
        >1 category, set needs_review=true with reason.
      - title:  prefer an English-looking title; among EN titles pick the first
        (offers are in deterministic store+id order).  Fallback to first title.
      - normalized_title: the pre-computed title_norm of the chosen offer.
      - image:  image of the title-representative offer; fallback to first
        member with a non-null image.
      - ean/mpn_root/mpn: most common non-null value (representative identifier).
      - offer_count, min_price, max_price: computed from cluster members.
    """
    needs_review = False
    review_reasons: list[str] = []

    # -- Brand --
    brands = [offers[i].effective_brand for i in cluster if offers[i].effective_brand]
    brand = _most_common_tiebreak(brands).lower() if brands else None

    # -- Category --
    cats = [offers[i].category for i in cluster if offers[i].category]
    distinct_cats = sorted(set(cats)) if cats else []
    if cats:
        category = _most_common_tiebreak(cats)
    else:
        category = None

    # Flag cross-category clusters for human review.
    if len(distinct_cats) > 1:
        needs_review = True
        review_reasons.append(f"cross-category: {distinct_cats}")

    # -- Title (EN-preference) --
    # Among cluster members, prefer the first offer whose title looks English.
    # Offers are loaded in ORDER BY store, store_product_id so this is stable.
    rep_idx = cluster[0]
    for i in cluster:
        if _is_en(offers[i].title):
            rep_idx = i
            break
    rep_offer = offers[rep_idx]
    canonical_title = rep_offer.title
    normalized_title = rep_offer.title_norm

    # -- Image --
    # Use the title-representative offer's image first; if null, scan for any.
    image_url = rep_offer.image_url
    if not image_url:
        for i in cluster:
            if offers[i].image_url:
                image_url = offers[i].image_url
                break

    # -- Representative identifiers --
    eans = [offers[i].ean for i in cluster if offers[i].ean]
    rep_ean = _most_common_tiebreak(eans) if eans else None

    mpn_roots = [offers[i].mpn_root for i in cluster if offers[i].mpn_root]
    rep_mpn_root = _most_common_tiebreak(mpn_roots) if mpn_roots else None

    mpns = [offers[i].mpn for i in cluster if offers[i].mpn]
    rep_mpn = _most_common_tiebreak(mpns) if mpns else None

    # -- Aggregate price stats --
    prices = [offers[i].price for i in cluster if offers[i].price is not None]
    min_price = min(prices) if prices else None
    max_price = max(prices) if prices else None

    # -- Availability flag --
    # True when at least one offer in the cluster is currently available
    # (i.e. in stock / purchasable).  Recomputed on every writer run so the
    # products table always reflects the latest store_products.available state.
    has_available_offer = any(offers[i].available for i in cluster)

    review_reason = "; ".join(review_reasons) if review_reasons else None

    return {
        "brand": brand,
        "category": category,
        "canonical_title": canonical_title,
        "normalized_title": normalized_title,
        "image_url": image_url,
        "ean": rep_ean,
        "mpn_root": rep_mpn_root,
        "mpn": rep_mpn,
        "match_method": match_method,
        "match_key": match_key,
        "offer_count": len(cluster),
        "store_count": len({offers[i].store for i in cluster}),
        "min_price": min_price,
        "max_price": max_price,
        "has_available_offer": has_available_offer,
        "needs_review": needs_review,
        "review_reason": review_reason,
    }


# ---------------------------------------------------------------------------
# SQL templates
# ---------------------------------------------------------------------------

# UPDATE non-identity fields on an existing canonical product row.
# Never touches product_id.  match_key is reconciled per STEP 3(2).
_UPDATE_PRODUCT_SQL = """
UPDATE products SET
    canonical_title  = %(canonical_title)s,
    normalized_title = %(normalized_title)s,
    brand            = %(brand)s,
    category         = %(category)s::category,
    image_url        = %(image_url)s,
    ean              = %(ean)s,
    mpn_root         = %(mpn_root)s,
    mpn              = %(mpn)s,
    match_method     = %(match_method)s::match_method,
    match_key        = %(match_key)s,
    offer_count      = %(offer_count)s,
    store_count      = %(store_count)s,
    min_price        = %(min_price)s,
    max_price        = %(max_price)s,
    has_available_offer = %(has_available_offer)s,
    needs_review     = %(needs_review)s,
    review_reason    = %(review_reason)s,
    updated_at       = now()
WHERE id = %(product_id)s
"""

# Link a store_products row to its canonical product and set its match_method.
_LINK_OFFER_SQL = """
UPDATE store_products
SET product_id   = %(product_id)s,
    match_method = %(match_method)s::match_method
WHERE store = %(store)s::store_name
  AND store_product_id = %(store_product_id)s
"""


# ---------------------------------------------------------------------------
# STEP 3-5: Identity resolution + transaction execution.
#
# For each cluster, resolve which canonical product row it maps to:
#   (1) Gather existing product_ids from member offers' store_products rows.
#   (2) Exactly one existing product_id -> attach to it.
#   (3) Multiple product_ids -> merge to MIN (survivor), flag for review.
#   (4) No existing product_id -> secondary lookup by match_key in products.
#   (5) No match at all -> create new canonical.
#
# All SQL runs in a single transaction.  Dry-run rolls back; --write commits.
# ---------------------------------------------------------------------------

def _run_writer(conn, offers, all_clusters, cluster_methods, cluster_keys,
                cluster_contribs, max_samples, do_write):
    """Execute the full identity-resolution + write pipeline in one transaction.

    Two-phase design for performance over remote connections:
      Phase 1 — In-memory: compute all identity decisions and representative
                fields without issuing any write SQL.
      Phase 2 — Batched SQL: execute all INSERTs, UPDATEs, and link UPDATEs
                using executemany (which psycopg3 pipelines automatically).

    Returns nothing; prints all output to stdout.
    """
    cur = conn.cursor()

    # -- Pre-flight: count existing products for cold-vs-warm detection. --
    cur.execute("SELECT COUNT(*) FROM products")
    existing_product_count = cur.fetchone()[0]
    is_cold = existing_product_count == 0

    # -- Load the store_products mapping: (store, store_product_id) -> product_id.
    # This tells us which offers already have a canonical attached. --
    cur.execute("""
        SELECT store, store_product_id, product_id
        FROM store_products
    """)
    sp_product_id: dict[tuple[str, str], int | None] = {}
    for row in cur.fetchall():
        sp_product_id[(row[0], row[1])] = row[2]

    # -- Load existing match_key -> product_id mapping (for secondary lookup). --
    existing_by_key: dict[str, list[int]] = defaultdict(list)
    if not is_cold:
        cur.execute("SELECT id, match_key FROM products WHERE match_key IS NOT NULL")
        for row in cur.fetchall():
            existing_by_key[row[1]].append(row[0])

    print("  Phase 1: computing identity decisions in memory...")

    # -- Counters for the report. --
    created_new = 0
    attached_existing = 0
    merge_events = 0
    repointed_offers = 0
    unchanged_offers = 0
    needs_review_total = 0
    review_reason_counts: Counter = Counter()
    cross_cat_details: list[tuple[str, str, list[str]]] = []
    merge_details: list[dict] = []
    sample_rows: list[dict] = []

    total_clusters = len(all_clusters)
    total_offers = len(offers)

    # Per-cluster decision records.  Each entry stores the resolution type,
    # the representative fields dict, and the member offer keys.
    # - "new": needs INSERT, product_id assigned later from DB
    # - "attached"/"key_lookup"/"ambiguous_key": needs UPDATE, product_id known
    # - "merged": needs UPDATE, product_id known (survivor)
    decisions: list[dict] = []

    # -----------------------------------------------------------------------
    # PHASE 1: Walk every cluster, compute representative fields and
    # identity-resolution decision purely in memory.
    # -----------------------------------------------------------------------
    for ci, cluster in enumerate(all_clusters):
        match_method = cluster_methods[ci]
        match_key, key_kind = cluster_keys[ci]
        contribs = cluster_contribs[ci]

        # STEP 2: pick representative fields for the canonical row.
        rep = _select_representative(
            cluster, offers, match_method, match_key, contribs,
        )

        # Track cross-category clusters for the report.
        if rep["needs_review"] and rep["review_reason"] and "cross-category" in rep["review_reason"]:
            cats = sorted({offers[i].category for i in cluster if offers[i].category})
            cross_cat_details.append((match_key, rep["category"], cats))

        # -- STEP 3: Identity resolution (in-memory only). --

        # (1) Gather distinct existing product_ids among member offers.
        member_keys = [(offers[i].store, offers[i].store_product_id) for i in cluster]
        existing_pids = set()
        for mk in member_keys:
            pid = sp_product_id.get(mk)
            if pid is not None:
                existing_pids.add(pid)

        resolved_pid: int | None = None
        resolution = ""

        if len(existing_pids) == 1:
            # (2) Exactly one existing product_id -> attach to it.
            resolved_pid = next(iter(existing_pids))
            resolution = "attached"
            attached_existing += 1
            rep["product_id"] = resolved_pid

        elif len(existing_pids) > 1:
            # (3) Multiple distinct product_ids -> merge event.
            # Survivor = smallest (oldest) product_id.
            survivor = min(existing_pids)
            absorbed = sorted(existing_pids - {survivor})
            resolved_pid = survivor
            resolution = "merged"
            merge_events += 1

            for mk in member_keys:
                pid = sp_product_id.get(mk)
                if pid is not None and pid != survivor:
                    repointed_offers += 1

            rep["product_id"] = survivor
            rep["needs_review"] = True
            absorbed_str = f"merge: absorbed product_ids {absorbed}"
            if rep["review_reason"]:
                rep["review_reason"] += f"; {absorbed_str}"
            else:
                rep["review_reason"] = absorbed_str

            merge_details.append({
                "match_key": match_key,
                "survivor": survivor,
                "absorbed": absorbed,
                "member_count": len(cluster),
                "stores": sorted({offers[i].store for i in cluster}),
            })

        else:
            # No member offer has an existing product_id.
            # (4) Secondary lookup by match_key in products table.
            key_hits = existing_by_key.get(match_key, [])

            if len(key_hits) == 1:
                resolved_pid = key_hits[0]
                resolution = "key_lookup"
                attached_existing += 1
                rep["product_id"] = resolved_pid

            elif len(key_hits) > 1:
                resolved_pid = min(key_hits)
                resolution = "ambiguous_key"
                attached_existing += 1
                rep["product_id"] = resolved_pid
                rep["needs_review"] = True
                ambig_str = f"ambiguous match_key: product_ids {sorted(key_hits)}"
                if rep["review_reason"]:
                    rep["review_reason"] += f"; {ambig_str}"
                else:
                    rep["review_reason"] = ambig_str

            else:
                # (5) No match at all -> will create a new canonical row.
                resolution = "new"
                created_new += 1
                # product_id will be assigned after the batch INSERT.

        # Count unchanged offers (already linked to the correct canonical).
        for mk in member_keys:
            old_pid = sp_product_id.get(mk)
            if old_pid is not None and old_pid == resolved_pid:
                unchanged_offers += 1

        # Track review counts.
        if rep["needs_review"]:
            needs_review_total += 1
            if rep["review_reason"]:
                for reason_part in rep["review_reason"].split("; "):
                    reason_key = reason_part.split(":")[0].strip()
                    review_reason_counts[reason_key] += 1

        # Collect sample rows for the report.
        if len(sample_rows) < max_samples:
            lang = "EN" if _is_en(rep["canonical_title"]) else "EL"
            sample_rows.append({
                "product_id": resolved_pid if resolution != "new" else "NEW",
                "resolution": resolution,
                "match_method": match_method,
                "match_key": match_key,
                "title": rep["canonical_title"][:80],
                "lang": lang,
                "brand": rep["brand"] or "-",
                "category": rep["category"] or "-",
                "member_count": len(cluster),
                "stores": sorted({offers[i].store for i in cluster}),
            })

        decisions.append({
            "ci": ci,
            "resolution": resolution,
            "resolved_pid": resolved_pid,
            "rep": rep,
            "member_keys": member_keys,
            "match_method": match_method,
            # Offer-index list for this cluster, needed by the collision-merge
            # step to recompute representative fields over the union of offers.
            "cluster": cluster,
        })

    # -------------------------------------------------------------------
    # PHASE 1b: Collision-merge — deduplicate decisions that target the
    # same resolved_pid.
    #
    # WHY collisions happen: as the matching pipeline evolves (tiers are
    # added, removed, or reweighted), a cluster that previously grouped
    # N offers under one product_id may now be split into several smaller
    # clusters.  Each sub-cluster independently resolves to the SAME
    # existing product_id via the store_products lookup, so without this
    # step Phase 2 would execute _UPDATE_PRODUCT_SQL multiple times for
    # that id.  Last write wins, and the aggregates (offer_count,
    # store_count, min_price, max_price, has_available_offer) would
    # reflect only one arbitrary sub-cluster instead of all offers that
    # still belong to this product.
    #
    # Fix: detect resolved_pid collisions among non-"new" decisions,
    # merge all colliding decisions into a single decision whose
    # representative fields are recomputed over the UNION of their
    # offer indices, so each product_id receives exactly one UPDATE.
    # -------------------------------------------------------------------

    # Group non-"new" decisions by resolved_pid to find collisions.
    # "new" decisions have resolved_pid=None (assigned later in Phase 2a),
    # so they cannot collide and are excluded.
    pid_to_dindices: dict[int, list[int]] = defaultdict(list)
    for di, d in enumerate(decisions):
        if d["resolution"] != "new" and d["resolved_pid"] is not None:
            pid_to_dindices[d["resolved_pid"]].append(di)

    collided_products = 0
    # Indices of decisions absorbed into a merged decision; removed below.
    absorbed_decision_indices: set[int] = set()

    for pid, d_indices in pid_to_dindices.items():
        if len(d_indices) <= 1:
            continue  # No collision for this product_id.

        collided_products += 1

        # -- Pick the winning sub-cluster --
        # Primary: largest member list (most offers).
        # Tie-break: earliest position in the decisions list, which is
        # deterministic because clusters derive from a stable offer order.
        winner_di = min(
            d_indices,
            key=lambda di: (-len(decisions[di]["cluster"]), di),
        )
        winning = decisions[winner_di]

        # -- Build the union of offer indices and member keys --
        union_cluster: list[int] = []
        union_member_keys: list[tuple[str, str]] = []
        for di in d_indices:
            union_cluster.extend(decisions[di]["cluster"])
            union_member_keys.extend(decisions[di]["member_keys"])

        # -- Recompute representative fields over the full union --
        # Uses the winning decision's match_method, match_key, and
        # contributing tiers so the canonical row reflects the best
        # available proposal.
        rep = _select_representative(
            union_cluster, offers,
            winning["match_method"],
            winning["rep"]["match_key"],
            cluster_contribs[winning["ci"]],
        )

        # Preserve the resolved product_id and flag for human review.
        rep["product_id"] = pid
        rep["needs_review"] = True
        collision_reason = (
            f"split-cluster reattachment: {len(d_indices)} clusters"
        )
        if rep["review_reason"]:
            rep["review_reason"] += f"; {collision_reason}"
        else:
            rep["review_reason"] = collision_reason

        # Replace all colliding decisions with a single merged decision
        # placed at the winner's position; mark the rest for removal.
        decisions[winner_di] = {
            "ci": winning["ci"],
            "resolution": "collision_merged",
            "resolved_pid": pid,
            "rep": rep,
            "member_keys": union_member_keys,
            "match_method": winning["match_method"],
            "cluster": union_cluster,
        }
        for di in d_indices:
            if di != winner_di:
                absorbed_decision_indices.add(di)

    # Remove absorbed decisions so Phase 2 sees exactly one decision per
    # product_id.  "collision_merged" resolution is != "new", so it flows
    # into update_decisions automatically.
    if absorbed_decision_indices:
        decisions = [
            d for di, d in enumerate(decisions)
            if di not in absorbed_decision_indices
        ]

    # -----------------------------------------------------------------------
    # PHASE 2: Execute all SQL in batches using executemany (pipelined by
    # psycopg3 for performance).
    # -----------------------------------------------------------------------
    print("  Phase 2: executing batched SQL...")

    # -- 2a: Batch INSERT all new products. --
    new_decisions = [d for d in decisions if d["resolution"] == "new"]
    if new_decisions:
        # executemany pipelines the INSERTs automatically.  We use a plain
        # INSERT without RETURNING here, then fetch the assigned product_ids
        # by match_key in a single SELECT afterwards.
        _INSERT_NO_RETURN_SQL = """
        INSERT INTO products (
            category, brand, canonical_title, normalized_title,
            ean, mpn_root, mpn, image_url,
            match_method, match_key,
            offer_count, store_count, min_price, max_price,
            has_available_offer,
            needs_review, review_reason
        ) VALUES (
            %(category)s::category, %(brand)s, %(canonical_title)s, %(normalized_title)s,
            %(ean)s, %(mpn_root)s, %(mpn)s, %(image_url)s,
            %(match_method)s::match_method, %(match_key)s,
            %(offer_count)s, %(store_count)s, %(min_price)s, %(max_price)s,
            %(has_available_offer)s,
            %(needs_review)s, %(review_reason)s
        )
        """
        cur.executemany(_INSERT_NO_RETURN_SQL, [d["rep"] for d in new_decisions])

        # Fetch the newly assigned product_ids by match_key.  Since match_keys
        # are collision-free (verified by inspect_match_keys), each key maps
        # to exactly one product.
        cur.execute("SELECT id, match_key FROM products WHERE match_key IS NOT NULL")
        key_to_pid: dict[str, int] = {}
        for row in cur.fetchall():
            key_to_pid[row[1]] = row[0]

        # Assign the real product_ids back to the new decisions.
        for d in new_decisions:
            mk = d["rep"]["match_key"]
            d["resolved_pid"] = key_to_pid[mk]

    # -- 2b: Batch UPDATE existing products (attach/merge/key_lookup). --
    update_decisions = [d for d in decisions if d["resolution"] != "new"]
    if update_decisions:
        cur.executemany(_UPDATE_PRODUCT_SQL, [d["rep"] for d in update_decisions])

    # -- 2c: Batch UPDATE store_products links. --
    # Build the full list of link params now that all product_ids are known.
    link_params: list[dict] = []
    for d in decisions:
        pid = d["resolved_pid"]
        mm = d["match_method"]
        for mk in d["member_keys"]:
            link_params.append({
                "product_id": pid,
                "match_method": mm,
                "store": mk[0],
                "store_product_id": mk[1],
            })

    if link_params:
        cur.executemany(_LINK_OFFER_SQL, link_params)

    # -------------------------------------------------------------------
    # 2d: Merge aftermath — repoint subscriptions, record redirects,
    # and delete absorbed products.
    #
    # Runs AFTER offer links have been repointed (2c) and BEFORE the
    # transaction is committed/rolled-back, so everything is atomic.
    # -------------------------------------------------------------------
    merge_decisions = [d for d in decisions if d["resolution"] == "merged"]

    # Collect ALL absorbed product ids across all merge events for batched
    # operations — avoids one-statement-per-id round-trips.
    all_absorbed_ids: list[int] = []
    # Map survivor -> list of absorbed ids (for per-merge operations).
    survivor_absorbed: list[tuple[int, list[int]]] = []
    for d in merge_decisions:
        survivor = d["resolved_pid"]
        absorbed = d["rep"]["review_reason"]
        # Extract absorbed ids from the merge_details list (already computed
        # in Phase 1).
        absorbed_ids = [
            md["absorbed"]
            for md in merge_details
            if md["survivor"] == survivor
        ]
        # Flatten: merge_details stores absorbed as a list of ints.
        flat_absorbed = []
        for a in absorbed_ids:
            if isinstance(a, list):
                flat_absorbed.extend(a)
            else:
                flat_absorbed.append(a)
        if flat_absorbed:
            survivor_absorbed.append((survivor, flat_absorbed))
            all_absorbed_ids.extend(flat_absorbed)

    repointed_subscriptions = 0
    redirect_rows_written = 0
    compressed_chain_rows = 0
    deleted_absorbed_products = 0

    if all_absorbed_ids:
        # -- Step (a): REPOINT SUBSCRIPTIONS --
        # For each absorbed id, move its price_subscriptions to the survivor,
        # but only when the same (email, product_id=survivor) pair does NOT
        # already exist.  This respects the UNIQUE(email, product_id)
        # constraint.  Duplicate-email rows are intentionally left behind
        # and will be removed by CASCADE when the absorbed product is
        # deleted in step (c), keeping the older survivor-side subscription.
        _REPOINT_SUBS_SQL = """
            UPDATE price_subscriptions
            SET    product_id = %(survivor)s
            WHERE  product_id = %(absorbed)s
              AND  NOT EXISTS (
                  SELECT 1 FROM price_subscriptions s2
                  WHERE  s2.email      = price_subscriptions.email
                    AND  s2.product_id = %(survivor)s
              )
        """
        repoint_params: list[dict] = []
        for survivor, absorbed_list in survivor_absorbed:
            for absorbed_id in absorbed_list:
                repoint_params.append({
                    "survivor": survivor,
                    "absorbed": absorbed_id,
                })
        if repoint_params:
            # Execute each repoint individually to accumulate accurate
            # rowcounts (merge events are rare, so no performance concern).
            for p in repoint_params:
                cur.execute(_REPOINT_SUBS_SQL, p)
                repointed_subscriptions += cur.rowcount

        # -- Step (b): RECORD REDIRECTS with chain compression --
        # Insert a mapping from each absorbed id to its survivor in
        # merged_products.  ON CONFLICT updates the target if the absorbed
        # id was already redirected from an earlier merge.
        _REDIRECT_SQL = """
            INSERT INTO merged_products (old_id, new_id)
            VALUES (%(absorbed)s, %(survivor)s)
            ON CONFLICT (old_id) DO UPDATE
                SET new_id    = EXCLUDED.new_id,
                    merged_at = now()
        """
        redirect_params: list[dict] = []
        for survivor, absorbed_list in survivor_absorbed:
            for absorbed_id in absorbed_list:
                redirect_params.append({
                    "absorbed": absorbed_id,
                    "survivor": survivor,
                })
        if redirect_params:
            cur.executemany(_REDIRECT_SQL, redirect_params)
            redirect_rows_written = len(redirect_params)

        # Chain compression: any earlier redirect that pointed AT an
        # absorbed id must now point at the final survivor.  This keeps
        # all mappings single-hop so the frontend never needs to follow
        # chains.
        _COMPRESS_SQL = """
            UPDATE merged_products
            SET    new_id    = %(survivor)s,
                   merged_at = now()
            WHERE  new_id    = %(absorbed)s
        """
        compress_params: list[dict] = []
        for survivor, absorbed_list in survivor_absorbed:
            for absorbed_id in absorbed_list:
                compress_params.append({
                    "absorbed": absorbed_id,
                    "survivor": survivor,
                })
        if compress_params:
            # Execute each compression individually to accumulate accurate
            # rowcounts (merge events are rare, so no performance concern).
            for p in compress_params:
                cur.execute(_COMPRESS_SQL, p)
                compressed_chain_rows += cur.rowcount

        # -- Step (c): DELETE ABSORBED PRODUCTS --
        # Safety assertion: verify no store_products rows still reference
        # absorbed ids.  If any do, it means offers were not fully
        # repointed in step 2c — abort the entire transaction rather than
        # leaving orphan references.
        cur.execute(
            "SELECT COUNT(*) FROM store_products WHERE product_id = ANY(%s)",
            (all_absorbed_ids,),
        )
        dangling_count = cur.fetchone()[0]
        if dangling_count != 0:
            raise RuntimeError(
                f"ABORTING: {dangling_count} store_products row(s) still "
                f"reference absorbed product ids {all_absorbed_ids}. "
                f"Offers must be repointed before deleting absorbed products."
            )

        # Safe to delete: offers repointed (2c), subscriptions repointed
        # (a), redirects recorded (b).  The FK from merged_products.new_id
        # references products(id) — new_id always points at the survivor
        # which continues to exist.  price_subscriptions CASCADE removes
        # only the duplicate-email rows intentionally left behind in (a).
        cur.execute(
            "DELETE FROM products WHERE id = ANY(%s)",
            (all_absorbed_ids,),
        )
        deleted_absorbed_products = cur.rowcount

    print("  SQL execution complete.")

    # ===================================================================
    # STEP 6: Raw output report
    # ===================================================================
    _header("WRITER REPORT")

    print(f"\n  Total offers:                {total_offers}")
    print(f"  Total proposed clusters:     {total_clusters}")
    print(f"  Run type:                    {'COLD START' if is_cold else 'WARM RUN'}")
    if not is_cold:
        print(f"  Existing products before:    {existing_product_count}")

    _header("Identity resolution counts")
    print(f"\n  created_new:              {created_new}")
    print(f"  attached_existing:        {attached_existing}")
    print(f"  merge_events:             {merge_events}")
    print(f"  repointed_offers:         {repointed_offers}")
    print(f"  unchanged_offers:         {unchanged_offers}")
    print(f"  collided_products:        {collided_products}")

    # -- Availability summary --
    # Count how many clusters have / lack at least one available offer.
    # Printed in both dry-run and write mode for verification.
    avail_true  = sum(1 for d in decisions if d["rep"]["has_available_offer"])
    avail_false = sum(1 for d in decisions if not d["rep"]["has_available_offer"])
    print(f"  has_available_offer=true: {avail_true}")
    print(f"  has_available_offer=false:{avail_false}")

    _header("Merge aftermath")
    print(f"\n  repointed_subscriptions:  {repointed_subscriptions}")
    print(f"  redirect_rows_written:    {redirect_rows_written}")
    print(f"  compressed_chain_rows:    {compressed_chain_rows}")
    print(f"  deleted_absorbed_products:{deleted_absorbed_products}")

    _header("Review flags")
    print(f"\n  needs_review total:   {needs_review_total}")
    if review_reason_counts:
        for reason, cnt in review_reason_counts.most_common():
            print(f"    {reason:<25s} {cnt}")

    # Cross-category detail.
    if cross_cat_details:
        _header(f"Cross-category clusters ({len(cross_cat_details)} total)")
        for mk, chosen, spanned in cross_cat_details[:max_samples]:
            print(f"  match_key: {mk}")
            print(f"    chosen category: {chosen},  spanned: {spanned}")

    # Sample representative selections.
    _header(f"Sample representative selections (first {len(sample_rows)})")
    for s in sample_rows:
        print(f"\n  product_id: {s['product_id']}  ({s['resolution']})")
        print(f"    match_method={s['match_method']}  match_key={s['match_key']}")
        print(f"    title ({s['lang']}): {s['title']}")
        print(f"    brand={s['brand']}  category={s['category']}")
        print(f"    members={s['member_count']}  stores={s['stores']}")

    # Merge event detail.
    if merge_details:
        _header(f"Merge events ({len(merge_details)} total)")
        for md in merge_details[:max_samples]:
            print(f"\n  match_key: {md['match_key']}")
            print(f"    survivor: {md['survivor']}")
            print(f"    absorbed: {md['absorbed']}  (deleted)")
            print(f"    members: {md['member_count']}, stores: {md['stores']}")

    # -- Final commit/rollback decision. --
    _header("FINAL")
    if do_write:
        conn.commit()
        print(f"\n  WROTE — committed.  "
              f"created={created_new}, attached={attached_existing}, "
              f"merged={merge_events}, repointed={repointed_offers}")
    else:
        conn.rollback()
        print(f"\n  DRY-RUN — rolled back.  "
              f"would_create={created_new}, would_attach={attached_existing}, "
              f"would_merge={merge_events}, would_repoint={repointed_offers}")

    print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Canonical product writer (default: dry-run, rolls back).",
    )
    parser.add_argument(
        "--write", action="store_true",
        help="Commit the transaction. Without this flag the run is a dry-run.",
    )
    parser.add_argument(
        "--samples", type=int, default=15,
        help="Max sample rows shown in the report.",
    )
    args = parser.parse_args()

    # Open a single connection used for everything: loading offers, reading
    # existing state, and executing inserts/updates.  autocommit is off by
    # default in psycopg, so all statements are inside one transaction.
    conn = get_connection()
    # Disable automatic prepared statements — they conflict with pgbouncer
    # transaction-mode pooling (which is what Supabase uses by default).
    conn.prepare_threshold = None

    # Print the active server-side statement timeout so every CI log
    # carries verbatim proof that the bound is in effect (nightly
    # verification checklist item).
    with conn.cursor() as cur:
        cur.execute("SHOW statement_timeout")
        print(f"statement_timeout: {cur.fetchone()[0]}")

    try:
        print("Loading offers...")
        offers = load_offers(conn)
        print(f"Loaded {len(offers)} offers.")

        print("Building clusters and proposals...")
        all_clusters, cluster_methods, cluster_keys, cluster_contribs = (
            _build_clusters_and_proposals(offers)
        )
        print(f"Built {len(all_clusters)} clusters.")

        print("Running identity resolution...")
        _run_writer(
            conn, offers, all_clusters, cluster_methods, cluster_keys,
            cluster_contribs, args.samples, args.write,
        )
    except Exception:
        # On any error, roll back to avoid partial writes.
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
