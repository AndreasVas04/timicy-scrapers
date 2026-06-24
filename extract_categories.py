"""
extract_categories.py
---------------------
Scans the scraped JSON files in data/ and produces a CSV listing every
distinct (store, product_type) pair with its occurrence count.
The developer then fills in the canonical_category column by hand
before running ingest.py.
"""

import csv
import json
import os
from collections import Counter
from pathlib import Path

# All stores we expect to have data for
STORES = ["istorm", "kotsovolos", "stephanis", "electroline", "public", "bionic"]
DATA_DIR = Path(__file__).parent / "data"
OUTPUT_CSV = Path(__file__).parent / "category_mapping.csv"


def load_store_data(store: str) -> list[dict] | None:
    """Load a store's JSON file, returning None if the file is missing."""
    path = DATA_DIR / f"{store}.json"
    if not path.exists():
        print(f"  WARNING: {path} not found — skipping {store}")
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def count_product_types(rows: list[dict]) -> Counter:
    """Count occurrences of each product_type, treating empty/missing as '(empty)'."""
    counter: Counter[str] = Counter()
    for row in rows:
        raw = (row.get("product_type") or "").strip()
        key = raw if raw else "(empty)"
        counter[key] += 1
    return counter


def main():
    # Collect (store, product_type) → count for all stores
    all_rows: list[tuple[str, str, int]] = []

    for store in STORES:
        data = load_store_data(store)
        if data is None:
            continue

        counts = count_product_types(data)

        # Print to console, sorted by count descending
        print(f"\n{'=' * 50}")
        print(f"  {store}  ({len(data)} products)")
        print(f"{'=' * 50}")
        for ptype, count in counts.most_common():
            print(f"  {count:>6}  {ptype}")

        # Accumulate rows for CSV
        for ptype, count in counts.most_common():
            all_rows.append((store, ptype, count))

    # Write category_mapping.csv — canonical_category left empty for the developer
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["store", "raw_product_type", "product_count", "canonical_category"])
        for store, ptype, count in all_rows:
            writer.writerow([store, ptype, count, ""])

    print(f"\nWrote {len(all_rows)} rows to {OUTPUT_CSV}")
    print("Fill in the 'canonical_category' column, then run ingest.py.")


if __name__ == "__main__":
    main()
