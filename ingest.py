"""
ingest.py
---------
Bulk-ingests scraped store data into the Supabase Postgres database.

Workflow:
  1. Load and validate category_mapping.csv → upsert into category_map table.
  2. For each store: COPY JSON into a temp staging table, map categories,
     upsert into store_products, and insert price_history only when
     price or availability actually changed.

Usage:
  python ingest.py                    # ingest all six stores
  python ingest.py istorm public      # ingest only these two stores
  python ingest.py --mark-disappeared # also mark unseen offers unavailable
"""

import argparse
import csv
import io
import json
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_STORES = {"istorm", "kotsovolos", "stephanis", "electroline", "public", "bionic"}

VALID_CATEGORIES = {
    "smartphones", "laptops", "tablets", "desktops", "monitors",
    "tvs", "smartwatches", "headphones", "speakers", "consoles",
    "cameras", "smart_home", "refrigerators", "washing_machines",
    "dryers", "dishwashers", "ovens", "air_conditioners", "vacuums",
    "coffee_machines", "air_fryers",
}

DATA_DIR = Path(__file__).parent / "data"
MAPPING_CSV = Path(__file__).parent / "category_mapping.csv"

# Columns we COPY into the staging table (order matters — must match the
# tab-delimited output we build in build_copy_buffer).
STAGING_COLS = [
    "store", "store_product_id", "title", "vendor", "product_type",
    "sku", "price", "available", "image_url", "product_url",
    "mpn", "mpn_root", "ean", "identifier_source", "scraped_at",
]

# ---------------------------------------------------------------------------
# Step A — Category mapping
# ---------------------------------------------------------------------------

