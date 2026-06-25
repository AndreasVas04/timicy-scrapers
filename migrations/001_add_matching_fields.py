#!/usr/bin/env python3
"""
001_add_matching_fields.py
--------------------------
Additive, idempotent migration that adds matching-layer columns, an enum
value, and an index to the products / store_products tables.

Running this script twice is a safe no-op — every statement uses
IF NOT EXISTS or ADD VALUE IF NOT EXISTS.

No destructive changes: no DROP, no DELETE, no ALTER TYPE ... RENAME,
no column removal.

Usage:
    # Dry run (default) — prints what it WOULD do, touches nothing:
    python -m migrations.001_add_matching_fields

    # Actually apply the migration:
    python -m migrations.001_add_matching_fields --apply

NOTE ON CONNECTION POOLING:
    If DATABASE_URL points at the Supabase pgbouncer transaction pooler
    (port 6543), the ALTER TYPE ... ADD VALUE step may fail because it
    requires a direct PostgreSQL connection. In that case, temporarily
    switch DATABASE_URL to the direct connection string (port 5432) for
    this migration only.
"""

import argparse
import os
import sys

import psycopg
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# SQL statements
# ---------------------------------------------------------------------------
# Separated into two groups because ALTER TYPE ... ADD VALUE cannot run
# inside a transaction block, and a newly added enum value cannot be used
# in the same transaction that adds it.

# Step 1 — must run in AUTOCOMMIT mode, outside any transaction.
# Adds the 'model_code' value to the existing match_method enum.
STEP1_ENUM_SQL = (
    "ALTER TYPE match_method ADD VALUE IF NOT EXISTS 'model_code';",
)

# Step 2 — runs inside a single explicit transaction so that either all
# columns and the index are added, or none are (atomic rollback on error).
STEP2_COLUMNS_SQL = (
    # -- products table: matching metadata columns --
    "ALTER TABLE products "
    "ADD COLUMN IF NOT EXISTS match_method match_method NOT NULL DEFAULT 'unmatched';",

    "ALTER TABLE products "
    "ADD COLUMN IF NOT EXISTS match_key text;",

    "ALTER TABLE products "
    "ADD COLUMN IF NOT EXISTS needs_review boolean NOT NULL DEFAULT false;",

    "ALTER TABLE products "
    "ADD COLUMN IF NOT EXISTS review_reason text;",

    # -- store_products table: per-offer match method --
    # The constant DEFAULT 'unmatched' is a metadata-only change in modern
    # PostgreSQL (>= 11) and will NOT rewrite the existing 17k rows.
    "ALTER TABLE store_products "
    "ADD COLUMN IF NOT EXISTS match_method match_method NOT NULL DEFAULT 'unmatched';",

    # -- Index on products.match_key for fast lookups during matching --
    "CREATE INDEX IF NOT EXISTS idx_products_match_key ON products (match_key);",
)


# ---------------------------------------------------------------------------
# Verification queries — run after applying (or described in dry-run).
# ---------------------------------------------------------------------------

def verify_state(cur: psycopg.Cursor) -> None:
    """Query and print the post-migration state so we can confirm success.

    Checks three things:
      1. Current values of the match_method enum.
      2. Whether the new columns exist on products and store_products.
      3. Whether idx_products_match_key exists.
    """
    print()
    print("=" * 70)
    print("  VERIFICATION")
    print("=" * 70)

    # 1. Enum values for match_method.
    cur.execute(
        """
        SELECT e.enumlabel
        FROM   pg_type t
        JOIN   pg_enum e ON e.enumtypid = t.oid
        WHERE  t.typname = 'match_method'
        ORDER  BY e.enumsortorder
        """
    )
    values = [row[0] for row in cur.fetchall()]
    print(f"\n  match_method enum values: {values}")
    if "model_code" in values:
        print("    'model_code' is present.")
    else:
        print("    WARNING: 'model_code' is MISSING.")

    # 2. Check for expected columns on both tables.
    expected = {
        "products": ["match_method", "match_key", "needs_review", "review_reason"],
        "store_products": ["match_method"],
    }
    for table, cols in expected.items():
        cur.execute(
            """
            SELECT column_name
            FROM   information_schema.columns
            WHERE  table_schema = 'public'
              AND  table_name   = %s
              AND  column_name  = ANY(%s)
            """,
            (table, cols),
        )
        found = {row[0] for row in cur.fetchall()}
        missing = set(cols) - found
        print(f"\n  {table} columns:")
        for col in cols:
            status = "exists" if col in found else "MISSING"
            print(f"    {col}: {status}")
        if missing:
            print(f"    WARNING: missing columns: {sorted(missing)}")

    # 3. Check for the match_key index.
    cur.execute(
        """
        SELECT indexname
        FROM   pg_indexes
        WHERE  schemaname = 'public'
          AND  tablename  = 'products'
          AND  indexname  = 'idx_products_match_key'
        """
    )
    idx_row = cur.fetchone()
    print(f"\n  idx_products_match_key: {'exists' if idx_row else 'MISSING'}")
    print()


