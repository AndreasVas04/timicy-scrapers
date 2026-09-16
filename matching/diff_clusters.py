#!/usr/bin/env python3
"""
diff_clusters.py
----------------
Read-only CLI that compares two cluster snapshots written by
dump_clusters.py (a baseline and a candidate pipeline version) and prints
a human-readable report of how the clusters changed.

Touches NO database and writes NO files. It is a report, not a gate: the
exit code is 0 whatever the diff contains.

Usage:
    python3 -m matching.diff_clusters data/clusters/baseline.json \\
        data/clusters/candidate.json --max 50 [--category NAME] [--stores]
"""

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field


# Titles in member lines are cut to this many characters.
TITLE_WIDTH = 90


# ---------------------------------------------------------------------------
# Formatting helpers (same style as inspect_match_keys.py)
# ---------------------------------------------------------------------------

def _header(title: str) -> None:
    """Print a section header with a visual separator."""
    print()
    print("=" * 90)
    print(f"  {title}")
    print("=" * 90)


def _print_member(member: dict, indent: str) -> None:
    """Print one member line: key | store | truncated title."""
    print(f"{indent}{member['key']}  |  {member['store']}  |  "
          f"{member['title'][:TITLE_WIDTH]}")


# ---------------------------------------------------------------------------
# Snapshot loading and indexing
# ---------------------------------------------------------------------------

class Snapshot:
    """One side of the comparison, indexed for fast lookups.

    Attributes:
      offer_count / cluster_count: copied from the file header.
      clusters:          cluster id -> cluster record (as in the JSON).
      member_to_cluster: member key -> id of the cluster containing it.
      members:           member key -> member record (key, store, title, price).
      member_sets:       cluster id -> frozenset of its member keys.
    """

    def __init__(self, path: str) -> None:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)

        self.path = path
        self.offer_count: int = doc["offer_count"]
        self.cluster_count: int = doc["cluster_count"]
        self.clusters: dict[str, dict] = {}
        self.member_to_cluster: dict[str, str] = {}
        self.members: dict[str, dict] = {}
        self.member_sets: dict[str, frozenset[str]] = {}

        for cluster in doc["clusters"]:
            cid = cluster["id"]
            self.clusters[cid] = cluster
            for member in cluster["members"]:
                self.member_to_cluster[member["key"]] = cid
                self.members[member["key"]] = member
            self.member_sets[cid] = frozenset(m["key"] for m in cluster["members"])

    def multi_store_count(self) -> int:
        """Number of clusters that span at least two stores."""
        return sum(1 for c in self.clusters.values() if len(c["stores"]) >= 2)


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------

@dataclass
class ClusterDiff:
    """Result of comparing a baseline snapshot with a candidate snapshot."""

    unchanged: int = 0
    # Baseline cluster ids whose members land in >= 2 candidate clusters.
    splits: list[str] = field(default_factory=list)
    # Candidate cluster ids that take members from >= 2 baseline clusters.
    merges: list[str] = field(default_factory=list)
    moved: int = 0
    added: list[str] = field(default_factory=list)    # keys only in candidate
    removed: list[str] = field(default_factory=list)  # keys only in baseline
    # baseline cluster id -> candidate cluster ids its shared members went to
    base_to_cands: dict[str, set[str]] = field(default_factory=dict)
    # candidate cluster id -> baseline cluster ids its shared members came from
    cand_to_bases: dict[str, set[str]] = field(default_factory=dict)


