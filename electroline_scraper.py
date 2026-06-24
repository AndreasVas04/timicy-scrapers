"""Electroline Cyprus (electroline.cy) product scraper.

Targets Electroline, a large Cyprus-native electronics retailer (~9.5k
products).  The site is a WordPress/WooCommerce store that embeds
Schema.org JSON-LD (via Yoast SEO) in every product page inside an
@graph array.  All data is available in the raw HTML — no JS rendering
is needed, so we use plain async httpx.

Strategy:
  1. Parse the XML sitemap index to discover all product sub-sitemaps.
  2. Fetch each sub-sitemap and collect all product page URLs.
  3. Fetch each product page concurrently (async httpx, semaphore-limited)
     and extract data from the embedded JSON-LD @graph block + OG meta tags.
  4. Extract brand from the first word of the product title (Electroline
     titles consistently start with the brand name in uppercase).
  5. Extract MPN from parenthetical patterns in the product title, e.g.
     "APPLE MTP43QL/A iPhone 15 (MTUX3ZD/A)" → MPN = "MTUX3ZD/A".
     If no parenthetical, try extracting from the second word of the title
     if it looks like a model number (e.g. "BOSCH WGG244ZXGR ...").
  6. Apply Apple part-number root extraction for cross-store matching.
  7. Upsert into Supabase `raw_products` and dump to data/electroline.json.

Data sources per field:
  - title, sku, category, price, availability: JSON-LD Product schema
  - image_url: OG meta tag (full URL) or JSON-LD image (relative path)
  - vendor (brand): first word of title before the model number
  - store_product_id: uses the SKU from JSON-LD (e.g. "IT63725")
  - mpn: parenthetical regex on title, else second-word model number
  - ean: not available from Electroline (always null)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal
from html import unescape
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

BASE_URL = "https://electroline.cy"
SITEMAP_INDEX_URL = f"{BASE_URL}/sitemap_index.xml"

# XML namespace used in sitemap files.
SITEMAP_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# Concurrency and rate-limiting settings.
# Electroline has ~9.5k products; at 5 concurrent with 200ms delay,
# that's roughly 9500 / 5 * 0.2 ≈ 6-10 minutes depending on response time.
INITIAL_CONCURRENCY = 5       # Max concurrent HTTP requests
MIN_CONCURRENCY = 1           # Floor when auto-throttling on 429s
REQUEST_DELAY = 0.2           # Seconds between requests per coroutine
MAX_RETRIES = 3               # Retry attempts on 429 / 5xx responses
PROGRESS_INTERVAL = 500       # Log progress every N products

# Matches Apple-style part numbers like "MH344TY/A" or "MQDT3ZM/A".
# Captures the region-independent root (e.g. "MH344") before the
# locale suffix (e.g. "TY/A").  Non-matching SKUs are left as-is.
APPLE_PN_RE = re.compile(r"^([A-Z0-9]{5,6})[A-Z]{1,3}/[A-Z]$")

# Extracts manufacturer part numbers from parenthetical patterns in
# product titles.  Requirements:
#   - At least 5 characters to filter out noise like "(PD)", "(XXS)"
#   - Must contain at least one letter AND one digit (or a slash/dash)
#   - We take the *last* match in the title, since earlier parentheticals
#     are often descriptive (e.g. "(4 years)", "(2nd Gen)")
MPN_TITLE_RE = re.compile(r"\(([A-Z0-9][A-Z0-9\-/]{4,})\)")

# Secondary MPN extraction: in Electroline titles the second word is
# often the model number, e.g. "APPLE MTP43QL/A iPhone 15 5G ...".
# This pattern matches model-number-like strings (alphanumeric with
# at least one digit and one letter, optional slashes/dashes).
MODEL_NUMBER_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-/]{4,}$")

# Browser-like headers to avoid being blocked by the server.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Pydantic model ────────────────────────────────────────────────────────

class VariantRow(BaseModel):
    """One normalised row per product, ready for DB upsert.

    Mirrors the schema used by the other scrapers (istorm, kotsovolos,
    stephanis) so that all stores land in the same raw_products table
    with a consistent structure.
    """

    store: str = "electroline"             # Fixed identifier for this retailer
    store_product_id: str                  # Electroline internal SKU (e.g. "IT63725")
    title: str                             # Product name from JSON-LD
    vendor: str | None = None              # Brand name extracted from title
    product_type: str | None = None        # Category from JSON-LD (deepest level)
    sku: str | None = None                 # Electroline SKU (same as store_product_id)
    price: Decimal | None = None           # Current listed price in EUR
    available: bool                        # Whether the product is in stock
    image_url: str | None = None           # Product image URL
    product_url: str                       # Full URL to the product page
    mpn: str | None = None                 # Manufacturer Part Number
    ean: str | None = None                 # EAN/GTIN (not available from Electroline)
    mpn_root: str | None = None            # Region-independent MPN root
    identifier_source: str = "none"        # How the MPN was obtained: "sku", "api", "title_regex", or "none"
    scraped_at: datetime = Field(          # UTC timestamp of when this row was scraped
        default_factory=lambda: datetime.now(timezone.utc),
    )


# ── MPN extraction ────────────────────────────────────────────────────────

def extract_mpn_from_title(title: str) -> str | None:
    """Extract a manufacturer part number from the product title.

    Electroline titles follow two patterns:
      1. Parenthetical: "... iPhone 15 Pro (MTUX3ZD/A) ..."
         → We take the last parenthetical that looks like a part number.
      2. Second-word model: "APPLE MTP43QL/A iPhone 15 5G ..."
         → The second word is often the model/part number.

    We try parenthetical first (more reliable), then fall back to the
    second-word pattern.  Returns None if no valid MPN is found.
    """
    # Strategy 1: parenthetical match (highest confidence)
    matches = MPN_TITLE_RE.findall(title)
    valid = [m for m in matches if not m.isdigit() and not m.isalpha()]
    if valid:
        return valid[-1]

    # Strategy 2: second word in title (common in Electroline titles)
    # e.g. "BOSCH WGG244ZXGR Hygiene ..." → "WGG244ZXGR"
    parts = title.split()
    if len(parts) >= 2:
        candidate = parts[1]
        # Must look like a model number: mixed alphanumeric with digits
        if (MODEL_NUMBER_RE.match(candidate)
                and not candidate.isdigit()
                and not candidate.isalpha()
                and any(c.isdigit() for c in candidate)):
            return candidate

    return None


def extract_mpn_root(mpn: str | None) -> str | None:
    """Derive mpn_root for cross-store matching.

    Apple part numbers like MTUX3ZD/A have a region suffix (ZD/A);
    the root (MTUX3) is the region-independent identifier used to
    match the same product across different stores.

    Non-Apple part numbers pass through unchanged.
    """
    if mpn is None:
        return None
    m = APPLE_PN_RE.match(mpn)
    return m.group(1) if m else mpn


# ── Sitemap parsing ───────────────────────────────────────────────────────

def fetch_product_urls() -> list[str]:
    """Fetch the sitemap index and all product sub-sitemaps.

    Electroline uses a WordPress/Yoast sitemap structure:
      - sitemap_index.xml → lists sub-sitemaps
      - product-sitemap{N}.xml → 200 product URLs each

    We only follow sub-sitemaps whose filename contains "product-sitemap"
    to skip blog posts, pages, and other non-product content.

    Returns a deduplicated list of all product page URLs.
    """
    log.info("Fetching sitemap index: %s", SITEMAP_INDEX_URL)

    with httpx.Client(headers=DEFAULT_HEADERS, timeout=30, follow_redirects=True) as client:
        # Step 1: fetch the sitemap index to get sub-sitemap URLs
        resp = client.get(SITEMAP_INDEX_URL)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        all_sitemaps = [loc.text for loc in root.findall(".//s:sitemap/s:loc", SITEMAP_NS)]

        # Filter to product sitemaps only (skip post-sitemap, page-sitemap, etc.)
        product_sitemaps = [s for s in all_sitemaps if "product-sitemap" in s]
        log.info("Found %d sub-sitemaps (%d are product sitemaps).",
                 len(all_sitemaps), len(product_sitemaps))

        # Step 2: fetch each product sub-sitemap and collect URLs
        product_urls: list[str] = []
        for sm_url in product_sitemaps:
            resp = client.get(sm_url)
            resp.raise_for_status()
            sm_root = ET.fromstring(resp.text)
            urls = [loc.text for loc in sm_root.findall(".//s:url/s:loc", SITEMAP_NS)]
            product_urls.extend(urls)
            log.info("  %s: %d URLs", sm_url.split("/")[-1], len(urls))

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for u in product_urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)

    log.info("Total unique product URLs: %d", len(unique))
    return unique


# ── HTML / JSON-LD parsing ────────────────────────────────────────────────

def _find_product_jsonld(html: str) -> dict[str, Any] | None:
    """Extract the Product JSON-LD object from the HTML.

    Electroline uses Yoast SEO which puts structured data in a single
    <script> block containing a JSON object with "@graph" array.  We
    search through the @graph items for one with @type == "Product".

    Returns the Product dict, or None if not found.
    """
    # Find all script blocks that contain schema.org context
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    for script in scripts:
        if "@context" not in script or "schema.org" not in script:
            continue
        try:
            data = json.loads(script)
        except (json.JSONDecodeError, ValueError):
            continue

        # Handle @graph array (Yoast SEO style)
        graph = data.get("@graph", [data])
        for item in graph:
            item_type = item.get("@type", "")
            if item_type == "Product" or (isinstance(item_type, list) and "Product" in item_type):
                return item

    return None


def _extract_og_image(html: str) -> str | None:
    """Extract the product image URL from OpenGraph meta tags.

    Falls back to this when the JSON-LD image field only has a relative path.
    OG meta tags always have the full absolute URL.
    """
    match = re.search(r'<meta\s+property="og:image"[^>]*content="([^"]*)"', html)
    if match:
        return match.group(1)
    # Try reversed attribute order (some WordPress themes do this)
    match = re.search(r'<meta\s+content="([^"]*)"[^>]*property="og:image"', html)
    return match.group(1) if match else None


def _clean_title(raw_title: str) -> str:
    """Clean the product title from JSON-LD.

    Electroline appends " - Electroline" to all product names in the
    JSON-LD.  We strip that suffix and decode HTML entities.
    """
    title = unescape(raw_title)
    # Remove the store name suffix
    title = re.sub(r"\s*-\s*Electroline\s*$", "", title)
    return title.strip()


def _extract_brand(title: str) -> str | None:
    """Extract the brand name from the product title.

    Electroline titles consistently start with the brand name in
    uppercase, e.g. "APPLE MTP43QL/A iPhone 15 5G Smartphone 128GB".

    We take the first word if it's all uppercase letters (at least 2 chars).
    Returns None if the first word doesn't match the pattern.
    """
    parts = title.split()
    if parts and len(parts[0]) >= 2 and parts[0].isupper() and parts[0].isalpha():
        return parts[0]
    return None


def _parse_category(raw_category: str | None) -> str | None:
    """Parse the category string from JSON-LD into the deepest level.

    Electroline categories come as HTML-encoded breadcrumb strings like:
      "ΤΗΛΕΦΩΝΙΑ &amp; TABLETS &gt; Κινητή Τηλεφωνία &gt; Κινητά-Smartphones"

    We decode HTML entities, split on " > ", and return the last (deepest)
    category level.  Returns None if no category is available.
    """
    if not raw_category:
        return None
    decoded = unescape(raw_category)
    parts = [p.strip() for p in decoded.split(">")]
    return parts[-1] if parts else None


def parse_product_page(html: str, url: str) -> VariantRow | None:
    """Parse a single product page's HTML into a VariantRow.

    Extracts all product data from the JSON-LD @graph block and OG meta
    tags.  Returns None if the page doesn't contain valid product data
    (e.g. if the JSON-LD is missing or malformed).
    """
    # Step 1: find the Product JSON-LD in the HTML
    product = _find_product_jsonld(html)
    if product is None:
        log.debug("No Product JSON-LD found for %s", url)
        return None

    # Step 2: extract core fields from JSON-LD
    raw_title = product.get("name", "")
    title = _clean_title(raw_title)
    if not title:
        log.debug("Empty title for %s", url)
        return None

    sku = product.get("sku")
    if not sku:
        log.debug("No SKU for %s", url)
        return None

    # Step 3: extract price and availability from the offers block
    offers = product.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}

    price_str = offers.get("price")
    price = Decimal(price_str) if price_str else None

    availability_url = offers.get("availability", "")
    available = "InStock" in availability_url

    # Step 4: extract category (decode HTML entities, take deepest level)
    raw_category = product.get("category")
    product_type = _parse_category(raw_category)

    # Step 5: extract image URL (prefer OG meta for full URL, fall back to JSON-LD)
    image_url = _extract_og_image(html)
    if not image_url:
        img_data = product.get("image", {})
        if isinstance(img_data, dict):
            img_path = img_data.get("@id") or img_data.get("url", "")
        elif isinstance(img_data, str):
            img_path = img_data
        else:
            img_path = ""
        if img_path:
            # Make relative URLs absolute
            image_url = img_path if img_path.startswith("http") else BASE_URL + img_path

    # Step 6: extract brand from title
    vendor = _extract_brand(title)

    # Step 7: extract MPN and compute mpn_root
    # Electroline MPNs come from title patterns (parenthetical or second-word)
    mpn = extract_mpn_from_title(title)
    mpn_root = extract_mpn_root(mpn)
    identifier_source = "title_regex" if mpn else "none"

    return VariantRow(
        store="electroline",
        store_product_id=sku,
        title=title,
        vendor=vendor,
        product_type=product_type,
        sku=sku,
        price=price,
        available=available,
        image_url=image_url,
        product_url=url,
        mpn=mpn,
        ean=None,       # Electroline does not expose EAN/GTIN
        mpn_root=mpn_root,
        identifier_source=identifier_source,
    )


# ── Adaptive async fetcher ────────────────────────────────────────────────

class AdaptiveFetcher:
    """Async HTTP fetcher that automatically reduces concurrency on 429s.

    Manages a semaphore-bounded pool of concurrent requests.  When the
    server returns HTTP 429 (Too Many Requests), the concurrency limit
    is halved (down to MIN_CONCURRENCY) and the request is retried with
    exponential backoff.  This prevents overwhelming the server while
    still maintaining good throughput when the server is happy.
    """

    def __init__(self, max_concurrency: int = INITIAL_CONCURRENCY) -> None:
        self._max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._throttle_count = 0

    @property
    def concurrency(self) -> int:
        """Current effective concurrency limit."""
        return self._max_concurrency

    def _reduce_concurrency(self) -> None:
        """Halve the concurrency limit and replace the semaphore.

        Called when the server returns HTTP 429 (Too Many Requests).
        Creates a new semaphore with the reduced limit so that future
        tasks are genuinely constrained.  In-flight tasks that already
        acquired the old semaphore will finish naturally; only tasks
        starting after this call will use the new, tighter semaphore.
        """
        new_limit = max(MIN_CONCURRENCY, self._max_concurrency // 2)
        if new_limit < self._max_concurrency:
            self._max_concurrency = new_limit
            self._semaphore = asyncio.Semaphore(new_limit)
            self._throttle_count += 1
            log.warning(
                "429 received — reducing concurrency to %d (throttle #%d)",
                self._max_concurrency, self._throttle_count,
            )

    async def fetch(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> tuple[str, str | None]:
        """Fetch a URL with retries and adaptive rate limiting.

        Returns (url, html_content) on success, or (url, None) if all
        retries fail.  Handles 429s, 5xx errors, timeouts, and connection
        errors with exponential backoff.
        """
        async with self._semaphore:
            # Polite delay to pace requests even under concurrency
            await asyncio.sleep(REQUEST_DELAY)

            for attempt in range(MAX_RETRIES):
                try:
                    resp = await client.get(url)

                    if resp.status_code == 200:
                        return (url, resp.text)

                    if resp.status_code == 429 or resp.status_code >= 500:
                        if resp.status_code == 429:
                            self._reduce_concurrency()
                        wait = 2 ** attempt
                        log.warning(
                            "HTTP %s for %s (attempt %d), retrying in %ds…",
                            resp.status_code, url, attempt + 1, wait,
                        )
                        await asyncio.sleep(wait)
                        continue

                    # Non-retryable error (e.g. 404, 403)
                    log.debug("HTTP %s for %s — skipping", resp.status_code, url)
                    return (url, None)

                except httpx.TooManyRedirects:
                    # Redirect loop — skip this URL entirely (not retryable)
                    log.debug("Redirect loop for %s — skipping", url)
                    return (url, None)

                except (
                    httpx.TimeoutException,
                    httpx.ConnectError,
                    httpx.RemoteProtocolError,
                    httpx.ReadError,
                    httpx.WriteError,
                    httpx.CloseError,
                ) as e:
                    wait = 2 ** attempt
                    log.warning(
                        "%s for %s (attempt %d), retrying in %ds…",
                        type(e).__name__, url, attempt + 1, wait,
                    )
                    await asyncio.sleep(wait)

            # All retries exhausted
            log.error("All retries failed for %s", url)
            return (url, None)


# ── Fetch + parse pipeline ────────────────────────────────────────────────

async def fetch_and_parse_all(
    urls: list[str],
    fetcher: AdaptiveFetcher,
) -> tuple[list[VariantRow], list[str], int, int]:
    """Fetch all product pages and parse them immediately to save memory.

    Each page is parsed right after fetching — the raw HTML is discarded
    so we never hold thousands of HTML strings in memory at once.

    Returns:
      - rows: list of parsed VariantRow objects
      - failures: list of URLs that failed after all retries
      - jsonld_count: pages successfully parsed from JSON-LD
      - fallback_count: pages where JSON-LD was missing or unparseable
    """
    rows: list[VariantRow] = []
    failures: list[str] = []
    jsonld_count = 0
    fallback_count = 0
    total = len(urls)
    completed = 0
    start_time = time.monotonic()

    async with httpx.AsyncClient(
        headers=DEFAULT_HEADERS,
        timeout=30.0,
        follow_redirects=True,
        limits=httpx.Limits(
            max_connections=INITIAL_CONCURRENCY * 2,
            max_keepalive_connections=INITIAL_CONCURRENCY,
        ),
    ) as client:
        # Batch size adapts to the current concurrency limit so that
        # after a 429 throttle reduces _max_concurrency, future batches
        # spawn fewer concurrent tasks (the semaphore alone cannot be
        # resized, but smaller batches achieve the same effect).
        idx = 0
        while idx < total:
            batch_size = fetcher.concurrency * 10
            batch_urls = urls[idx : idx + batch_size]
            tasks = [fetcher.fetch(client, url) for url in batch_urls]
            results = await asyncio.gather(*tasks)
            idx += len(batch_urls)

            for url, html in results:
                completed += 1
                if html is not None:
                    row = parse_product_page(html, url)
                    if row is not None:
                        rows.append(row)
                        jsonld_count += 1
                    else:
                        fallback_count += 1
                else:
                    failures.append(url)

                # Log progress at regular intervals
                if completed % PROGRESS_INTERVAL == 0 or completed == total:
                    elapsed = time.monotonic() - start_time
                    rate = completed / elapsed if elapsed > 0 else 0
                    log.info(
                        "Progress: %d/%d (%.1f%%) — %.1f pages/sec — "
                        "concurrency=%d — %d failures",
                        completed, total, 100 * completed / total,
                        rate, fetcher.concurrency, len(failures),
                    )

    return rows, failures, jsonld_count, fallback_count


# ── Supabase upsert ───────────────────────────────────────────────────────

def upsert_to_supabase(rows: list[VariantRow]) -> None:
    """Upsert variant rows into the Supabase `raw_products` table.

    Uses a Postgres upsert (INSERT … ON CONFLICT UPDATE) keyed on
    (store, store_product_id) so that re-running the scraper updates
    existing rows rather than creating duplicates.

    Requires SUPABASE_URL and SUPABASE_SERVICE_KEY env vars.
    Silently skips if credentials are missing (useful for local dev).
    """
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        log.warning("SUPABASE_URL / SUPABASE_SERVICE_KEY not set — skipping DB upsert.")
        return

    from supabase import create_client

    sb = create_client(url, key)

    # Serialize Pydantic models to dicts with JSON-safe types
    records = []
    for r in rows:
        d = r.model_dump()
        d["price"] = float(d["price"]) if d["price"] is not None else None
        d["scraped_at"] = d["scraped_at"].isoformat()
        records.append(d)

    # Upsert in batches of 500 to stay within Supabase payload limits
    batch_size = 500
    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        sb.table("raw_products").upsert(
            batch, on_conflict="store,store_product_id",
        ).execute()
        log.info("Upserted batch %d–%d", i + 1, min(i + batch_size, len(records)))


# ── Main entry point ──────────────────────────────────────────────────────

async def async_main(limit: int | None = None) -> None:
    """Async entry point: sitemap → fetch → parse → export → upsert.

    Args:
        limit: If set, only process the first N product URLs (for testing).
    """
    log.info("=== Electroline scraper starting ===")

    # 1. Parse sitemap to get all product URLs
    product_urls = fetch_product_urls()

    if limit:
        log.info("TEST MODE: limiting to first %d products.", limit)
        product_urls = product_urls[:limit]

    # 2. Fetch all product pages and parse immediately (saves memory)
    log.info("Fetching %d product pages…", len(product_urls))
    fetcher = AdaptiveFetcher(max_concurrency=INITIAL_CONCURRENCY)
    rows, failures, jsonld_count, fallback_count = await fetch_and_parse_all(
        product_urls, fetcher,
    )
    log.info(
        "Parsed %d rows — %d from JSON-LD, %d pages missing JSON-LD, %d failures.",
        len(rows), jsonld_count, fallback_count, len(failures),
    )

    # 3. Log MPN extraction stats
    mpn_present = sum(1 for r in rows if r.mpn is not None)
    mpn_missing = sum(1 for r in rows if r.mpn is None)
    apple_pn = sum(1 for r in rows if r.mpn is not None and r.mpn_root != r.mpn)
    passthrough = sum(1 for r in rows if r.mpn is not None and r.mpn_root == r.mpn)
    ean_present = sum(1 for r in rows if r.ean is not None)
    log.info("MPNs — from title: %d | missing: %d", mpn_present, mpn_missing)
    log.info("mpn_root — Apple PN (shortened): %d | passthrough: %d", apple_pn, passthrough)
    log.info("EANs — present: %d (Electroline does not expose EAN)", ean_present)

    # 4. Dump all rows to a local JSON file
    out_path = Path("data/electroline.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    serializable = []
    for r in rows:
        d = r.model_dump()
        d["price"] = float(d["price"]) if d["price"] is not None else None
        d["scraped_at"] = d["scraped_at"].isoformat()
        serializable.append(d)
    out_path.write_text(
        json.dumps(serializable, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    log.info("Wrote %d rows to %s", len(serializable), out_path)

    # 5. Save failed URLs for potential retry
    if failures:
        errors_path = Path("data/electroline_errors.json")
        errors_path.write_text(
            json.dumps(failures, indent=2), encoding="utf-8",
        )
        log.info("Saved %d failed URLs to %s", len(failures), errors_path)

    # 6. Upsert to Supabase (no-op if credentials aren't configured)
    upsert_to_supabase(rows)

    # 7. Print sample rows for quick visual verification
    log.info("=== Sample rows ===")
    seen_categories: set[str | None] = set()
    sample_rows: list[VariantRow] = []
    for r in rows:
        if r.product_type not in seen_categories:
            seen_categories.add(r.product_type)
            sample_rows.append(r)
            if len(sample_rows) >= 5:
                break
    if len(sample_rows) < 5:
        for r in rows:
            if r not in sample_rows:
                sample_rows.append(r)
                if len(sample_rows) >= 5:
                    break

    for r in sample_rows:
        log.info(
            "  %s | €%s | sku=%s | mpn=%s | root=%s | cat=%s | avail=%s",
            r.title[:50], r.price, r.sku, r.mpn, r.mpn_root,
            r.product_type, r.available,
        )

    log.info("=== Done ===")


def main() -> None:
    """Synchronous entry point — runs the async scraper.

    Pass an integer argument to limit the number of products scraped
    (useful for testing), e.g.:
        python electroline_scraper.py 200
    """
    import sys

    if len(sys.argv) > 1 and not sys.argv[1].isdigit():
        log.error("Invalid argument '%s' — expected a numeric limit. Exiting.", sys.argv[1])
        return
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    asyncio.run(async_main(limit=limit))


if __name__ == "__main__":
    main()
