"""
union_find.py
-------------
Generic union-find (disjoint-set) data structure over integer indices.

This class knows nothing about offers, tiers, or match keys — it is
purely index-based and reusable by any tier that needs to merge groups.

Supports path compression and union by size for near-constant-time
operations.  Deterministic tie-breaking ensures identical output across
runs for the same input sequence.
"""


class UnionFind:
    """Disjoint-set forest over indices 0..n-1.

    Provides path compression on find() and union by size on union().
    Tie-breaking is deterministic: when two trees have equal size, the
    smaller root index becomes the new root.
    """

    def __init__(self, n: int) -> None:
        """Create *n* disjoint singletons with indices 0 through n-1."""
        # _parent[i] is the parent of node i; initially each node is its own root.
        self._parent: list[int] = list(range(n))
        # _size[i] is the size of the tree rooted at i (only meaningful at roots).
        self._size: list[int] = [1] * n
        self._n = n

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def find(self, x: int) -> int:
        """Return the root representative of the set containing *x*.

        Uses path compression: every node on the path from *x* to the root
        is repointed directly to the root, flattening the tree for future
        lookups.
        """
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression: point every node on the path directly to root.
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        """Merge the sets containing *a* and *b*.

        Uses union by size: the smaller tree is attached under the larger
        tree's root.  When sizes are equal, the smaller root index becomes
        the new root — this deterministic tie-break ensures stable output
        across runs.
        """
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return  # Already in the same set.

        # Ensure ra is the root that will survive (the larger tree, or
        # the smaller index on a tie).
        if self._size[ra] < self._size[rb]:
            ra, rb = rb, ra
        elif self._size[ra] == self._size[rb] and ra > rb:
            # Deterministic tie-break: smaller index wins.
            ra, rb = rb, ra

        # Attach rb's tree under ra.
        self._parent[rb] = ra
        self._size[ra] += self._size[rb]

    def connected(self, a: int, b: int) -> bool:
        """Return True if *a* and *b* are in the same set."""
        return self.find(a) == self.find(b)

    # ------------------------------------------------------------------
    # Cluster extraction
    # ------------------------------------------------------------------

    def groups(self) -> list[list[int]]:
        """Return all clusters as a list of sorted member-lists.

        Each inner list contains the indices belonging to one cluster,
        sorted ascending.  The outer list is sorted by the smallest
        member of each cluster (i.e. by the first element of each inner
        list), giving fully deterministic output.

        Singletons are included — filter on len() >= 2 if you only want
        non-trivial clusters.
        """
        clusters: dict[int, list[int]] = {}
        for i in range(self._n):
            root = self.find(i)
            clusters.setdefault(root, []).append(i)
        # Sort members within each cluster, then sort clusters by their
        # smallest member for deterministic ordering.
        result = [sorted(members) for members in clusters.values()]
        result.sort(key=lambda c: c[0])
        return result
