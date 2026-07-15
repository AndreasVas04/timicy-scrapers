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

import csv
import json
import logging
import os
import random
import re
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
REQUEST_DELAY_MIN = 1.5                      # Minimum seconds between listing API requests
REQUEST_DELAY_MAX = 2.5                      # Maximum seconds — jitter breaks fixed-interval fingerprint
LISTING_BLOCK_COOLDOWN = 60.0                # Seconds to pause after detecting an Akamai block
LISTING_BLOCK_RETRIES = 2                    # Retries of the same page after a block cooldown
LISTING_BLOCK_CIRCUIT = 5                    # Consecutive blocked categories before aborting listing phase
MAX_RETRIES = 3                             # Retry attempts on 429 / 5xx responses
MAX_PAGES_PER_CATEGORY = 50                 # Safety cap per category (50 × 100 = 5000 products)
                                            # protects against a pathological API that never
                                            # reports the record set as complete

# AEM CMS endpoint that serves the full navigation/category tree as JSON.
# Discovered by inspecting the Next.js _app bundle: the site loads this
# on every page to render the mega-menu.
NAV_MENU_URL = "https://new-content.kotsovolos.cy/content/kotsovolos/b2c/cy/home.navMenu.json"

# PDP availability enrichment (phase 2 of availability detection).
# The listing API's OrderableFlagCY attribute reports "false" for many
# products that the product page itself sells as available — the flag
# tracks Greek-warehouse orderability, not what the Cyprus site actually
# offers shoppers.  For listing-level "unavailable" products in mapped
# categories we therefore fetch the Next.js data-route JSON behind the
# product page and read its server-rendered availabilityData block, which
# is the exact availability badge shown on the site.
ENRICHMENT_MAX_FETCHES = 3500       # Hard cap on PDP fetches per run (safety valve)
ENRICHMENT_MIN_INTERVAL = 0.4       # Min seconds between PDP fetches (~2.5 req/s)
ENRICHMENT_403_COOLDOWN = 45.0      # Seconds to pause when Akamai answers 403

# Same mapping file ingest.py loads; used to skip PDP fetches for products
# whose category is unmapped (those rows never survive the ingest step).
CATEGORY_MAPPING_CSV = Path("category_mapping.csv")

# availabilityData.availableStatusKey values that mean the product can be
# bought (immediately, as last pieces, or on back-order).  NOT_AVAILABLE
# and any unknown or missing status map to not-available (fail-safe).
AVAILABLE_STATUS_KEYS = {"IMMEDIATELY_AVAILABLE", "LAST_PIECES", "ON_ORDER"}

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


# ── Listing-phase helpers ─────────────────────────────────────


def _polite_pause() -> None:
    """Sleep for a randomised interval between REQUEST_DELAY_MIN and REQUEST_DELAY_MAX.

    The jitter breaks the fixed-interval request fingerprint that Akamai
    uses for bot detection, and roughly halves the sustained request rate
    compared to the old fixed 1.0 s delay.
    """
    time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))


class ListingBlockedError(Exception):
    """Raised when a listing API response is identified as an Akamai block.

    Akamai bot-protection can redirect listing requests to an HTML error
    page.  Because the httpx client follows redirects, the block arrives
    as HTTP 200 with an HTML body instead of the expected JSON.  This
    exception signals that condition so callers can retry after a cooldown.
    """


# Simple counters tracking Akamai block events during the listing phase.
# Logged at the end of fetch_all_products for nightly observability.
listing_block_stats: dict[str, int] = {
    "blocks_detected": 0,
    "retries_recovered": 0,
    "categories_skipped": 0,
    "listing_requests_total": 0,
}


