#!/usr/bin/env python3
"""
inspect_normalization.py
------------------------
Local CLI tool for eyeballing normalization output against real scraper JSON.

READ-ONLY: never writes any file or touches any database.

Usage examples:
    python -m matching.inspect_normalization --store stephanis
    python -m matching.inspect_normalization --store public --limit 50
    python -m matching.inspect_normalization data/istorm.json --limit 10
"""

import argparse
import json
import sys
from pathlib import Path

# Support running both as a package module (-m matching.inspect_normalization)
# and directly (python3 matching/inspect_normalization.py).
try:
    from .normalize import (
        extract_brand_from_title,
        extract_model_codes,
        looks_suspicious_brand,
        normalize_brand,
        normalize_title,
    )
except ImportError:
    from normalize import (
        extract_brand_from_title,
        extract_model_codes,
        looks_suspicious_brand,
        normalize_brand,
        normalize_title,
    )


DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def load_records(source: str) -> list[dict]:
    """Load product records from a JSON file or a --store name."""
    path = Path(source)
    if not path.exists():
        # Maybe it is a bare store name — try data/<store>.json
        path = DATA_DIR / f"{source}.json"
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(
        description="Eyeball normalization output against scraper JSON."
    )
    parser.add_argument(
        "file",
        nargs="?",
        default=None,
        help="Path to a JSON file (e.g. data/public.json).",
    )
    parser.add_argument(
        "--store", "-s",
        default=None,
        help="Store name (looks up data/<store>.json).",
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=30,
        help="Maximum number of records to display (default: 30).",
    )
    args = parser.parse_args()

    source = args.file or args.store
    if not source:
        parser.error("Provide a JSON file path or --store name.")

    records = load_records(source)
    limit = min(args.limit, len(records))

    print(f"Showing {limit} of {len(records)} records\n")
    print("=" * 100)

    for rec in records[:limit]:
        raw_title = rec.get("title", "")
        raw_vendor = rec.get("vendor", "")

        brand = normalize_brand(raw_vendor)
        title = normalize_title(raw_title, brand)

        # Extract model codes from the raw title for cross-language matching.
        model_codes = extract_model_codes(raw_title)

        # Check whether the vendor-derived brand looks suspicious.
        suspicious = looks_suspicious_brand(brand, raw_title)
        suggested = None
        if suspicious:
            suggested = extract_brand_from_title(raw_title)

        print(f"  vendor:  {raw_vendor!r:40s}  ->  {brand}")
        print(f"  title:   {raw_title}")
        print(f"  normal:  {title}")
        print(f"  models:  {model_codes}")
        if suspicious:
            flag = f"  brand?:  SUSPICIOUS (suggested: {suggested})"
            print(flag)
        print("-" * 100)


if __name__ == "__main__":
    main()
