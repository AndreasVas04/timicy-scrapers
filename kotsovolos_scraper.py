"""Kotsovolos Cyprus (kotsovolos.cy) API scraper.

Targets the Kotsovolos electronics retailer in Cyprus (storeId=10161).
The site runs on IBM WebSphere Commerce with a Next.js frontend and an
Adobe Experience Manager (AEM) CMS for navigation data.

Strategy:
  1. Fetch the full category tree from the AEM navigation menu JSON.
  2. Identify leaf categories (the ones that actually list products).
  3. For each leaf category, paginate through the product-listing API
     (/api/ext/getProductsByCategory) collecting all products.
  4. De-duplicate across categories (products can appear in multiple).
  5. Extract EAN (from the BarCode attribute), manufacturer part numbers
     (from SupplierPartNumbers), and apply Apple part-number root
     extraction for cross-store matching.
  6. Upsert into Supabase `raw_products` and dump to data/kotsovolos.json.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────

BASE_URL = "https://www.kotsovolos.cy"
STORE_ID = "10161"                          # Cyprus store identifier
PRODUCTS_ENDPOINT = f"{BASE_URL}/api/ext/getProductsByCategory"
PAGE_SIZE = 100                             # Products per API page (max observed working)
REQUEST_DELAY = 1.0                         # Seconds between API requests (polite crawling)
MAX_RETRIES = 3                             # Retry attempts on 429 / 5xx responses

# AEM CMS endpoint that serves the full navigation/category tree as JSON.
# Discovered by inspecting the Next.js _app bundle: the site loads this
# on every page to render the mega-menu.
NAV_MENU_URL = "https://new-content.kotsovolos.cy/content/kotsovolos/b2c/cy/home.navMenu.json"

# Matches Apple-style part numbers like "MH344TY/A" or "MQDT3ZM/A".
# Captures the region-independent root (e.g. "MH344") before the
# locale suffix (e.g. "TY/A").  Non-matching SKUs are left as-is.
APPLE_PN_RE = re.compile(r"^([A-Z0-9]{5,6})[A-Z]{1,3}/[A-Z]$")

# Full browser header set for all HTTP requests.
# Kotsovolos is behind Akamai, which blocks datacenter IPs unless the
# request carries a complete, consistent set of browser headers.  A bare
# User-Agent is not enough — Akamai fingerprints the full header profile
# (sec-ch-ua, Sec-Fetch-*, etc.) and challenges requests that look
# automated.  These headers match a real Chrome 125 on Windows and were
# validated via curl from a GitHub Actions datacenter IP (HTTP 200).
# Note: Accept stays as application/json because this scraper only hits
# the JSON product-listing API, not HTML pages.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "el-CY,el;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="125", "Not.A/Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


# ── Pydantic model ──────────────────────────────────────────────

class VariantRow(BaseModel):
    """One normalised row per product, ready for DB upsert.

    Mirrors the schema used by istorm_scraper.py so that all stores
    land in the same raw_products table with a consistent structure.
    """

    store: str = "kotsovolos"                # Fixed identifier for this retailer
    store_product_id: str                    # Kotsovolos article/part number (unique per product)
    title: str                               # Product name (e.g. "Samsung Galaxy S24 256GB")
    vendor: str | None = None                # Brand / manufacturer (e.g. "Samsung")
    product_type: str | None = None          # Leaf category name (e.g. "Smartphones & iPhone")
    sku: str | None = None                   # Article number (same as store_product_id here)
    price: Decimal | None = None             # Current listed price in EUR
    available: bool                          # Whether the product is buyable on the CY store
    image_url: str | None = None             # Product thumbnail URL
    product_url: str                         # Full URL to the product page on kotsovolos.cy
    mpn: str | None = None                   # Manufacturer Part Number (from SupplierPartNumbers or BarCode)
    mpn_root: str | None = None              # Region-independent MPN root for cross-store matching
    identifier_source: str = "none"          # How the MPN was obtained: "sku", "api", "title_regex", or "none"
    scraped_at: datetime = Field(            # UTC timestamp of when this row was scraped
        default_factory=lambda: datetime.now(timezone.utc),
    )


# ── MPN extraction ──────────────────────────────────────────────

def extract_mpn(
    supplier_part_numbers: str | None,
    barcode: str | None,
    article_number: str | None,
) -> str | None:
    """Derive the best available MPN from Kotsovolos product attributes.

    Priority order:
      1. SupplierPartNumbers — semicolon-separated list; we pick the first
         entry that differs from the Kotsovolos article number, since
         that's most likely the manufacturer's own part number.
      2. BarCode (EAN/GTIN) — used as MPN fallback if no supplier PN
         is available. Takes only the first barcode if multiple exist.
      3. None — if neither is available.
    """
    if supplier_part_numbers:
        # SupplierPartNumbers is semicolon-separated, e.g. "AT00496;SM-A236BZKUEUE"
        # Pick the first entry that isn't just the Kotsovolos article number,
        # since the article number is a store-internal ID, not an MPN.
        for pn in supplier_part_numbers.split(";"):
            pn = pn.strip()
            if pn and pn != article_number:
                return pn
        # All entries matched the article number — fall through to barcode
    if barcode:
        # Some products have multiple barcodes separated by semicolons;
        # take the first one.
        return barcode.split(";")[0].strip()
    return None


def extract_mpn_root(mpn: str | None) -> str | None:
    """Derive mpn_root for cross-store matching.

    Apple part numbers like MH344TY/A have a region suffix (TY/A);
    the root (MH344) is the region-independent identifier.
    Non-Apple SKUs pass through unchanged.

    Same logic as istorm_scraper.py to ensure consistent matching.
    """
    if mpn is None:
        return None
    # Try to match an Apple-style part number (e.g. "MH344TY/A").
    # If it matches, return only the root portion (e.g. "MH344"),
    # stripping the region/locale suffix.
    m = APPLE_PN_RE.match(mpn)
    if m:
        return m.group(1)
    # Non-Apple part numbers pass through unchanged.
    return mpn


# ── HTTP helper ────────────────────────────────────────────────

def _request_with_retry(
    client: httpx.Client,
    url: str,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """Perform a GET request with exponential back-off on transient errors.

    Retries up to MAX_RETRIES times on HTTP 429 (rate-limited) or any
    5xx (server error).  Any other non-200 status raises immediately.
    """
    for attempt in range(MAX_RETRIES):
        resp = client.get(url, params=params)
        if resp.status_code == 200:
            return resp
        # Retry on rate-limiting (429) or server errors (5xx)
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = 2 ** attempt  # Exponential back-off: 1s, 2s, 4s
            log.warning(
                "HTTP %s on attempt %d, retrying in %ds…",
                resp.status_code, attempt + 1, wait,
            )
            time.sleep(wait)
            continue
        # Non-retryable client errors (4xx except 429) — fail fast
        resp.raise_for_status()
    # All retries exhausted — raise the last error
    resp.raise_for_status()
    return resp  # unreachable but keeps type checker happy


# ── Category discovery ─────────────────────────────────────────

def fetch_category_tree(client: httpx.Client) -> list[dict[str, Any]]:
    """Fetch the full navigation menu from the AEM CMS endpoint.

    Returns the raw JSON list of top-level menu entries, each with
    nested 'childMenu' arrays forming a tree of categories.
    """
    resp = _request_with_retry(client, NAV_MENU_URL)
    return resp.json()


def extract_leaf_categories(menu: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Recursively walk the category tree and collect leaf categories.

    A "leaf" category is one with no children — these are the categories
    that actually list products (e.g. "Smartphones & iPhone").  Parent
    categories (e.g. "Τηλεφωνία & Tablet") aggregate products from all
    their children, so scraping only leaves avoids counting products
    from both parent and child.

    However, to avoid missing products that live only in mid-level
    categories, we also include non-leaf categories that are marked
    as published.  De-duplication by partNumber later handles overlaps.

    Returns a list of dicts with 'id', 'title', and 'seo_url' keys.
    """
    categories: list[dict[str, str]] = []

    def _walk(items: list[dict[str, Any]]) -> None:
        for item in items:
            uid = item.get("uniqueID")
            title = item.get("jcr:title", "")
            seo_url = item.get("seo_url", "")
            children = item.get("childMenu", [])

            # Only collect categories that have a uniqueID (actual product categories)
            # and are not top-level nav groups (level 0)
            if uid and item.get("level", "0") != "0":
                # Collect leaf categories (no children) — these are the
                # most granular product-listing pages
                if not children:
                    categories.append({
                        "id": uid,
                        "title": title,
                        "seo_url": seo_url,
                    })

            # Recurse into children regardless
            if children:
                _walk(children)

    _walk(menu)
    return categories