def _fetch_listing_page(
    client: httpx.Client,
    params: dict[str, str],
) -> dict[str, Any]:
    """Fetch one page from the product-listing API, guarding against Akamai blocks.

    Increments listing_requests_total on every call (counts page-fetch
    attempts).  Raises ListingBlockedError when the response looks like
    an Akamai bot-protection redirect rather than valid API JSON.

    Why status-code checking alone is insufficient: the httpx client is
    created with follow_redirects=True, so Akamai's 302 redirect to an
    HTML error page is silently followed and arrives here as HTTP 200
    with an HTML body.  We therefore inspect the redirect history, the
    final URL, and the JSON-parseability of the body to detect blocks.
    """
    listing_block_stats["listing_requests_total"] += 1
    resp = _request_with_retry(client, PRODUCTS_ENDPOINT, params)

    # Block detection (a): the response chain contains a redirect whose
    # final URL points to an Akamai error page.  This catches the exact
    # 302 → assets.kotsovolos.gr/vp/error-pages/… pattern seen in prod.
    if resp.history:
        final_host = resp.url.host
        final_path = str(resp.url.path)
        if final_host == "assets.kotsovolos.gr" or "/vp/error-pages/" in final_path:
            listing_block_stats["blocks_detected"] += 1
            raise ListingBlockedError(
                f"Redirect to Akamai block page: {resp.url}"
            )

    # Block detection (b) + (c): the body is not valid JSON, or parses
    # to something other than a dict (e.g. an HTML error page that
    # slipped through without a redirect, or a JSON array).
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        listing_block_stats["blocks_detected"] += 1
        raise ListingBlockedError(
            f"Response is not valid JSON (likely HTML block page): {exc}"
        ) from exc

    if not isinstance(data, dict):
        listing_block_stats["blocks_detected"] += 1
        raise ListingBlockedError(
            f"Parsed JSON is {type(data).__name__}, expected dict — probable block page"
        )

    return data


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
) -> tuple[list[dict[str, Any]], bool]:
    """Paginate through all products in a single category.

    The Kotsovolos API uses an unusual pagination style: the page number
    and page size are passed inside a single 'params' query parameter
    as a URL-encoded query string (e.g. "pageNumber=2&pageSize=100").
    This was discovered by inspecting the minified Next.js app bundle.

    Returns a tuple of:
      - list of raw product dicts from the API's catalogEntryView
      - blocked: True if pagination was aborted due to a persistent
        Akamai block (all retries for a page exhausted).  Partial results
        collected before the block are still returned.
    """
    all_products: list[dict[str, Any]] = []
    page = 1
    total = 0  # will be set from the first successful API response

    while True:
        # Build the pagination string that goes inside the 'params' query param.
        # Page 1 doesn't strictly need it, but including it keeps behaviour consistent.
        params: dict[str, str] = {
            "catId": category_id,
            "storeId": STORE_ID,
            "params": f"pageNumber={page}&pageSize={PAGE_SIZE}",
        }

        # Attempt to fetch this page, retrying after cooldown if Akamai blocks.
        data: dict[str, Any] | None = None
        for retry in range(LISTING_BLOCK_RETRIES + 1):
            try:
                data = _fetch_listing_page(client, params)
                break  # success — exit retry loop
            except ListingBlockedError:
                if retry < LISTING_BLOCK_RETRIES:
                    # Recoverable: log the block, wait for cooldown, then retry
                    # the SAME page (not the next one).
                    log.warning(
                        "  [%s] Akamai block on page %d (attempt %d/%d) — "
                        "cooling down %.0fs before retry.",
                        category_title, page, retry + 1,
                        LISTING_BLOCK_RETRIES + 1, LISTING_BLOCK_COOLDOWN,
                    )
                    time.sleep(LISTING_BLOCK_COOLDOWN)
                else:
                    # All retries exhausted — persistent block.  Return
                    # whatever we collected so far (partial results).
                    log.warning(
                        "  [%s] Persistent Akamai block on page %d after %d retries — "
                        "returning %d partial products.",
                        category_title, page, LISTING_BLOCK_RETRIES,
                        len(all_products),
                    )
                    return all_products, True  # blocked=True

        # If a retry succeeded, record the recovery
        if retry > 0 and data is not None:
            listing_block_stats["retries_recovered"] += 1

        products = data.get("catalogEntryView", [])
        total = data.get("recordSetTotal", 0)
        # The API returns recordSetComplete as a JSON STRING ("true" /
        # "false"), not a boolean.  A plain truthiness check would treat
        # the non-empty string "false" as complete and stop after page 1,
        # so the flag must be parsed with an explicit string comparison.
        complete = str(data.get("recordSetComplete", "true")).lower() == "true"

        if not products:
            # Empty response — either the category has no products,
            # or we've paginated past the last page.
            if page == 1:
                log.info("  [%s] No products found.", category_title)
            break

        all_products.extend(products)

        if page == 1:
            log.info("  [%s] %d total products, fetching…", category_title, total)

        # Check if we've collected all products.  The count-based guard
        # is a backstop in case the API misreports the complete flag.
        if complete or len(all_products) >= total:
            break

        # Safety cap: never fetch more than MAX_PAGES_PER_CATEGORY pages
        # for one category, in case the API keeps reporting an incomplete
        # record set forever (pathological pagination loop).
        if page >= MAX_PAGES_PER_CATEGORY:
            log.warning(
                "  [%s] Page cap (%d) reached with %d/%d products — stopping.",
                category_title, MAX_PAGES_PER_CATEGORY, len(all_products), total,
            )
            break

        page += 1
        _polite_pause()  # Randomised delay between pages to avoid bot fingerprinting

    # Permanent truncation observability: if the category yielded fewer
    # products than the API's own total, say so loudly in the nightly
    # logs so a pagination regression cannot go unnoticed again.
    if len(all_products) < total:
        log.warning(
            "  [%s] Truncated: fetched %d of %d products.",
            category_title, len(all_products), total,
        )

    return all_products, False  # blocked=False — category completed normally


