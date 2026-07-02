#!/usr/bin/env python3
"""
002_merge_support.py
--------------------
Additive, idempotent migration that creates the merged_products table (with
RLS), and adds a store_count column to products.

Running this script twice is a safe no-op — every statement uses
IF NOT EXISTS guards or catalog checks before acting.

No destructive changes: no DROP of existing objects, no DELETE, no column
removal.

Usage:
    # Dry run (default) — prints what it WOULD do, touches nothing:
    python -m migrations.002_merge_support

    # Actually apply the migration:
    python -m migrations.002_merge_support --apply
"""

import argparse
import os
import sys

import psycopg
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# SQL statements
# ---------------------------------------------------------------------------
# All DDL runs inside a single explicit transaction — no enum changes are
# needed, so there is no autocommit split.  If any statement fails, the
# entire transaction is rolled back and the database is left unchanged.

MIGRATION_SQL = (
    # -- Section 1: merged_products table --
    # Stores the mapping from absorbed (old) product IDs to surviving (new)
    # product IDs after a merge.  old_id has NO foreign key because the
    # original products row is deleted during the merge; new_id references the
    # surviving product and cascades on delete.
    """
    CREATE TABLE IF NOT EXISTS merged_products (
        old_id      integer         PRIMARY KEY,
        new_id      integer         NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        merged_at   timestamptz     NOT NULL DEFAULT now()
    );
    """,

    # -- Index on new_id for reverse lookups and chain compression --
    # Allows efficient queries like "find all old IDs that were merged into
    # this survivor" and supports chain-compression walks.
    "CREATE INDEX IF NOT EXISTS idx_merged_products_new_id "
    "ON merged_products (new_id);",

    # -- Section 2: Row Level Security on merged_products --
    # The frontend (anon role via PostgREST/Supabase) needs read access to
    # issue permanent redirects for absorbed product IDs.
    "ALTER TABLE merged_products ENABLE ROW LEVEL SECURITY;",

    # -- Section 3: products.store_count column --
    # Tracks how many distinct stores carry this product.  The DEFAULT 0 is a
    # metadata-only change in modern PostgreSQL (>= 11) and will NOT rewrite
    # existing rows.
    "ALTER TABLE products "
    "ADD COLUMN IF NOT EXISTS store_count integer NOT NULL DEFAULT 0;",
)

# -- Backfill store_count from store_products --
# Runs as a separate statement after the column is guaranteed to exist.
# Products with no linked offers keep the DEFAULT 0.
BACKFILL_STORE_COUNT_SQL = """
    UPDATE products p
    SET    store_count = sub.cnt
    FROM   (
        SELECT product_id, count(DISTINCT store) AS cnt
        FROM   store_products
        WHERE  product_id IS NOT NULL
        GROUP  BY product_id
    ) sub
    WHERE  p.id = sub.product_id
      AND  p.store_count IS DISTINCT FROM sub.cnt;
"""

# -- RLS policy creation --
# Handled procedurally (see _ensure_rls_policy) because CREATE POLICY has no
# IF NOT EXISTS guard.  We check the catalog first and only create if missing.
RLS_POLICY_NAME = "anon_select_merged_products"
RLS_POLICY_SQL = (
    f"CREATE POLICY {RLS_POLICY_NAME} ON merged_products "
    "FOR SELECT TO anon USING (true);"
)


# ---------------------------------------------------------------------------
# Helper: idempotent RLS policy creation
# ---------------------------------------------------------------------------

def _ensure_rls_policy(cur: psycopg.Cursor, *, dry_run: bool) -> None:
    """Create the anon SELECT policy on merged_products if it does not exist.

    CREATE POLICY lacks an IF NOT EXISTS clause, so we query pg_policies to
    check first.  This keeps the migration idempotent on re-runs.
    """
    cur.execute(
        """
        SELECT 1
        FROM   pg_policies
        WHERE  schemaname = 'public'
          AND  tablename  = 'merged_products'
          AND  policyname = %s
        """,
        (RLS_POLICY_NAME,),
    )
    if cur.fetchone() is not None:
        print(f"    Policy '{RLS_POLICY_NAME}' already exists — skipping.")
        return

    if dry_run:
        print(f"  [DRY RUN] {RLS_POLICY_SQL}")
    else:
        print(f"  Executing: {RLS_POLICY_SQL}")
        cur.execute(RLS_POLICY_SQL)
        print("  Done.")