def compute_diff(base: Snapshot, cand: Snapshot) -> ClusterDiff:
    """Classify every cluster and member change between the two snapshots."""
    base_keys = base.member_to_cluster.keys()
    cand_keys = cand.member_to_cluster.keys()

    # Members that exist on both sides are the only ones that can split,
    # merge or move; the rest are simply added or removed.
    common = base_keys & cand_keys
    added = sorted(cand_keys - base_keys)
    removed = sorted(base_keys - cand_keys)

    # Map clusters across sides through their shared members.
    base_to_cands: dict[str, set[str]] = defaultdict(set)
    cand_to_bases: dict[str, set[str]] = defaultdict(set)
    for key in common:
        b = base.member_to_cluster[key]
        c = cand.member_to_cluster[key]
        base_to_cands[b].add(c)
        cand_to_bases[c].add(b)

    # A cluster can appear in both lists (split in one direction, merged in
    # the other); that is intentional.
    splits = sorted(b for b, cs in base_to_cands.items() if len(cs) >= 2)
    merges = sorted(c for c, bs in cand_to_bases.items() if len(bs) >= 2)

    # Unchanged: a baseline cluster with an identical member set on the
    # candidate side. Any one member is enough to find the only candidate
    # cluster that could be identical.
    unchanged = 0
    for bid, bset in base.member_sets.items():
        if not bset:
            continue
        cid = cand.member_to_cluster.get(min(bset))
        if cid is not None and cand.member_sets[cid] == bset:
            unchanged += 1

    # Moved: a shared member whose baseline and candidate clusters have
    # different member sets, but where neither a split (on its baseline
    # cluster) nor a merge (on its candidate cluster) explains it. In
    # practice this means its cluster changed only through added/removed
    # offers, which should be rare.
    split_set, merge_set = set(splits), set(merges)
    moved = 0
    for key in common:
        b = base.member_to_cluster[key]
        c = cand.member_to_cluster[key]
        if base.member_sets[b] == cand.member_sets[c]:
            continue
        if b in split_set or c in merge_set:
            continue
        moved += 1

    return ClusterDiff(
        unchanged=unchanged,
        splits=splits,
        merges=merges,
        moved=moved,
        added=added,
        removed=removed,
        base_to_cands=dict(base_to_cands),
        cand_to_bases=dict(cand_to_bases),
    )


# ---------------------------------------------------------------------------
# Detail-section filters (--category, --stores)
# ---------------------------------------------------------------------------

def _passes_filters(
    cluster: dict,
    counterpart_ids: set[str],
    counterpart_side: Snapshot,
    category: str | None,
    stores_only: bool,
) -> bool:
    """Decide whether a split/merge cluster is shown in the detail sections.

    cluster:          the cluster being reported (baseline for a split,
                      candidate for a merge).
    counterpart_ids:  the clusters on the other side it maps to.
    category:         if given, the reported cluster's category must match.
    stores_only:      if set, keep it only when at least one counterpart
                      cluster has a different stores list.
    """
    if category is not None and cluster["category"] != category:
        return False
    if stores_only and all(
        counterpart_side.clusters[x]["stores"] == cluster["stores"]
        for x in counterpart_ids
    ):
        return False
    return True


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------

def print_summary(base: Snapshot, cand: Snapshot, diff: ClusterDiff) -> None:
    """Section 1: side-by-side totals, then the diff counts."""
    _header("1. Summary")
    print(f"\n  baseline:  {base.path}")
    print(f"  candidate: {cand.path}\n")
    print(f"  {'':<24s} {'baseline':>10s} {'candidate':>10s}")
    print(f"  {'offers':<24s} {base.offer_count:>10d} {cand.offer_count:>10d}")
    print(f"  {'clusters':<24s} {base.cluster_count:>10d} {cand.cluster_count:>10d}")
    print(f"  {'multi-store clusters':<24s} "
          f"{base.multi_store_count():>10d} {cand.multi_store_count():>10d}")
    print()
    print(f"  {'unchanged':<24s} {diff.unchanged:>10d}")
    print(f"  {'splits':<24s} {len(diff.splits):>10d}")
    print(f"  {'merges':<24s} {len(diff.merges):>10d}")
    print(f"  {'moved':<24s} {diff.moved:>10d}")
    print(f"  {'added':<24s} {len(diff.added):>10d}")
    print(f"  {'removed':<24s} {len(diff.removed):>10d}")


def print_category_table(base: Snapshot, cand: Snapshot, diff: ClusterDiff) -> None:
    """Section 2: splits (by baseline category) and merges (by candidate
    category), busiest categories first."""
    _header("2. Splits and merges by category")
    split_cats = Counter(base.clusters[b]["category"] for b in diff.splits)
    merge_cats = Counter(cand.clusters[c]["category"] for c in diff.merges)
    cats = sorted(
        set(split_cats) | set(merge_cats),
        key=lambda k: (-(split_cats[k] + merge_cats[k]), k),
    )
    if not cats:
        print("\n  (no splits or merges)")
        return
    print(f"\n  {'category':<40s} {'splits':>8s} {'merges':>8s}")
    for cat in cats:
        print(f"  {cat or '(none)':<40s} {split_cats[cat]:>8d} {merge_cats[cat]:>8d}")


