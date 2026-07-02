"""
load.py
-------
Read-only offer loader for the matching layer.

SELECTs all rows from store_products, computes derived normalization fields
once per row, and returns a list of frozen EnrichedOffer dataclass instances.

This module contains NO matching logic and performs NO database writes.
"""

import os
import sys
from dataclasses import dataclass, field

import psycopg
from dotenv import load_dotenv

from .normalize import (
    clean_ean,
    ean_key,
    extract_brand_from_title,
    extract_model_codes,
    looks_suspicious_brand,
    mpn_key,
    mpn_root_key,
    normalize_brand,
    normalize_title,
    title_key,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EnrichedOffer:
    """A store_products row enriched with derived normalization fields.

    Raw fields come directly from the database.  Derived fields are computed
    once at construction via the normalization module and are never mutated.
    """

    # -- Raw fields (exactly as stored in store_products) --
    store: str
    store_product_id: str
    title: str
    vendor: str | None
    category: str | None
    sku: str | None
    price: object  # Decimal or None
    available: bool
    image_url: str | None
    product_url: str
    mpn: str | None
    mpn_root: str | None
    ean: str | None
    identifier_source: str

    # -- Derived fields (computed from raw fields via normalization) --
    brand_norm: str = ""
    title_norm: str = ""
    title_key: str | None = None
    ean_key: str | None = None
    mpn_root_key: str | None = None
    mpn_key: str | None = None
    model_codes: tuple[str, ...] = ()  # tuple for frozen dataclass hashability
    is_suspicious_brand: bool = False
    brand_from_title: str | None = None
    effective_brand: str | None = None


def _build_offer(row: dict) -> EnrichedOffer:
    """Construct an EnrichedOffer from a raw database row dict.

    All derived fields are computed here, once, using the locked
    normalization functions. This keeps the dataclass frozen and the
    derivation logic in one place.
    """
    raw_title = row["title"]
    raw_vendor = row["vendor"]
    raw_category = row["category"]
    raw_ean = row["ean"]
    raw_mpn = row["mpn"]
    raw_mpn_root = row["mpn_root"]

    # -- Brand normalization and trust checks --
    brand_norm = normalize_brand(raw_vendor)
    suspicious = looks_suspicious_brand(brand_norm, raw_title)
    brand_from_title_val = extract_brand_from_title(raw_title)

    # effective_brand: use the vendor-derived brand if it looks trustworthy,
    # otherwise fall back to whatever the title yields (may be None).
    if not suspicious:
        effective_brand = brand_norm
    else:
        effective_brand = brand_from_title_val

    # -- Title normalization (uses effective brand for brand removal) --
    t_norm = normalize_title(raw_title, brand_norm)

    # -- Match-key builders --
    # title_key needs brand, category, and normalized title.
    t_key = title_key(brand_norm, raw_category or "", t_norm) or None

    # ean_key returns "" for invalid EANs; normalize to None.
    e_key = ean_key(raw_ean or "") or None

    # mpn_root_key and mpn_key use effective_brand (the trusted brand
    # resolution) instead of raw brand_norm.  The vendor field is unreliable
    # at several stores (null vendors, typos like "PHLIPS", product lines
    # used as vendor).  effective_brand is the same trusted resolution that
    # model_code and title tiers consume: the vendor-derived brand when
    # trustworthy, otherwise the brand extracted from the title.  Building
    # all identity keys from the same trusted brand ensures offers with
    # defective vendor data are not invisible to identifier-based matching.
    mr_key = mpn_root_key(effective_brand or "", raw_mpn_root or "") or None
    m_key = mpn_key(effective_brand or "", raw_mpn or "") or None

    # -- Model-code extraction --
    codes = extract_model_codes(raw_title or "")

    return EnrichedOffer(
        # Raw fields
        store=row["store"],
        store_product_id=row["store_product_id"],
        title=raw_title or "",
        vendor=raw_vendor,
        category=raw_category,
        sku=row["sku"],
        price=row["current_price"],
        available=row["available"],
        image_url=row["image_url"],
        product_url=row["product_url"],
        mpn=raw_mpn,
        mpn_root=raw_mpn_root,
        ean=raw_ean,
        identifier_source=row["identifier_source"],
        # Derived fields
        brand_norm=brand_norm,
        title_norm=t_norm,
        title_key=t_key,
        ean_key=e_key,
        mpn_root_key=mr_key,
        mpn_key=m_key,
        model_codes=tuple(codes),
        is_suspicious_brand=suspicious,
        brand_from_title=brand_from_title_val,
        effective_brand=effective_brand,
    )


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_connection() -> psycopg.Connection:
    """Open a psycopg connection using DATABASE_URL from the .env file.

    Uses the same connection approach as the ingestion pipeline (ingest.py).
    Never hardcodes credentials.
    """
    load_dotenv()
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set. Copy .env.example to .env and fill it in.")
        sys.exit(1)
    return psycopg.connect(db_url)


# ---------------------------------------------------------------------------
# Offer loader
# ---------------------------------------------------------------------------

# The SELECT pulls every raw field we need from store_products.
# No JOINs, no writes, no subqueries that mutate state.
_LOAD_SQL = """
SELECT store,
       store_product_id,
       title,
       vendor,
       category,
       sku,
       current_price,
       available,
       image_url,
       product_url,
       mpn,
       mpn_root,
       ean,
       identifier_source
FROM   store_products
ORDER  BY store, store_product_id
"""


def load_offers(conn: psycopg.Connection) -> list[EnrichedOffer]:
    """Load all store_products rows and return them as EnrichedOffer instances.

    Each row is enriched with derived normalization fields computed once at
    construction. This function performs only a single SELECT and writes
    nothing to the database.
    """
    cur = conn.cursor(row_factory=psycopg.rows.dict_row)
    cur.execute(_LOAD_SQL)
    rows = cur.fetchall()
    return [_build_offer(row) for row in rows]


# ---------------------------------------------------------------------------
# Main (import-safe)
# ---------------------------------------------------------------------------

def main() -> None:
    """Quick self-test: load all offers and print a count summary."""
    conn = None
    try:
        conn = get_connection()
        offers = load_offers(conn)
        print(f"Loaded {len(offers)} enriched offers.")
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()