# ---------------------------------------------------------------------------
# Verification queries — run after applying (or described in dry-run).
# ---------------------------------------------------------------------------

def verify_state(cur: psycopg.Cursor) -> None:
    """Query and print the post-migration state so we can confirm success.

    Checks four things:
      1. merged_products table existence and its columns.
      2. RLS policy presence on merged_products.
      3. store_count column on products.
      4. Sample of 5 products showing offer_count vs store_count.
    """
    print()
    print("=" * 70)
    print("  VERIFICATION")
    print("=" * 70)

    # 1. merged_products table and columns.
    cur.execute(
        """
        SELECT column_name, data_type, is_nullable
        FROM   information_schema.columns
        WHERE  table_schema = 'public'
          AND  table_name   = 'merged_products'
        ORDER  BY ordinal_position
        """
    )
    rows = cur.fetchall()
    if rows:
        print("\n  merged_products table: exists")
        for col_name, data_type, nullable in rows:
            print(f"    {col_name}: {data_type} (nullable: {nullable})")
    else:
        print("\n  merged_products table: MISSING")

    # 1b. Index on new_id.
    cur.execute(
        """
        SELECT indexname
        FROM   pg_indexes
        WHERE  schemaname = 'public'
          AND  tablename  = 'merged_products'
          AND  indexname  = 'idx_merged_products_new_id'
        """
    )
    idx_row = cur.fetchone()
    print(f"\n  idx_merged_products_new_id: {'exists' if idx_row else 'MISSING'}")

    # 2. RLS policy.
    cur.execute(
        """
        SELECT policyname
        FROM   pg_policies
        WHERE  schemaname = 'public'
          AND  tablename  = 'merged_products'
          AND  policyname = %s
        """,
        (RLS_POLICY_NAME,),
    )
    policy_row = cur.fetchone()
    print(f"  RLS policy '{RLS_POLICY_NAME}': {'exists' if policy_row else 'MISSING'}")

    # 3. store_count column on products.
    cur.execute(
        """
        SELECT column_name
        FROM   information_schema.columns
        WHERE  table_schema = 'public'
          AND  table_name   = 'products'
          AND  column_name  = 'store_count'
        """
    )
    sc_row = cur.fetchone()
    print(f"  products.store_count: {'exists' if sc_row else 'MISSING'}")

    # 4. Sample: 5 products with offer_count vs store_count side by side.
    cur.execute(
        """
        SELECT id, offer_count, store_count
        FROM   products
        ORDER  BY id
        LIMIT  5
        """
    )
    sample = cur.fetchall()
    if sample:
        print("\n  Sample products (id, offer_count, store_count):")
        for pid, oc, sc in sample:
            print(f"    product {pid}: offer_count={oc}, store_count={sc}")
    else:
        print("\n  (no products in table — cannot show sample)")

    print()


# ---------------------------------------------------------------------------
# Pre-flight check — report what already exists vs. what will be added.
# ---------------------------------------------------------------------------