def print_split_details(
    base: Snapshot, cand: Snapshot, diff: ClusterDiff,
    max_items: int, category: str | None, stores_only: bool,
) -> None:
    """Section 3: for each split baseline cluster, show where its members went."""
    selected = [
        b for b in diff.splits
        if _passes_filters(base.clusters[b], diff.base_to_cands[b], cand,
                           category, stores_only)
    ]
    shown = selected[:max_items]
    _header(f"3. Split details (showing {len(shown)} of {len(selected)})")

    for bid in shown:
        bc = base.clusters[bid]
        print(f"\n  baseline cluster {bid}  [{bc['category'] or '(none)'}]")
        print(f"    match_method: {bc['match_method']}")
        print(f"    match_key:    {bc['match_key']}")

        # One group per candidate cluster that received some of its members.
        for cid in sorted(diff.base_to_cands[bid]):
            cc = cand.clusters[cid]
            landed = sorted(k for k in base.member_sets[bid]
                            if cand.member_to_cluster.get(k) == cid)
            others = len(cc["members"]) - len(landed)
            print(f"\n    -> candidate cluster {cid}  "
                  f"(method={cc['match_method']}, {len(landed)} members from here"
                  f"{f', +{others} from elsewhere' if others else ''})")
            for key in landed:
                _print_member(cand.members[key], "         ")

        # Members that no longer exist in the candidate snapshot at all.
        gone = sorted(k for k in base.member_sets[bid]
                      if k not in cand.member_to_cluster)
        if gone:
            print(f"\n    -> not in candidate ({len(gone)} members)")
            for key in gone:
                _print_member(base.members[key], "         ")


def print_merge_details(
    base: Snapshot, cand: Snapshot, diff: ClusterDiff,
    max_items: int, category: str | None, stores_only: bool,
) -> None:
    """Section 4: for each merged candidate cluster, show where its members came from."""
    selected = [
        c for c in diff.merges
        if _passes_filters(cand.clusters[c], diff.cand_to_bases[c], base,
                           category, stores_only)
    ]
    shown = selected[:max_items]
    _header(f"4. Merge details (showing {len(shown)} of {len(selected)})")

    for cid in shown:
        cc = cand.clusters[cid]
        print(f"\n  candidate cluster {cid}  [{cc['category'] or '(none)'}]")
        print(f"    match_method: {cc['match_method']}")
        print(f"    match_key:    {cc['match_key']}")

        # One group per baseline cluster that contributed members.
        for bid in sorted(diff.cand_to_bases[cid]):
            bc = base.clusters[bid]
            came = sorted(k for k in cand.member_sets[cid]
                          if base.member_to_cluster.get(k) == bid)
            others = len(bc["members"]) - len(came)
            print(f"\n    <- baseline cluster {bid}  "
                  f"(method={bc['match_method']}, {len(came)} members from there"
                  f"{f', {others} went elsewhere' if others else ''})")
            print(f"       match_key: {bc['match_key']}")
            for key in came:
                _print_member(base.members[key], "         ")

        # Members that did not exist in the baseline snapshot.
        new = sorted(k for k in cand.member_sets[cid]
                     if k not in base.member_to_cluster)
        if new:
            print(f"\n    <- new in candidate ({len(new)} members)")
            for key in new:
                _print_member(cand.members[key], "         ")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only: compare two cluster snapshots from dump_clusters.py.",
    )
    parser.add_argument("baseline", help="Baseline snapshot JSON.")
    parser.add_argument("candidate", help="Candidate snapshot JSON.")
    parser.add_argument(
        "--max", type=int, default=50,
        help="Max clusters shown in each of the split and merge detail sections.",
    )
    parser.add_argument(
        "--category", default=None,
        help="Restrict split/merge details to this category.",
    )
    parser.add_argument(
        "--stores", action="store_true",
        help="Restrict split/merge details to clusters whose stores list changed.",
    )
    args = parser.parse_args()

    base = Snapshot(args.baseline)
    cand = Snapshot(args.candidate)
    diff = compute_diff(base, cand)

    print_summary(base, cand, diff)
    print_category_table(base, cand, diff)
    print_split_details(base, cand, diff, args.max, args.category, args.stores)
    print_merge_details(base, cand, diff, args.max, args.category, args.stores)
    print()


if __name__ == "__main__":
    main()