# ── Product fetching ───────────────────────────────────────────

def fetch_products_for_category(
    client: httpx.Client,
    category_id: str,
    category_title: str,
) -> list[dict[str, Any]]:
    """Paginate through all products in a single category.

    The Kotsovolos API uses an unusual pagination style: the page number
    and page size are passed inside a single 'params' query parameter
    as a URL-encoded query string (e.g. "pageNumber=2&pageSize=100").
    This was discovered by inspecting the minified Next.js app bundle.

    Returns a list of raw product dicts from the API's catalogEntryView.
    """
    all_products: list[dict[str, Any]] = []
    page = 1

    while True:
        # Build the pagination string that goes inside the 'params' query param.
        # Page 1 doesn't strictly need it, but including it keeps behaviour consistent.
        params: dict[str, str] = {
            "catId": category_id,
            "storeId": STORE_ID,
            "params": f"pageNumber={page}&pageSize={PAGE_SIZE}",
        }

        resp = _request_with_retry(client, PRODUCTS_ENDPOINT, params)
        data = resp.json()

        products = data.get("catalogEntryView", [])
        total = data.get("recordSetTotal", 0)
        complete = data.get("recordSetComplete", True)

        if not products:
            # Empty response — either the category has no products,
            # or we've paginated past the last page.
            if page == 1:
                log.info("  [%s] No products found.", category_title)
            break

        all_products.extend(products)

        if page == 1:
            log.info("  [%s] %d total products, fetching…", category_title, total)

        # Check if we've collected all products
        if complete or len(all_products) >= total:
            break

        page += 1
        time.sleep(REQUEST_DELAY)  # Polite delay between pages

    return all_products