def preflight_report(cur: psycopg.Cursor) -> None:
    """Print a before summary: which objects already exist."""
    print()
    print("=" * 70)
    print("  PRE-FLIGHT: current state")
    print("=" * 70)

    # 1. merged_products table.
    cur.execute(
        """
        SELECT 1
        FROM   information_schema.tables
        WHERE  table_schema = 'public'
          AND  table_name   = 'merged_products'
        """
    )
    table_exists = cur.fetchone() is not None
    print(f"\n  merged_products table: {'already exists' if table_exists else 'will be created'}")

    # 2. Index on new_id.
    cur.execute(
        """
        SELECT 1
        FROM   pg_indexes
        WHERE  schemaname = 'public'
          AND  tablename  = 'merged_products'
          AND  indexname  = 'idx_merged_products_new_id'
        """
    )
    idx_exists = cur.fetchone() is not None
    print(f"  idx_merged_products_new_id: {'already exists' if idx_exists else 'will be created'}")

    # 3. RLS policy.
    cur.execute(
        """
        SELECT 1
        FROM   pg_policies
        WHERE  schemaname = 'public'
          AND  tablename  = 'merged_products'
          AND  policyname = %s
        """,
        (RLS_POLICY_NAME,),
    )
    policy_exists = cur.fetchone() is not None
    print(f"  RLS policy '{RLS_POLICY_NAME}': {'already exists' if policy_exists else 'will be created'}")

    # 4. store_count column on products.
    cur.execute(
        """
        SELECT 1
        FROM   information_schema.columns
        WHERE  table_schema = 'public'
          AND  table_name   = 'products'
          AND  column_name  = 'store_count'
        """
    )
    col_exists = cur.fetchone() is not None
    print(f"  products.store_count: {'already exists' if col_exists else 'will be added'}")

    # 5. Current products count (context for backfill).
    cur.execute("SELECT count(*) FROM products")
    total = cur.fetchone()[0]
    print(f"\n  Total products rows: {total}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add merge-support schema (merged_products table, RLS, store_count). Additive, idempotent.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually execute the migration. Without this flag, only a dry run is performed.",
    )
    args = parser.parse_args()
    dry_run = not args.apply

    # Load DATABASE_URL the same way ingest.py and the other migrations do.
    load_dotenv()
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set. Copy .env.example to .env and fill it in.")
        sys.exit(1)

    if dry_run:
        print("DRY RUN — no changes will be made. Pass --apply to execute.\n")
    else:
        print("APPLYING migration to the database.\n")

    conn = None
    try:
        # Open a connection for the pre-flight check.  We use autocommit here
        # so the read-only pre-flight queries don't hold an open transaction.
        conn = psycopg.connect(db_url, autocommit=True)
        cur = conn.cursor()

        # -- Pre-flight report --
        preflight_report(cur)

        # Close the autocommit connection before opening a transactional one.
        conn.close()
        conn = None

        # ---------------------------------------------------------------
        # Single transaction: table + index + RLS + column + backfill.
        # No enum changes needed, so everything fits in one transaction.
        # If any statement fails, the entire transaction is rolled back and
        # the database is left unchanged.
        # ---------------------------------------------------------------
        print("-" * 70)
        print("  STEP 1: table, index, RLS, column, backfill (single transaction)")
        print("-" * 70)

        if dry_run:
            for sql in MIGRATION_SQL:
                print(f"  [DRY RUN] {sql.strip()}")
            # Show the policy creation that would happen.
            print(
                f"  [DRY RUN] {RLS_POLICY_SQL}"
                "\n    (only if policy does not already exist — checked via pg_policies)"
            )
            print(f"  [DRY RUN] {BACKFILL_STORE_COUNT_SQL.strip()}")
            print()
            print("Dry run complete. Re-run with --apply to execute.")
            print("\n  Verification would confirm: merged_products table + columns,")
            print("  idx_merged_products_new_id, RLS policy, products.store_count,")
            print("  and a sample of offer_count vs store_count.")
        else:
            # Open a new connection with default autocommit=False (transactional).
            conn = psycopg.connect(db_url)
            cur = conn.cursor()
            try:
                # DDL: create table, index, enable RLS, add column.
                for sql in MIGRATION_SQL:
                    print(f"  Executing: {sql.strip()}")
                    cur.execute(sql)

                # RLS policy (idempotent via catalog check).
                _ensure_rls_policy(cur, dry_run=False)

                # Backfill store_count from store_products.
                print(f"  Executing: backfill store_count ...")
                cur.execute(BACKFILL_STORE_COUNT_SQL)
                rows_updated = cur.rowcount
                print(f"  Backfilled {rows_updated} product(s).")

                conn.commit()
                print("\n  Transaction committed.")
            except Exception:
                conn.rollback()
                print("\n  ERROR — transaction rolled back. No changes were made.")
                raise

            # -- Post-migration verification --
            verify_state(cur)

    finally:
        # Always close the connection cleanly, even on error.
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()