def load_and_upsert_category_mapping(conn) -> dict[tuple[str, str], str]:
    """
    Read category_mapping.csv, validate canonical_category values,
    upsert valid rows into category_map, and return a lookup dict
    keyed by (store, raw_product_type) → canonical_category.
    """
    if not MAPPING_CSV.exists():
        print(f"ERROR: {MAPPING_CSV} not found. Run extract_categories.py first.")
        sys.exit(1)

    mapping: dict[tuple[str, str], str] = {}
    skipped = 0

    with open(MAPPING_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            store = row["store"].strip()
            raw_pt = row["raw_product_type"].strip()
            canon = row["canonical_category"].strip()

            # Rows with empty canonical_category are intentionally excluded
            if not canon:
                skipped += 1
                continue

            # Validate against the enum
            if canon not in VALID_CATEGORIES:
                print(
                    f"ERROR: Invalid canonical_category '{canon}' "
                    f"for store='{store}', raw_product_type='{raw_pt}'.\n"
                    f"Valid values: {', '.join(sorted(VALID_CATEGORIES))}"
                )
                sys.exit(1)

            mapping[(store, raw_pt)] = canon

    # Upsert all valid mappings into category_map in one go
    with conn.cursor() as cur:
        if mapping:
            values = [
                (store, raw_pt, canon)
                for (store, raw_pt), canon in mapping.items()
            ]
            cur.executemany(
                """
                INSERT INTO category_map (store, raw_product_type, category)
                VALUES (%s, %s, %s)
                ON CONFLICT (store, raw_product_type)
                DO UPDATE SET category = EXCLUDED.category
                """,
                values,
            )
    conn.commit()

    print(f"Category mapping: {len(mapping)} loaded, {skipped} skipped (empty).")
    return mapping


# ---------------------------------------------------------------------------
# Step B — Store ingestion
# ---------------------------------------------------------------------------

def _escape_copy_text(val: str) -> str:
    """Escape a string value for the Postgres COPY text format.

    Titles and other free-text fields can contain embedded newlines, carriage
    returns, or tabs that would otherwise corrupt the tab-delimited COPY stream.
    Replacements are applied in this order so that backslashes introduced by
    later steps are not double-escaped.
    """
    val = val.replace("\\", "\\\\")
    val = val.replace("\t", "\\t")
    val = val.replace("\n", "\\n")
    val = val.replace("\r", "\\r")
    return val


def build_copy_buffer(rows: list[dict]) -> io.StringIO:
    # raw docstring: it documents the literal backslash-N that Postgres COPY uses for NULL
    r"""
    Build a tab-delimited in-memory file suitable for COPY FROM.
    Handles NULL conversion (\N) and boolean casting.
    """
    buf = io.StringIO()
    for row in rows:
        fields = []
        for col in STAGING_COLS:
            val = row.get(col)
            if val is None:
                fields.append("\\N")
            elif col == "available":
                # Postgres expects 't'/'f' for boolean in text mode COPY
                fields.append(_escape_copy_text("t" if val else "f"))
            else:
                fields.append(_escape_copy_text(str(val)))
        buf.write("\t".join(fields) + "\n")
    buf.seek(0)
    return buf


def ingest_store(conn, store: str, category_map: dict[tuple[str, str], str],
                 mark_disappeared: bool = False):
    """
    Ingest a single store's JSON file:
      1. Load JSON → COPY into a temp staging table.
      2. Map categories via category_map.
      3. Upsert into store_products, tracking new/updated/unchanged.
      4. Insert price_history rows only for new + changed offers.
      5. Optionally mark store_products rows that did not appear in the
         scrape as unavailable (--mark-disappeared).
    Everything runs inside a single transaction per store.
    """
    json_path = DATA_DIR / f"{store}.json"
    if not json_path.exists():
        print(f"  WARNING: {json_path} not found — skipping {store}")
        return

    with open(json_path, encoding="utf-8") as f:
        rows = json.load(f)

    total = len(rows)
    if total == 0:
        print(f"  {store}: 0 rows in JSON — nothing to do.")
        return

    # Deduplicate on store_product_id, keeping the last occurrence for each id.
    # Some scrapers can emit the same product more than once (e.g. the same
    # product under both its English and Greek language URLs). The bulk
    # INSERT ... ON CONFLICT upsert requires a unique store_product_id per
    # batch — duplicates cause "cannot affect row a second time".
    seen: dict[str, dict] = {}
    for row in rows:
        seen[row["store_product_id"]] = row
    if len(seen) < total:
        rows = list(seen.values())
        print(f"  {store}: collapsed {total - len(rows)} duplicate store_product_id rows -> {len(rows)} unique")
        total = len(rows)

    # Collect unmapped (store, product_type) pairs for the summary
    unmapped_pairs: set[tuple[str, str]] = set()
    for row in rows:
        raw_pt = (row.get("product_type") or "").strip()
        if raw_pt and (store, raw_pt) not in category_map:
            unmapped_pairs.add((store, raw_pt))

    try:
        with conn.transaction():
            cur = conn.cursor()

            # -- Create temp staging table (dropped automatically at end of tx) --
            cur.execute("""
                CREATE TEMP TABLE _staging (
                    store           TEXT,
                    store_product_id TEXT,
                    title           TEXT,
                    vendor          TEXT,
                    product_type    TEXT,
                    sku             TEXT,
                    price           NUMERIC,
                    available       BOOLEAN,
                    image_url       TEXT,
                    product_url     TEXT,
                    mpn             TEXT,
                    mpn_root        TEXT,
                    ean             TEXT,
                    identifier_source TEXT,
                    scraped_at      TIMESTAMPTZ
                ) ON COMMIT DROP
            """)

            # -- Bulk load via COPY --
            copy_buf = build_copy_buffer(rows)
            with cur.copy("COPY _staging FROM STDIN") as copy:
                for line in copy_buf:
                    copy.write(line)

            # -- Snapshot the full scraped id set BEFORE the category filter --
            # This temp table captures every store_product_id the scraper saw,
            # including products whose (store, product_type) has no category
            # mapping.  We must compare against this pre-filter set when marking
            # disappeared offers; comparing against the post-filter _staging
            # would falsely mark uncategorised-but-still-existing products as
            # disappeared.  The table is ON COMMIT DROP so it lives only for
            # this transaction.
            cur.execute("""
                CREATE TEMP TABLE _scraped_ids ON COMMIT DROP AS
                SELECT store_product_id FROM _staging
            """)

            # -- Add a category column to staging, filled from category_map --
            cur.execute("ALTER TABLE _staging ADD COLUMN category TEXT")
            cur.execute("""
                UPDATE _staging s
                SET category = cm.category
                FROM category_map cm
                -- cm.store is store_name enum; s.store is text from COPY staging
                WHERE cm.store::text = s.store
                  AND cm.raw_product_type = s.product_type
            """)

            # Count and skip uncategorised products. The catalog only covers
            # the 21 canonical categories, so products whose (store, product_type)
            # has no mapping in category_map are intentionally excluded from
            # store_products and price_history to avoid storage bloat.
            cur.execute("SELECT COUNT(*) FROM _staging WHERE category IS NULL")
            skipped_no_category = cur.fetchone()[0]
            cur.execute("DELETE FROM _staging WHERE category IS NULL")

            # -- Upsert into store_products --
            # We use a CTE that returns which rows were actually inserted vs updated,
            # and whether price/availability changed (to decide about price_history).
            cur.execute("""
                WITH upsert AS (
                    INSERT INTO store_products (
                        store, store_product_id, title, vendor, product_type,
                        category, sku, mpn, mpn_root, ean, identifier_source,
                        current_price, available, image_url, product_url,
                        first_seen_at, last_scraped_at, last_changed_at
                    )
                    SELECT
                        s.store::store_name,       -- text → enum for INSERT
                        s.store_product_id,
                        s.title,
                        s.vendor,
                        s.product_type,
                        s.category::category,  -- staging column is text from COPY; target is the "category" enum
                        s.sku,
                        s.mpn,
                        s.mpn_root,
                        s.ean,
                        s.identifier_source::identifier_source,
                        s.price,
                        s.available,
                        s.image_url,
                        s.product_url,
                        NOW(),
                        NOW(),
                        NOW()
                    FROM _staging s
                    ON CONFLICT (store, store_product_id) DO UPDATE SET
                        title           = EXCLUDED.title,
                        vendor          = EXCLUDED.vendor,
                        product_type    = EXCLUDED.product_type,
                        category        = EXCLUDED.category,
                        sku             = EXCLUDED.sku,
                        mpn             = EXCLUDED.mpn,
                        mpn_root        = EXCLUDED.mpn_root,
                        ean             = EXCLUDED.ean,
                        identifier_source = EXCLUDED.identifier_source,
                        current_price   = EXCLUDED.current_price,
                        available       = EXCLUDED.available,
                        image_url       = EXCLUDED.image_url,
                        product_url     = EXCLUDED.product_url,
                        last_scraped_at = NOW(),
                        -- Only bump last_changed_at when price or availability differ
                        last_changed_at = CASE
                            WHEN store_products.current_price IS DISTINCT FROM EXCLUDED.current_price
                              OR store_products.available IS DISTINCT FROM EXCLUDED.available
                            THEN NOW()
                            ELSE store_products.last_changed_at
                        END
                    RETURNING
                        id,
                        current_price,
                        available,
                        -- xmax = 0 means the row was freshly inserted (no prior version)
                        (xmax = 0) AS is_insert,
                        -- Detect if price or availability actually changed on update
                        CASE
                            WHEN xmax = 0 THEN TRUE
                            ELSE (
                                current_price IS DISTINCT FROM (
                                    SELECT sp2.current_price FROM store_products sp2
                                    WHERE sp2.id = store_products.id
                                )
                            )
                        END AS dummy_flag
                )
                SELECT
                    COUNT(*) FILTER (WHERE is_insert)  AS inserted,
                    COUNT(*) FILTER (WHERE NOT is_insert) AS conflict_count
                FROM upsert
            """)
            inserted, conflict_count = cur.fetchone()

            # For price_history, we need to know which rows are new or changed.
            # We do this with a separate query that compares staging to current
            # store_products state. This avoids the tricky xmax approach for
            # detecting changes in RETURNING.

            # Insert price_history for ALL new rows (just inserted) and for
            # existing rows where price or availability changed.
            cur.execute("""
                INSERT INTO price_history (store_product_id, price, available, recorded_at)
                SELECT sp.id, sp.current_price, sp.available, NOW()
                FROM store_products sp
                JOIN _staging s
                  -- sp.store is store_name enum; s.store is text from COPY staging
                  ON sp.store::text = s.store
                 AND sp.store_product_id = s.store_product_id
                WHERE
                    -- New rows: first_seen_at equals last_scraped_at (just created)
                    sp.first_seen_at = sp.last_scraped_at
                    -- Or changed rows: last_changed_at equals last_scraped_at
                    OR sp.last_changed_at = sp.last_scraped_at
            """)
            price_history_written = cur.rowcount

            # Count how many were truly "changed" vs "unchanged" among conflicts.
            # Changed = last_changed_at was bumped to NOW (= last_scraped_at).
            # Unchanged = last_changed_at < last_scraped_at.
            cur.execute("""
                SELECT COUNT(*) FROM store_products sp
                JOIN _staging s
                  -- sp.store is store_name enum; s.store is text from COPY staging
                  ON sp.store::text = s.store
                 AND sp.store_product_id = s.store_product_id
                WHERE sp.first_seen_at < sp.last_scraped_at
                  AND sp.last_changed_at = sp.last_scraped_at
            """)
            updated = cur.fetchone()[0]
            unchanged = conflict_count - updated

            # -- Mark disappeared offers --
            # When --mark-disappeared is active, any store_products row for
            # this store that is currently available=true but whose
            # store_product_id was NOT in the scraper output is marked
            # unavailable.  This catches products the store has silently
            # removed from its catalog, preventing stale prices from winning
            # best-price comparisons.
            #
            # The "available = true" predicate makes this idempotent: a
            # second run with the same data finds nothing left to mark.
            # We do NOT touch last_scraped_at — these rows were genuinely
            # not scraped, so the freshness badge must reflect that.
            #
            # A price_history row is inserted for each disappeared offer so
            # that the availability transition is recorded, matching the
            # convention used for regular price/availability changes.
            disappeared_count = 0
            if mark_disappeared:
                cur.execute("""
                    UPDATE store_products
                    SET available = false,
                        last_changed_at = NOW()
                    WHERE store = %s::store_name
                      AND available = true
                      AND store_product_id NOT IN (
                          SELECT store_product_id FROM _scraped_ids
                      )
                    RETURNING id, current_price
                """, (store,))
                disappeared_rows = cur.fetchall()
                disappeared_count = len(disappeared_rows)

                # Record price_history for each disappeared offer so the
                # available=false transition appears in the price timeline.
                if disappeared_rows:
                    cur.executemany(
                        """
                        INSERT INTO price_history
                            (store_product_id, price, available, recorded_at)
                        VALUES (%s, %s, false, NOW())
                        """,
                        [(row[0], row[1]) for row in disappeared_rows],
                    )

        # Print per-store summary
        print(f"  {store}: {total} read, {skipped_no_category} skipped (no category), "
              f"{inserted} inserted, {updated} updated, {unchanged} unchanged, "
              f"{price_history_written} price_history rows, "
              f"{disappeared_count} marked disappeared")

        # Report unmapped categories
        if unmapped_pairs:
            print(f"  UNMAPPED CATEGORIES for {store}:")
            for s, pt in sorted(unmapped_pairs):
                print(f"    - {pt}")

    except Exception as e:
        # Transaction is automatically rolled back by the context manager
        print(f"  ERROR ingesting {store}: {e}")
        raise


# ---------------------------------------------------------------------------
# CLI and main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Ingest scraped store data into the database."
    )
    parser.add_argument(
        "stores",
        nargs="*",
        default=list(VALID_STORES),
        help="Stores to ingest (default: all six).",
    )
    parser.add_argument(
        "--mark-disappeared",
        action="store_true",
        default=False,
        help="After a successful scrape, mark store_products rows that did "
             "not appear in the scrape as unavailable. Safe for nightly full "
             "scrapes; omit for manual partial ingests.",
    )
    return parser.parse_args()


def main():
    load_dotenv()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set. Copy .env.example to .env and fill it in.")
        sys.exit(1)

    args = parse_args()

    # Validate store names before doing anything
    for store in args.stores:
        if store not in VALID_STORES:
            print(f"ERROR: Unknown store '{store}'. Valid: {', '.join(sorted(VALID_STORES))}")
            sys.exit(1)

    with psycopg.connect(db_url) as conn:
        # Disable automatic prepared statements — they conflict with
        # pgbouncer transaction-mode pooling (which Supabase uses by
        # default), causing "DuplicatePreparedStatement" errors when a
        # pooled server connection is reused.  Mirrors the existing guard
        # in matching/writer.py and matching/load.py.
        conn.prepare_threshold = None

        # Step A: load category mapping into DB and into a local lookup dict
        category_map = load_and_upsert_category_mapping(conn)

        # Step B: ingest each requested store
        print()
        for store in sorted(args.stores):
            ingest_store(conn, store, category_map, args.mark_disappeared)

    print("\nDone.")


if __name__ == "__main__":
    main()