# ---------------------------------------------------------------------------
# Pre-flight check — report what already exists vs. what will be added.
# ---------------------------------------------------------------------------

def preflight_report(cur: psycopg.Cursor) -> None:
    """Print a before summary: which columns/values already exist."""
    print()
    print("=" * 70)
    print("  PRE-FLIGHT: current state")
    print("=" * 70)

    # Enum values.
    cur.execute(
        """
        SELECT e.enumlabel
        FROM   pg_type t
        JOIN   pg_enum e ON e.enumtypid = t.oid
        WHERE  t.typname = 'match_method'
        ORDER  BY e.enumsortorder
        """
    )
    values = [row[0] for row in cur.fetchall()]
    has_model_code = "model_code" in values
    print(f"\n  match_method enum: {values}")
    print(f"    'model_code' already present: {'YES' if has_model_code else 'NO (will be added)'}")

    # Columns.
    targets = {
        "products": ["match_method", "match_key", "needs_review", "review_reason"],
        "store_products": ["match_method"],
    }
    for table, cols in targets.items():
        cur.execute(
            """
            SELECT column_name
            FROM   information_schema.columns
            WHERE  table_schema = 'public'
              AND  table_name   = %s
            """,
            (table,),
        )
        existing = {row[0] for row in cur.fetchall()}
        print(f"\n  {table}:")
        for col in cols:
            status = "already exists" if col in existing else "will be added"
            print(f"    {col}: {status}")

    # Index.
    cur.execute(
        """
        SELECT 1
        FROM   pg_indexes
        WHERE  schemaname = 'public'
          AND  tablename  = 'products'
          AND  indexname  = 'idx_products_match_key'
        """
    )
    idx_exists = cur.fetchone() is not None
    print(f"\n  idx_products_match_key: {'already exists' if idx_exists else 'will be created'}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add matching-layer schema fields (additive, idempotent).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually execute the migration. Without this flag, only a dry run is performed.",
    )
    args = parser.parse_args()
    dry_run = not args.apply

    # Load DATABASE_URL the same way ingest.py does.
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
        # Open a connection for the pre-flight check and (if applying) step 1.
        # Step 1 needs autocommit because ALTER TYPE ... ADD VALUE cannot run
        # inside a transaction block.
        conn = psycopg.connect(db_url, autocommit=True)
        cur = conn.cursor()

        # -- Pre-flight report --
        preflight_report(cur)

        # ---------------------------------------------------------------
        # Step 1: Add 'model_code' to the match_method enum.
        # Runs in autocommit mode so the new value is immediately visible.
        # ---------------------------------------------------------------
        print("-" * 70)
        print("  STEP 1: enum value (autocommit, outside transaction)")
        print("-" * 70)
        for sql in STEP1_ENUM_SQL:
            if dry_run:
                print(f"  [DRY RUN] {sql}")
            else:
                print(f"  Executing: {sql}")
                cur.execute(sql)
                print("  Done.")
        print()

        # Close the autocommit connection before opening a transactional one.
        conn.close()
        conn = None

        # ---------------------------------------------------------------
        # Step 2: Add columns and index inside a single transaction.
        # If any statement fails, the entire transaction is rolled back so
        # the database is left unchanged.
        # ---------------------------------------------------------------
        print("-" * 70)
        print("  STEP 2: columns + index (single transaction)")
        print("-" * 70)

        if dry_run:
            for sql in STEP2_COLUMNS_SQL:
                print(f"  [DRY RUN] {sql}")
            print()
            print("Dry run complete. Re-run with --apply to execute.")
            # Still show what verification would check.
            print("\n  Verification would confirm: match_method enum values,")
            print("  new columns on products/store_products, and idx_products_match_key.")
        else:
            # Open a new connection with default autocommit=False (transactional).
            conn = psycopg.connect(db_url)
            cur = conn.cursor()
            try:
                for sql in STEP2_COLUMNS_SQL:
                    print(f"  Executing: {sql}")
                    cur.execute(sql)
                conn.commit()
                print("\n  Transaction committed.")
            except Exception:
                conn.rollback()
                print("\n  ERROR — transaction rolled back. No changes were made in step 2.")
                raise

            # -- Post-migration verification --
            verify_state(cur)

    finally:
        # Always close the connection cleanly, even on error.
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()