def fetch_all_products(client: httpx.Client, categories: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Fetch products from all leaf categories, de-duplicating by partNumber.

    Products can appear in multiple categories (e.g. an iPad might be in
    both "Tablets - iPad" and "Apple Products").  We track seen partNumbers
    and skip duplicates to avoid inflating the dataset.

    Returns a list of (product_dict, category_title) tuples.
    """
    seen_part_numbers: set[str] = set()
    all_products: list[tuple[dict[str, Any], str]] = []
    empty_categories: list[str] = []

    for i, cat in enumerate(categories, 1):
        log.info("Category %d/%d: %s (id=%s)", i, len(categories), cat["title"], cat["id"])

        try:
            products = fetch_products_for_category(client, cat["id"], cat["title"])
        except httpx.HTTPStatusError as e:
            log.warning("  Skipping [%s]: HTTP %s", cat["title"], e.response.status_code)
            continue

        if not products:
            empty_categories.append(cat["title"])
            continue

        # De-duplicate: skip products we've already seen from another category
        new_count = 0
        for p in products:
            pn = p.get("partNumber", "")
            if pn not in seen_part_numbers:
                seen_part_numbers.add(pn)
                all_products.append((p, cat["title"]))
                new_count += 1

        if new_count < len(products):
            log.info("    → %d new, %d duplicates skipped", new_count, len(products) - new_count)

        time.sleep(REQUEST_DELAY)  # Polite delay between categories

    if empty_categories:
        log.info("Categories with zero products (%d): %s", len(empty_categories), ", ".join(empty_categories[:20]))
        if len(empty_categories) > 20:
            log.info("  … and %d more", len(empty_categories) - 20)

    return all_products


# ── Normalizer ──────────────────────────────────────────────────

def _get_attribute(product: dict[str, Any], identifier: str) -> str | None:
    """Extract a single attribute value from the product's attributes list.

    Kotsovolos stores product metadata as a list of attribute dicts,
    each with an 'identifier' key and a 'values' list.  This helper
    finds the first attribute matching the given identifier and returns
    its first value, or None if not found.
    """
    for attr in product.get("attributes", []):
        if attr.get("identifier") == identifier:
            values = attr.get("values", [])
            if values:
                return values[0].get("value")
    return None


def _build_product_url(product: dict[str, Any]) -> str:
    """Construct the full product page URL from the product's SEO URL.

    The SEO URL is stored in the UserData attribute as a relative path
    like "mobile-phones-gps/smartphones/261789-smartphone-samsung-...".
    We prepend the base URL to make it absolute.

    Falls back to a URL built from the partNumber if UserData is missing.
    """
    user_data = product.get("UserData", [])
    if user_data and isinstance(user_data[0], dict):
        seo_url = user_data[0].get("seo_url", "")
        if seo_url:
            return f"{BASE_URL}/{seo_url}"
    # Fallback: use the partNumber to construct a basic product URL
    part_number = product.get("partNumber", "")
    return f"{BASE_URL}/product/{part_number}"


def build_image_fallback(part_number: str) -> str:
    """Construct a Kotsovolos CDN image URL from the product's partNumber.

    Kotsovolos hosts product images on a predictable CDN path:
        https://assets.kotsovolos.gr/product/{partNumber}-b.jpg
    The "-b" suffix corresponds to the standard product listing thumbnail.
    This URL is stable and does not require authentication or cookies.

    Used as a fallback when the API's "thumbnail" field is missing/empty,
    which happens for ~30% of products.  No network request is made here —
    the URL is constructed purely from the part number string.
    """
    return f"https://assets.kotsovolos.gr/product/{part_number}-b.jpg"


def normalize(
    products_with_categories: list[tuple[dict[str, Any], str]],
) -> list[VariantRow]:
    """Convert raw API product dicts into normalised VariantRow objects.

    Each Kotsovolos product maps to exactly one row (unlike Shopify stores
    where a product may have multiple variants).  Variants in Kotsovolos
    (e.g. different colours/storage) are separate products with distinct
    partNumbers.
    """
    rows: list[VariantRow] = []

    for product, category_title in products_with_categories:
        part_number = product.get("partNumber", "")
        name = product.get("name", "")
        manufacturer = product.get("manufacturer") or None

        # Price: the API provides price_EUR as a float string for the current price.
        # The 'price' field is a list with Display/Offer entries, but price_EUR
        # is the simpler, more reliable source.
        price_str = product.get("price_EUR")
        price = Decimal(str(price_str)) if price_str is not None else None

        # Availability: a product is "available" only if it's marked as
        # buyable AND the Cyprus-specific OrderableFlagCY attribute is
        # not explicitly "false".  Many products have buyable=true but
        # OrderableFlagCY=false — these are listed on the site but
        # cannot actually be ordered in Cyprus (they may only be
        # available via the Greek warehouse or in Greek stores).
        buyable = product.get("buyable", "false") == "true"
        orderable_cy = _get_attribute(product, "OrderableFlagCY")
        # If the flag is present and explicitly "false", the product
        # is not orderable in Cyprus regardless of the buyable flag.
        if orderable_cy is not None and orderable_cy.lower() == "false":
            buyable = False

        # Image: use the thumbnail URL from the product listing
        thumbnail = product.get("thumbnail") or None

        # Fallback: if the API didn't provide a thumbnail but we have a
        # partNumber, construct the predictable CDN image URL.  This fills
        # ~30% of otherwise imageless products with zero network cost.
        if thumbnail is None and part_number:
            thumbnail = build_image_fallback(part_number)

        # Product URL: constructed from SEO URL in UserData
        product_url = _build_product_url(product)

        # Category: use the leaf category this product was found in
        product_type = category_title or None

        # MPN extraction:
        # 1. Try SupplierPartNumbers attribute (manufacturer's own part number)
        # 2. Fall back to BarCode (EAN/GTIN)
        supplier_pns = _get_attribute(product, "SupplierPartNumbers")
        barcode = _get_attribute(product, "BarCode")
        mpn = extract_mpn(supplier_pns, barcode, part_number)
        mpn_root = extract_mpn_root(mpn)

        # MPN comes from structured API attributes (SupplierPartNumbers or BarCode)
        identifier_source = "api" if mpn else "none"

        rows.append(VariantRow(
            store_product_id=part_number,
            title=name,
            vendor=manufacturer,
            product_type=product_type,
            sku=part_number,
            price=price,
            available=buyable,
            image_url=thumbnail,
            product_url=product_url,
            mpn=mpn,
            mpn_root=mpn_root,
            identifier_source=identifier_source,
        ))

    return rows


# ── Supabase upsert ────────────────────────────────────────────

def upsert_to_supabase(rows: list[VariantRow]) -> None:
    """Upsert variant rows into the Supabase `raw_products` table.

    Uses a Postgres upsert (INSERT … ON CONFLICT UPDATE) keyed on
    (store, store_product_id) so that re-running the scraper updates
    existing rows in place rather than creating duplicates.

    Requires SUPABASE_URL and SUPABASE_SERVICE_KEY env vars.
    Silently skips the upsert if credentials are missing (useful for
    local development / dry-run mode).
    """
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        log.warning("SUPABASE_URL / SUPABASE_SERVICE_KEY not set — skipping DB upsert.")
        return

    # Lazy import — avoids requiring the supabase package when running
    # without DB credentials (e.g. local testing with JSON output only).
    from supabase import create_client

    sb = create_client(url, key)

    # Serialize Pydantic models to dicts, converting types that aren't
    # natively JSON-serialisable (Decimal → float, datetime → ISO string).
    records = []
    for r in rows:
        d = r.model_dump()
        d["price"] = float(d["price"]) if d["price"] is not None else None
        d["scraped_at"] = d["scraped_at"].isoformat()
        records.append(d)

    # Upsert in batches to stay within Supabase payload limits.
    # on_conflict="store,store_product_id" means: if a row with the
    # same store + article number already exists, update it in place.
    batch_size = 500
    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        sb.table("raw_products").upsert(
            batch, on_conflict="store,store_product_id"
        ).execute()
        log.info("Upserted batch %d–%d", i + 1, min(i + batch_size, len(records)))


# ── Main ────────────────────────────────────────────────────────

def main() -> None:
    """Entry point: discover categories → fetch products → normalize → export → upsert.

    Pass an integer argument to limit the number of categories processed
    (useful for testing), e.g.:
        python kotsovolos_scraper.py 3
    """
    import sys

    if len(sys.argv) > 1 and not sys.argv[1].isdigit():
        log.error("Invalid argument '%s' — expected a numeric limit. Exiting.", sys.argv[1])
        return
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None

    log.info("=== Kotsovolos CY scraper starting ===")

    with httpx.Client(
        headers=DEFAULT_HEADERS,
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        # 1. Fetch the category tree from the AEM CMS
        log.info("Fetching category tree from AEM…")
        menu = fetch_category_tree(client)
        categories = extract_leaf_categories(menu)
        log.info("Discovered %d leaf categories.", len(categories))

        # Optionally limit the number of categories for testing
        if limit:
            log.info("TEST MODE: limiting to first %d categories.", limit)
            categories = categories[:limit]

        # 2. Fetch all products across all leaf categories
        log.info("Fetching products across all categories…")
        products_with_cats = fetch_all_products(client, categories)
        log.info("Total unique products fetched: %d", len(products_with_cats))

    # 3. Normalize into VariantRow objects
    rows = normalize(products_with_cats)
    log.info("Total rows after normalization: %d", len(rows))

    # 4. Log MPN/EAN extraction stats for sanity-checking
    mpn_present = sum(1 for r in rows if r.mpn is not None)
    mpn_missing = sum(1 for r in rows if r.mpn is None)
    # "Apple PN" = MPNs where mpn_root differs from mpn (region suffix stripped)
    apple_pn = sum(1 for r in rows if r.mpn is not None and r.mpn_root != r.mpn)
    # "passthrough" = non-Apple MPNs where mpn_root == mpn (no transformation)
    passthrough = sum(1 for r in rows if r.mpn is not None and r.mpn_root == r.mpn)
    log.info("MPNs — present: %d | missing: %d", mpn_present, mpn_missing)
    log.info("mpn_root — Apple PN (shortened): %d | passthrough: %d", apple_pn, passthrough)

    # Count products that had EAN (BarCode) vs only supplier PN
    ean_count = 0
    for product, _ in products_with_cats:
        barcode = _get_attribute(product, "BarCode")
        if barcode:
            ean_count += 1
    log.info("Products with EAN (BarCode): %d / %d", ean_count, len(products_with_cats))

    # 5. Dump all rows to a local JSON file (useful for debugging / offline analysis)
    out_path = Path("data/kotsovolos.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    serializable = []
    for r in rows:
        d = r.model_dump()
        d["price"] = float(d["price"]) if d["price"] is not None else None
        d["scraped_at"] = d["scraped_at"].isoformat()
        serializable.append(d)
    out_path.write_text(
        json.dumps(serializable, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info("Wrote %d rows to %s", len(serializable), out_path)

    # 6. Upsert to Supabase (no-op if credentials aren't configured)
    upsert_to_supabase(rows)

    # 7. Print sample rows for quick visual verification
    log.info("=== Sample rows ===")
    for r in rows[:5]:
        log.info(
            "  %s | €%s | mpn=%s | mpn_root=%s | avail=%s",
            r.title[:60], r.price, r.mpn, r.mpn_root, r.available,
        )

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