def fetch_all_products(client: httpx.Client, categories: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Fetch products from all leaf categories, de-duplicating by partNumber.

    Products can appear in multiple categories (e.g. an iPad might be in
    both "Tablets - iPad" and "Apple Products").  We track seen partNumbers
    and skip duplicates to avoid inflating the dataset.

    Implements a circuit breaker: if LISTING_BLOCK_CIRCUIT consecutive
    categories end in a persistent Akamai block, the listing loop aborts
    early.  A mass block should fail the night quickly via the downstream
    row-count validation floor, not burn hours in cooldowns.

    Returns a list of (product_dict, category_title) tuples.
    """
    seen_part_numbers: set[str] = set()
    all_products: list[tuple[dict[str, Any], str]] = []
    empty_categories: list[str] = []
    consecutive_blocks = 0  # consecutive categories ending in persistent block

    for i, cat in enumerate(categories, 1):
        log.info("Category %d/%d: %s (id=%s)", i, len(categories), cat["title"], cat["id"])

        try:
            products, blocked = fetch_products_for_category(client, cat["id"], cat["title"])
        except httpx.HTTPStatusError as e:
            log.warning("  Skipping [%s]: HTTP %s", cat["title"], e.response.status_code)
            continue

        # Track consecutive persistent blocks for the circuit breaker.
        # A category that completes without a persistent block resets the
        # counter; a blocked category increments it.
        if blocked:
            listing_block_stats["categories_skipped"] += 1
            consecutive_blocks += 1
        else:
            consecutive_blocks = 0

        if not products:
            empty_categories.append(cat["title"])
            # Check circuit breaker even if the category returned no products
            if consecutive_blocks >= LISTING_BLOCK_CIRCUIT:
                log.error(
                    "Circuit breaker tripped: %d consecutive categories blocked by Akamai — "
                    "aborting listing phase with %d products collected so far.",
                    consecutive_blocks, len(all_products),
                )
                break
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

        # Circuit breaker: if too many consecutive categories are blocked,
        # stop the listing loop to avoid burning hours in cooldowns.
        if consecutive_blocks >= LISTING_BLOCK_CIRCUIT:
            log.error(
                "Circuit breaker tripped: %d consecutive categories blocked by Akamai — "
                "aborting listing phase with %d products collected so far.",
                consecutive_blocks, len(all_products),
            )
            break

        _polite_pause()  # Randomised delay between categories to avoid bot fingerprinting

    if empty_categories:
        log.info("Categories with zero products (%d): %s", len(empty_categories), ", ".join(empty_categories[:20]))
        if len(empty_categories) > 20:
            log.info("  … and %d more", len(empty_categories) - 20)

    # Observability: always log block stats so nightly logs show the
    # listing phase's Akamai interaction even when all counters are zero.
    log.info(
        "Kotsovolos listing block stats: blocks_detected=%d, retries_recovered=%d, "
        "categories_skipped=%d, listing_requests_total=%d",
        listing_block_stats["blocks_detected"],
        listing_block_stats["retries_recovered"],
        listing_block_stats["categories_skipped"],
        listing_block_stats["listing_requests_total"],
    )

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


def _parse_positive_decimal(value: Any) -> Decimal | None:
    """Parse a raw price value, accepting it only if it is a number > 0.

    The API mixes floats, numeric strings and missing keys across its
    price fields, and stale entries are often 0.0.  Anything that is not
    a strictly positive number returns None so the caller can fall
    through to the next price source.
    """
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed > 0 else None


def _extract_offer_value(price_list: Any) -> Decimal | None:
    """Return the 'Offer' entry's value from a Kotsovolos price list.

    Price data comes as a list of usage-tagged dicts, e.g.
    [{"usage": "Display", "value": "1299.0"}, {"usage": "Offer", "value": "1269.0"}].
    'Display' is the crossed-out list price; 'Offer' is what the site
    actually charges.  Returns the Offer value only when it is present,
    parseable and greater than zero — otherwise None so the caller can
    fall through to the next source.
    """
    if not isinstance(price_list, list):
        return None
    for entry in price_list:
        if isinstance(entry, dict) and entry.get("usage") == "Offer":
            return _parse_positive_decimal(entry.get("value"))
    return None


def _select_price(product: dict[str, Any]) -> tuple[Decimal | None, str]:
    """Select the selling price for a product, preferring 'Offer' entries.

    The listing API exposes three price sources that can disagree:
      1. product["price"]            — product-level Display/Offer list
      2. product["sKUs"][0]["price"] — SKU-level Display/Offer list
      3. product["price_EUR"]        — flat field, stale for a large
                                       slice of the catalog (sometimes 0.0)
    The website renders the Offer value, so it is preferred (product
    level first, then the first SKU) and price_EUR is used only when
    neither Offer entry is usable.  Returns (price, source_tag) where
    source_tag is one of "offer_product", "offer_sku",
    "price_eur_fallback" or "none" for observability counting.
    """
    price = _extract_offer_value(product.get("price"))
    if price is not None:
        return price, "offer_product"

    # Some products (e.g. ones whose product-level Offer entry has no
    # "value" key) only carry a usable Offer price on their first SKU.
    skus = product.get("sKUs")
    if isinstance(skus, list) and skus and isinstance(skus[0], dict):
        price = _extract_offer_value(skus[0].get("price"))
        if price is not None:
            return price, "offer_sku"

    # Last resort: the flat field the scraper historically used.
    price = _parse_positive_decimal(product.get("price_EUR"))
    if price is not None:
        return price, "price_eur_fallback"

    return None, "none"


def normalize(
    products_with_categories: list[tuple[dict[str, Any], str]],
) -> tuple[list[VariantRow], set[str]]:
    """Convert raw API product dicts into normalised VariantRow objects.

    Each Kotsovolos product maps to exactly one row (unlike Shopify stores
    where a product may have multiple variants).  Variants in Kotsovolos
    (e.g. different colours/storage) are separate products with distinct
    partNumbers.

    Returns (rows, enrichment_candidates): the normalised rows plus the
    set of partNumbers whose OrderableFlagCY attribute was "false".
    Those rows carry the phase-1 value available=False and are the
    candidates for the phase-2 PDP re-check in enrich_availability().
    """
    rows: list[VariantRow] = []
    # Distribution of which price source produced each row's price,
    # logged once per run for observability.
    price_sources: Counter[str] = Counter()
    # partNumbers flagged OrderableFlagCY="false" — phase-2 candidates.
    enrichment_candidates: set[str] = set()

    for product, category_title in products_with_categories:
        part_number = product.get("partNumber", "")
        name = product.get("name", "")
        manufacturer = product.get("manufacturer") or None

        # Price: prefer the rendered 'Offer' entry (product level, then
        # first SKU) over the flat price_EUR field, which is stale for a
        # large slice of the catalog.  _select_price documents the exact
        # fallback order and returns a source tag for observability.
        price, price_source = _select_price(product)
        price_sources[price_source] += 1

        # Availability phase 1 (listing-level): a product is "available"
        # only if it's marked as buyable AND the Cyprus-specific
        # OrderableFlagCY attribute is not explicitly "false".  Many
        # products have buyable=true but OrderableFlagCY=false — the flag
        # tracks Greek-warehouse orderability and often disagrees with
        # what the Cyprus product page actually sells.
        buyable = product.get("buyable", "false") == "true"
        orderable_cy = _get_attribute(product, "OrderableFlagCY")
        # If the flag is present and explicitly "false", keep the
        # conservative phase-1 value (not available) but remember the
        # partNumber: phase 2 re-checks these against the product page
        # and flips the ones the site really sells.
        if orderable_cy is not None and orderable_cy.lower() == "false":
            buyable = False
            enrichment_candidates.add(part_number)

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

    # One-line distribution of price sources so nightly logs show how
    # often each fallback level was used (a sudden shift here signals an
    # API format change).
    log.info(
        "Price field sources: offer_product=%d, offer_sku=%d, price_eur_fallback=%d, none=%d",
        price_sources["offer_product"],
        price_sources["offer_sku"],
        price_sources["price_eur_fallback"],
        price_sources["none"],
    )

    return rows, enrichment_candidates


# ── PDP availability enrichment (phase 2) ─────────────────────


def _load_mapped_product_types() -> set[str]:
    """Return the kotsovolos raw product types that ingest.py will keep.

    Replicates the loading rule of ingest.py's category-mapping step:
    rows in category_mapping.csv whose canonical_category is blank are
    dropped during ingest, so spending PDP fetches on their products
    would be wasted budget.  Only mapped kotsovolos product types are
    returned.

    A missing CSV yields an empty set, which simply disables enrichment
    for the run (fail-safe: rows keep their phase-1 availability).
    """
    if not CATEGORY_MAPPING_CSV.exists():
        log.warning(
            "%s not found — skipping availability enrichment.", CATEGORY_MAPPING_CSV
        )
        return set()

    mapped: set[str] = set()
    with open(CATEGORY_MAPPING_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            store = (row.get("store") or "").strip()
            raw_pt = (row.get("raw_product_type") or "").strip()
            canon = (row.get("canonical_category") or "").strip()
            # Rows with a blank canonical_category are intentionally
            # excluded, exactly as ingest.py excludes them.
            if store == "kotsovolos" and raw_pt and canon:
                mapped.add(raw_pt)
    return mapped


class _MinIntervalLimiter:
    """Wall-clock pacer enforcing a minimum interval between requests.

    Synchronous counterpart of public_scraper.py's asyncio RateLimiter:
    each wait() call sleeps until its time slot opens, then advances the
    slot by the configured interval, so PDP fetches never exceed the
    target rate no matter how fast responses come back.
    """

    def __init__(self, min_interval: float) -> None:
        self._interval = min_interval   # minimum seconds between request starts
        self._next_allowed = 0.0        # monotonic timestamp of the next open slot

    def wait(self) -> None:
        """Sleep until the next slot opens, then claim it."""
        now = time.monotonic()
        if now < self._next_allowed:
            time.sleep(self._next_allowed - now)
            now = time.monotonic()
        self._next_allowed = max(now, self._next_allowed) + self._interval


class AvailabilityEnricher:
    """Fetches per-product availability from the Next.js data route.

    The product page is server-side rendered and its pageProps carry an
    availabilityData block holding the availability badge shown to
    shoppers.  The same JSON is served without the surrounding HTML from
        {BASE_URL}/_next/data/{buildId}/{slug}.json
    where buildId identifies the current frontend deployment and slug is
    the product URL path.  This class:
      * discovers buildId once from the homepage's __NEXT_DATA__ script,
      * fetches the data-route JSON for each requested product,
      * refreshes buildId once per run if the route starts returning 404
        (which happens when the site deploys mid-run),
      * falls back to parsing the full PDP HTML when the refreshed route
        still 404s for a product,
      * treats every failure as "not available" (fail-safe: a network
        problem must never mark an unavailable product as in stock).

    Requests go through the same httpx.Client as the listing scrape, so
    they carry the identical browser-header fingerprint and connection
    settings that already pass Akamai.
    """

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self._limiter = _MinIntervalLimiter(ENRICHMENT_MIN_INTERVAL)
        self._build_id: str | None = None     # current Next.js deployment id
        self._build_id_refreshed = False      # buildId re-fetch allowed once per run
        self.status_counter: Counter[str] = Counter()  # availableStatusKey distribution
        self.cta_counter: Counter[str] = Counter()     # ctaNameTextKey distribution
        self.fetch_count = 0                  # PDP HTTP requests made (cap enforcement)

    # -- buildId discovery -------------------------------------------

    @staticmethod
    def _extract_next_data(html: str) -> dict[str, Any] | None:
        """Parse the __NEXT_DATA__ JSON blob embedded in a Next.js page.

        Locates the <script id="__NEXT_DATA__"> tag and scans forward
        with a string-aware balanced-brace counter to find the end of the
        JSON object; the blob contains nested braces and quoted strings,
        so a naive regex or find("</script>") is not reliable.
        """
        marker = html.find('id="__NEXT_DATA__"')
        if marker == -1:
            return None
        start = html.find("{", marker)
        if start == -1:
            return None
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(html)):
            ch = html[i]
            if in_string:
                # Inside a JSON string: only an unescaped quote ends it.
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    # Found the matching closing brace of the blob.
                    try:
                        return json.loads(html[start : i + 1])
                    except json.JSONDecodeError:
                        return None
        return None

    def _fetch_build_id(self) -> str | None:
        """Fetch the homepage and extract the current Next.js buildId.

        Tries the structured __NEXT_DATA__ parse first, then falls back
        to a plain regex on the "buildId" key in case the balanced-brace
        scan fails (e.g. unexpected markup changes).  Returns None on any
        failure — the caller decides whether to skip enrichment.
        """
        try:
            self._limiter.wait()
            resp = self._client.get(BASE_URL)
        except httpx.HTTPError as exc:
            log.warning("Homepage fetch for buildId failed: %s", exc)
            return None
        if resp.status_code != 200:
            log.warning("Homepage fetch for buildId returned HTTP %d.", resp.status_code)
            return None

        html = resp.text
        data = self._extract_next_data(html)
        if isinstance(data, dict):
            build_id = data.get("buildId")
            if isinstance(build_id, str) and build_id:
                return build_id

        # Regex fallback: the buildId also appears as a plain JSON key
        # inside the same script tag.
        m = re.search(r'"buildId"\s*:\s*"([^"]+)"', html)
        if m:
            return m.group(1)

        log.warning("Could not extract buildId from homepage HTML.")
        return None

    def ensure_build_id(self) -> bool:
        """Discover the buildId if not yet known.

        Returns False when discovery fails, in which case the caller must
        skip enrichment for this run (the data route cannot be built).
        """
        if self._build_id is None:
            self._build_id = self._fetch_build_id()
        return self._build_id is not None

    # -- per-product fetch -------------------------------------------

    def _data_route_url(self, slug: str) -> str:
        """Build the Next.js data-route URL for a product page slug."""
        return f"{BASE_URL}/_next/data/{self._build_id}/{slug}.json"

    def _get_pdp(self, url: str) -> httpx.Response:
        """Rate-limited GET with the Next.js data-route header additions.

        Sends the client's default browser headers plus the two headers
        the site's own frontend uses for data-route XHRs.  Every call
        counts against the per-run fetch cap.
        """
        self._limiter.wait()
        self.fetch_count += 1
        return self._client.get(
            url,
            headers={"Accept": "application/json", "x-nextjs-data": "1"},
        )

    def _get_pdp_with_403_cooldown(self, url: str) -> httpx.Response | None:
        """GET a PDP URL, pausing once on an Akamai 403 before retrying.

        Akamai signals rate exhaustion with 403 rather than 429; a fixed
        cooldown lets its token bucket refill.  A second consecutive 403
        is returned to the caller, which records the product as failed.
        Network-level errors return None (also recorded as failed).
        """
        try:
            resp = self._get_pdp(url)
            if resp.status_code == 403:
                log.warning(
                    "HTTP 403 from %s — cooling down for %.0fs.",
                    url, ENRICHMENT_403_COOLDOWN,
                )
                time.sleep(ENRICHMENT_403_COOLDOWN)
                resp = self._get_pdp(url)
            return resp
        except httpx.HTTPError as exc:
            log.warning("PDP fetch error for %s: %s", url, exc)
            return None

    def _classify(self, availability: Any) -> bool:
        """Map an availabilityData block to a boolean and count keys.

        IMMEDIATELY_AVAILABLE / LAST_PIECES / ON_ORDER → purchasable.
        NOT_AVAILABLE, unknown keys and missing data → not purchasable
        (fail-safe).  Unknown keys are counted verbatim so brand-new
        statuses show up in the nightly logs instead of silently mapping
        to False forever.
        """
        if not isinstance(availability, dict):
            self.status_counter["<missing availabilityData>"] += 1
            self.cta_counter["<missing availabilityData>"] += 1
            return False
        status_key = availability.get("availableStatusKey")
        cta_key = availability.get("ctaNameTextKey")
        self.status_counter[str(status_key)] += 1
        self.cta_counter[str(cta_key)] += 1
        return status_key in AVAILABLE_STATUS_KEYS

    def check_available(self, product_url: str) -> tuple[bool, bool]:
        """Return (available, fetch_succeeded) for one product page.

        Fetch order:
          1. /_next/data/{buildId}/{slug}.json — small JSON payload.
          2. On 404: refresh the buildId once per run (a data-route 404
             usually means the frontend deployed mid-run) and retry.
          3. On persistent 404: fall back to the full PDP HTML and parse
             its embedded __NEXT_DATA__ blob, which carries the same
             pageProps.
        Any unrecoverable failure returns (False, False): the row keeps
        its phase-1 "not available" value and the failure is counted by
        the caller.
        """
        # The slug is the product URL path without the leading slash,
        # e.g. "mobile-phones-gps/smartphones/306232-smartphone-...".
        slug = urlparse(product_url).path.lstrip("/")

        resp = self._get_pdp_with_403_cooldown(self._data_route_url(slug))

        if resp is not None and resp.status_code == 404 and not self._build_id_refreshed:
            # First 404 of the run: assume the deployment changed and the
            # old buildId went stale.  Refresh it once and retry.
            self._build_id_refreshed = True
            new_build_id = self._fetch_build_id()
            if new_build_id:
                log.info("Data route 404 — refreshed buildId and retrying.")
                self._build_id = new_build_id
                resp = self._get_pdp_with_403_cooldown(self._data_route_url(slug))

        if resp is not None and resp.status_code == 200:
            try:
                payload = resp.json()
            except (json.JSONDecodeError, ValueError):
                payload = None
            if isinstance(payload, dict):
                page_props = payload.get("pageProps")
                if isinstance(page_props, dict):
                    return self._classify(page_props.get("availabilityData")), True
            # Non-JSON or unexpectedly-shaped 200 body: fail-safe below.

        if resp is not None and resp.status_code == 404:
            # The data route still 404s after the buildId refresh — fetch
            # the full product page and read the same data from its
            # embedded __NEXT_DATA__ script.
            resp = self._get_pdp_with_403_cooldown(product_url)
            if resp is not None and resp.status_code == 200:
                data = self._extract_next_data(resp.text)
                if isinstance(data, dict):
                    props = data.get("props")
                    if isinstance(props, dict):
                        page_props = props.get("pageProps")
                        if isinstance(page_props, dict):
                            return self._classify(page_props.get("availabilityData")), True

        # Every remaining path (network error, HTTP error status, broken
        # payload) lands here: report a failed fetch, keep available=False.
        return False, False


def enrich_availability(
    client: httpx.Client,
    rows: list[VariantRow],
    candidate_ids: set[str],
) -> None:
    """Phase 2 of availability detection: PDP re-check for flagged rows.

    The listing API marks many Cyprus-sellable products with
    OrderableFlagCY="false" even though the product page sells them (the
    flag tracks Greek-warehouse orderability).  For every phase-1
    candidate whose category is mapped in category_mapping.csv (i.e.
    whose rows survive ingest), fetch the product page's server-rendered
    availabilityData and flip the row to available=True when the page
    shows a purchasable badge.

    Rows are mutated in place.  All failure modes leave available=False.
    """
    candidates = [r for r in rows if r.store_product_id in candidate_ids]

    # Products in unmapped categories never reach the matching pipeline,
    # so fetching their PDPs would waste the request budget.  They keep
    # their conservative phase-1 value without a fetch.
    mapped_types = _load_mapped_product_types()
    to_fetch = [r for r in candidates if r.product_type in mapped_types]
    skipped_unmapped = len(candidates) - len(to_fetch)

    enricher = AvailabilityEnricher(client)
    fetched = flipped_true = confirmed_false = failed = overflow = 0

    if to_fetch and not enricher.ensure_build_id():
        # Without a buildId the data route cannot be constructed.  Skip
        # enrichment entirely for this run rather than crashing the
        # scrape; every candidate keeps its phase-1 availability.
        log.warning("buildId discovery failed — skipping availability enrichment this run.")
        to_fetch = []

    for row in to_fetch:
        if enricher.fetch_count >= ENRICHMENT_MAX_FETCHES:
            # Cap reached: remaining candidates stay at their phase-1
            # value (False) and are counted below.
            overflow += 1
            continue
        fetched += 1
        available, ok = enricher.check_available(row.product_url)
        if not ok:
            failed += 1
        elif available:
            row.available = True
            flipped_true += 1
        else:
            confirmed_false += 1

    if overflow:
        log.warning(
            "Enrichment fetch cap (%d) reached — %d candidate(s) left at phase-1 value.",
            ENRICHMENT_MAX_FETCHES, overflow,
        )

    # Observability block: always printed so nightly logs expose the
    # availability pipeline's behaviour even on runs where nothing
    # changed.  Distributions are plain dicts of verbatim keys.
    log.info(
        "Kotsovolos availability status distribution: %s", dict(enricher.status_counter)
    )
    log.info("Kotsovolos cta distribution: %s", dict(enricher.cta_counter))
    log.info(
        "Kotsovolos enrichment: candidates=%d, skipped_unmapped=%d, fetched=%d, "
        "flipped_true=%d, confirmed_false=%d, failed=%d",
        len(candidates), skipped_unmapped, fetched, flipped_true, confirmed_false, failed,
    )


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

        # 3. Normalize into VariantRow objects (phase-1 availability from
        #    the listing API; also collects the phase-2 candidate set)
        rows, candidate_ids = normalize(products_with_cats)
        log.info("Total rows after normalization: %d", len(rows))

        # 4. Phase-2 availability: PDP re-check for listing-level
        #    "unavailable" products in mapped categories.  Runs inside the
        #    client block so the requests reuse the same connection pool
        #    and browser-header fingerprint as the listing scrape.
        enrich_availability(client, rows, candidate_ids)

    # 5. Log MPN/EAN extraction stats for sanity-checking
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

    # 6. Dump all rows to a local JSON file (useful for debugging / offline analysis)
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

    # 7. Upsert to Supabase (no-op if credentials aren't configured)
    upsert_to_supabase(rows)

    # 8. Print sample rows for quick visual verification
    log.info("=== Sample rows ===")
    for r in rows[:5]:
        log.info(
            "  %s | €%s | mpn=%s | mpn_root=%s | avail=%s",
            r.title[:60], r.price, r.mpn, r.mpn_root, r.available,
        )

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
