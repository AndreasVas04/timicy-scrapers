"""Bionic Cyprus (bionic.com.cy) product scraper.

Targets Bionic Electronics, a Cyprus-native computers/gaming/components
retailer since 1990 with stores in 4 cities (~5,200 products).

The site is built on "Reactive Retail" (React on Rails).  Product data
is NOT in JSON-LD or microdata — it's embedded as a JSON blob inside a
<script type="application/json" id="App-react-component-..."> tag that
React hydrates on the client.  This JSON contains the full product
object at the top level, so we can extract everything with plain httpx
(no browser rendering needed).

Strategy:
  1. Fetch the gzipped XML sitemap to collect all product page URLs.
  2. Filter to English-language URLs (/en/products/...) to avoid
     scraping the same product twice (Greek /el/ pages are duplicates).
  3. Fetch each product page with async httpx and extract the embedded
     React component JSON from the HTML.
  4. Parse product data from the JSON: title, sku, price (inc-VAT),
     availability, images, category, and product attributes.
  5. Extract brand from the first word of the title (the site does not
     have a reliable brand field in the API).
  6. Extract MPN from structured attributes ("Manufacturer Part Number"
     or "Part Number" labels) or from parenthetical regex on the title.
  7. Apply Apple part-number root extraction for cross-store matching.
  8. Upsert into Supabase `raw_products` and dump to data/bionic.json.

JSON data structure (inside the <script> tag):
  {
    "componentPath": "products/Product",
    "product": {
      "id": 9210,
      "sku": "12000856",
      "title": "Corsair iCUE LINK RX140 ...",
      "price_value": 31.89,          # ex-VAT
      "total_price_value": 37.95,     # inc-VAT (what customers pay)
      "inStock": true,
      "totalStock": 3,
      "images": [{"preview": "https://...", "thumbnail": "https://..."}],
      "attributes": [
        {"prototypeAttributeId": 3057, "translations": [{"locale": "en", "title": "CO-9051019-WW"}]}
      ],
      "storeLocations": [{"title": "Nicosia Store", "inStock": true}, ...],
      ...
    },
    "category": {
      "translations": [{"locale": "en", "title": "Case Cooling Fans"}],
      "attributes": [
        {"id": 3057, "translations": [{"locale": "en", "title": "Manufacturer Part Number"}]},
        {"id": 2528, "translations": [{"locale": "en", "title": "Brand"}]},
        ...
      ]
    },
    "currency": "EUR"
  }

Data sources per field:
  - title:            product.title
  - vendor (brand):   first word of title (no reliable structured brand field)
  - product_type:     category.translations[locale=en].title
  - sku:              product.sku  (Bionic's internal SKU number)
  - price:            product.total_price_value  (inc-VAT, in EUR)
  - available:        product.inStock
  - image_url:        product.images[0].preview (or .thumbnail)
  - product_url:      the fetched URL itself
  - store_product_id: str(product.id)
  - ean:              from category attributes labeled "UPC"/"EAN"/"GTIN"/"Barcode"
  - mpn:              from category attributes labeled "Manufacturer Part Number"
                      / "Part Number" / "MPN", else parenthetical title regex
  - mpn_root:         Apple PN root extraction, else passthrough
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
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

# ── Constants ────────────────────────────────────────────────────────────

BASE_URL = "https://bionic.com.cy"
SITEMAP_URL = f"{BASE_URL}/sitemap_products.xml.gz"

# XML namespace used in sitemap files.
SITEMAP_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# Concurrency and rate-limiting settings.
# Bionic has ~5,200 products.  At 5 concurrent with 200ms delay,
# that's roughly 5200 / 5 * 0.2 ≈ 3.5 minutes.
INITIAL_CONCURRENCY = 5       # Max concurrent HTTP requests
MIN_CONCURRENCY = 1           # Floor when auto-throttling on 429s
REQUEST_DELAY = 0.2           # Seconds between requests per coroutine
MAX_RETRIES = 3               # Retry attempts on 429 / 5xx responses
PROGRESS_INTERVAL = 500       # Log progress every N products

# Regex to match Apple-style part numbers like "MH344TY/A" or "MQDT3ZM/A".
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

# Regex to find the React component JSON blob in the HTML.
# Bionic uses React on Rails which embeds component data in a
# <script type="application/json" id="App-react-component-{uuid}"> tag.
REACT_JSON_RE = re.compile(
    r'<script type="application/json"[^>]*id="App-react-component-[^"]*">(.*?)</script>',
    re.DOTALL,
)

# Browser-like headers for HTTP requests.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Category attribute labels that indicate a Manufacturer Part Number.
# Different product categories use different label names, so we match
# against multiple known variants (case-insensitive).
MPN_LABELS = {"manufacturer part number", "part number", "mpn"}

# Category attribute labels that indicate an EAN/UPC/GTIN barcode.
EAN_LABELS = {"upc", "ean", "gtin", "barcode", "gtin13"}

# Category attribute labels that indicate a brand name.
BRAND_LABELS = {"brand"}


# ── Pydantic model ──────────────────────────────────────────────────────

class VariantRow(BaseModel):
    """One normalised row per product, ready for DB upsert.

    Mirrors the schema used by the other scrapers (istorm, kotsovolos,
    stephanis, electroline, public) so that all stores land in the same
    raw_products table with a consistent structure.
    """

    store: str = "bionic"                  # Fixed identifier for this retailer
    store_product_id: str                  # Bionic product ID (e.g. "9210")
    title: str                             # Product name from JSON
    vendor: str | None = None              # Brand name (first word of title)
    product_type: str | None = None        # Category from JSON
    sku: str | None = None                 # Bionic internal SKU
    price: Decimal | None = None           # Price including VAT in EUR
    available: bool                        # Whether the product is in stock
    image_url: str | None = None           # Product image URL
    product_url: str                       # Full URL to the product page
    mpn: str | None = None                 # Manufacturer Part Number
    ean: str | None = None                 # EAN/UPC barcode if available
    mpn_root: str | None = None            # Region-independent MPN root
    identifier_source: str = "none"        # How the MPN was obtained: "sku", "api", "title_regex", or "none"
    scraped_at: datetime = Field(          # UTC timestamp of when this row was scraped
        default_factory=lambda: datetime.now(timezone.utc),
    )


# ── MPN extraction ──────────────────────────────────────────────────────

def extract_mpn_from_title(title: str) -> str | None:
    """Extract a manufacturer part number from parenthetical text in the title.

    Bionic titles sometimes include model numbers in parentheses,
    e.g. "Corsair iCUE LINK RX140 ... (CO-9051019-WW)".

    We take the *last* parenthetical match that looks like a part number
    (5+ alphanumeric/dash/slash characters).  Filters out pure digits
    (years) and pure letters (size codes like "XXL").

    Returns None if no valid part number pattern is found.
    """
    matches = MPN_TITLE_RE.findall(title)
    valid = [m for m in matches if not m.isdigit() and not m.isalpha()]
    return valid[-1] if valid else None


def extract_mpn_root(mpn: str | None) -> str | None:
    """Derive mpn_root for cross-store matching.

    Apple part numbers like MTUX3ZD/A have a region suffix (ZD/A);
    the root (MTUX3) is the region-independent identifier.
    Non-Apple part numbers pass through unchanged.
    """
    if mpn is None:
        return None
    m = APPLE_PN_RE.match(mpn)
    return m.group(1) if m else mpn


# ── Sitemap parsing ─────────────────────────────────────────────────────

def fetch_product_urls() -> list[str]:
    """Fetch the product sitemap and extract English product page URLs.

    Bionic's sitemap is at /sitemap_products.xml.gz and contains URLs
    for both English (/en/) and Greek (/el/) versions of each product.
    We only keep the English URLs to avoid scraping duplicates.

    The sitemap may be served as gzipped binary or as plain XML despite
    the .gz extension, so we try gzip decompression first and fall back
    to plain text.

    Returns a list of unique English product URLs.
    """
    log.info("Fetching product sitemap: %s", SITEMAP_URL)

    with httpx.Client(headers=DEFAULT_HEADERS, timeout=30, follow_redirects=True) as client:
        resp = client.get(SITEMAP_URL)
        resp.raise_for_status()

        # Handle gzipped or plain XML
        try:
            xml_text = gzip.decompress(resp.content).decode("utf-8")
            log.info("Sitemap decompressed from gzip (%d bytes → %d chars).",
                     len(resp.content), len(xml_text))
        except gzip.BadGzipFile:
            xml_text = resp.text
            log.info("Sitemap served as plain XML (%d chars).", len(xml_text))

        root = ET.fromstring(xml_text)
        all_urls = [loc.text for loc in root.findall(".//s:url/s:loc", SITEMAP_NS)]
        log.info("Total URLs in sitemap: %d", len(all_urls))

        # Filter to English product URLs only (skip /el/ duplicates)
        en_urls = [u for u in all_urls if "/en/products/" in u]

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for url in en_urls:
            if url not in seen:
                seen.add(url)
                unique.append(url)

        log.info("English product URLs (deduplicated): %d", len(unique))
        return unique


# ── HTML / JSON parsing ─────────────────────────────────────────────────

def _extract_en_translation(translations: list[dict], field: str = "title") -> str | None:
    """Get the English translation value from a translations array.

    Bionic's React JSON stores localised strings as:
      [{"locale": "en", "title": "Laptops"}, {"locale": "el", "title": "Φορητοί"}]

    Returns the value of `field` for locale "en", or None if not found.
    """
    for t in translations:
        if t.get("locale") == "en":
            return t.get(field)
    return None


def _build_attr_label_map(category: dict) -> dict[int, str]:
    """Build a mapping from category attribute ID → English label.

    Each product category defines a set of attribute "slots" (e.g. Brand,
    UPC, Manufacturer Part Number).  Products then store attribute values
    keyed by these slot IDs.  This function creates the reverse map so
    we can look up what each attribute means.

    Returns {attribute_id: english_label} for all attributes in the category.
    """
    label_map: dict[int, str] = {}
    for attr in category.get("attributes", []):
        label = _extract_en_translation(attr.get("translations", []))
        if label:
            label_map[attr["id"]] = label
    return label_map


def _extract_from_attributes(
    product_attrs: list[dict],
    label_map: dict[int, str],
    target_labels: set[str],
) -> str | None:
    """Search product attributes for a value matching one of the target labels.

    Iterates through the product's attribute list, maps each attribute ID
    to its label using the category's label_map, and returns the first
    non-empty English value whose label matches any of the target_labels.

    For example, to find the MPN:
      _extract_from_attributes(attrs, label_map, {"manufacturer part number", "part number"})

    Returns None if no matching attribute with a non-empty value is found.
    """
    for attr in product_attrs:
        attr_id = attr.get("prototypeAttributeId")
        label = label_map.get(attr_id, "")
        if label.lower() in target_labels:
            val = _extract_en_translation(attr.get("translations", []))
            if val and val.strip():
                return val.strip()
    return None


def _extract_brand_from_title(title: str) -> str | None:
    """Extract the brand name from the first word of the product title.

    Bionic titles consistently start with the brand name:
      "Corsair iCUE LINK RX140 ..."  → "Corsair"
      "Samsung Galaxy S25 ..."        → "Samsung"
      "HP F6U65AE 302 Tri-Colour"    → "HP"

    Returns the first word of the title, or None if the title is empty.
    """
    if not title:
        return None
    return title.split()[0]


def parse_product_page(html: str, url: str) -> VariantRow | None:
    """Parse a Bionic product page's HTML and extract a VariantRow.

    Finds the React component JSON blob embedded in the page, extracts
    the product and category data, maps category attributes to labels,
    and builds a normalised VariantRow.

    Returns None if the page doesn't contain valid product data (e.g.
    the product was removed or the page redirected to a 404).
    """
    # Step 1: Find the React component JSON blob in the HTML.
    match = REACT_JSON_RE.search(html)
    if not match:
        log.debug("No React JSON found in %s", url)
        return None

    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        log.debug("Invalid JSON in React component for %s", url)
        return None

    # Step 2: Extract the product and category objects.
    # These are top-level keys in the JSON (not inside appProps).
    product = data.get("product")
    if not product or not isinstance(product, dict) or not product.get("id"):
        log.debug("No product data in %s", url)
        return None

    category = data.get("category") or {}

    # Step 3: Extract basic fields.
    title = product.get("title", "").strip()
    if not title:
        return None

    product_id = str(product["id"])
    sku = product.get("sku")
    if sku:
        sku = str(sku)

    # Price: use total_price_value (inc-VAT) which is what customers pay.
    total_price = product.get("total_price_value")
    price = Decimal(str(total_price)) if total_price and total_price > 0 else None

    # Availability: the inStock boolean from the JSON.
    available = bool(product.get("inStock", False))

    # Step 4: Extract category name from translations.
    cat_translations = category.get("translations", [])
    product_type = _extract_en_translation(cat_translations)

    # Step 5: Extract image URL (first image's preview or thumbnail).
    images = product.get("images", [])
    image_url = None
    if images and isinstance(images, list):
        first_img = images[0]
        image_url = (
            first_img.get("preview")
            or first_img.get("thumbnail")
            or first_img.get("banner")
        )

    # Step 6: Build the category attribute label map and extract
    # brand, MPN, and EAN from structured attributes.
    label_map = _build_attr_label_map(category)
    product_attrs = product.get("attributes", [])

    # Try to get brand from structured attributes first, fall back to title.
    brand = _extract_from_attributes(product_attrs, label_map, BRAND_LABELS)
    if not brand:
        brand = _extract_brand_from_title(title)

    # Try to get MPN from structured attributes first, fall back to title regex.
    # Track the source so the matching layer knows how reliable the MPN is.
    mpn = _extract_from_attributes(product_attrs, label_map, MPN_LABELS)
    if mpn:
        identifier_source = "api"
    else:
        mpn = extract_mpn_from_title(title)
        identifier_source = "title_regex" if mpn else "none"

    # Try to get EAN from structured attributes.
    ean = _extract_from_attributes(product_attrs, label_map, EAN_LABELS)

    # Step 7: Derive mpn_root for cross-store matching.
    mpn_root = extract_mpn_root(mpn)

    return VariantRow(
        store="bionic",
        store_product_id=product_id,
        title=title,
        vendor=brand,
        product_type=product_type,
        sku=sku,
        price=price,
        available=available,
        image_url=image_url,
        product_url=url,
        mpn=mpn,
        ean=ean,
        mpn_root=mpn_root,
        identifier_source=identifier_source,
    )


# ── Adaptive async fetcher ──────────────────────────────────────────────

class AdaptiveFetcher:
    """Async HTTP fetcher that automatically reduces concurrency on 429s.

    Manages a semaphore-bounded pool of concurrent requests.  When the
    server returns HTTP 429, the concurrency limit is halved (down to
    MIN_CONCURRENCY) and the request is retried with exponential backoff.

    This pattern is shared across all scrapers in the project to be
    respectful of each store's server capacity.
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

    async def fetch_page(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> tuple[str, str | None]:
        """Fetch a product page's HTML with retries and backoff.

        Returns (url, html) on success, or (url, None) if all retries
        fail.  Handles 429s (rate limiting), 5xx (server errors),
        timeouts, and connection errors with exponential backoff.

        Pages that redirect to "page-not-found" are treated as missing
        products and return (url, None) without retrying.
        """
        async with self._semaphore:
            # Polite delay to pace requests and avoid overwhelming the server
            await asyncio.sleep(REQUEST_DELAY)

            for attempt in range(MAX_RETRIES):
                try:
                    resp = await client.get(url)

                    # Check if the page redirected to a 404 page.
                    # Bionic returns 200 but redirects to /page-not-found
                    # for removed products.
                    if "page-not-found" in str(resp.url):
                        return (url, None)

                    if resp.status_code == 200:
                        return (url, resp.text)

                    if resp.status_code == 404:
                        return (url, None)

                    if resp.status_code == 429 or resp.status_code >= 500:
                        if resp.status_code == 429:
                            self._reduce_concurrency()
                        wait = 2 ** attempt
                        log.warning(
                            "HTTP %s for %s (attempt %d), retrying in %ds…",
                            resp.status_code, url.split("/")[-1][:40],
                            attempt + 1, wait,
                        )
                        await asyncio.sleep(wait)
                        continue

                    # Non-retryable error (e.g. 403)
                    log.debug("HTTP %s for %s — skipping",
                              resp.status_code, url.split("/")[-1][:40])
                    return (url, None)

                except httpx.TooManyRedirects:
                    log.debug("Redirect loop for %s — skipping",
                              url.split("/")[-1][:40])
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
                        type(e).__name__, url.split("/")[-1][:40],
                        attempt + 1, wait,
                    )
                    await asyncio.sleep(wait)

            # All retries exhausted
            log.error("All retries failed for %s", url.split("/")[-1][:40])
            return (url, None)


# ── Fetch + parse pipeline ──────────────────────────────────────────────

async def fetch_and_parse_all(
    urls: list[str],
    fetcher: AdaptiveFetcher,
) -> tuple[list[VariantRow], list[str], int, int]:
    """Fetch all product pages and parse them immediately.

    Each page's HTML is parsed right after fetching — the raw HTML is
    discarded so memory stays bounded.  This is the same parse-on-fetch
    pattern used by the other scrapers in this project.

    Returns:
      - rows: list of parsed VariantRow objects
      - failures: list of URLs that failed after all retries
      - success_count: products with valid data extracted
      - skip_count: products where page was fetched but had no data
    """
    rows: list[VariantRow] = []
    failures: list[str] = []
    success_count = 0
    skip_count = 0
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
            tasks = [fetcher.fetch_page(client, url) for url in batch_urls]
            results = await asyncio.gather(*tasks)
            idx += len(batch_urls)

            for url, html in results:
                completed += 1
                if html is not None:
                    row = parse_product_page(html, url)
                    if row is not None:
                        rows.append(row)
                        success_count += 1
                    else:
                        skip_count += 1
                else:
                    failures.append(url)

                # Log progress at regular intervals
                if completed % PROGRESS_INTERVAL == 0 or completed == total:
                    elapsed = time.monotonic() - start_time
                    rate = completed / elapsed if elapsed > 0 else 0
                    log.info(
                        "Progress: %d/%d (%.1f%%) — %.1f products/sec — "
                        "concurrency=%d — %d parsed, %d skipped, %d failures",
                        completed, total, 100 * completed / total,
                        rate, fetcher.concurrency,
                        success_count, skip_count, len(failures),
                    )

    return rows, failures, success_count, skip_count


# ── Supabase upsert ─────────────────────────────────────────────────────

def upsert_to_supabase(rows: list[VariantRow]) -> None:
    """Upsert variant rows into the Supabase `raw_products` table.

    Uses INSERT … ON CONFLICT UPDATE keyed on (store, store_product_id).
    Silently skips if SUPABASE_URL / SUPABASE_SERVICE_KEY env vars are
    not set, allowing the scraper to run without a database.
    """
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        log.warning("SUPABASE_URL / SUPABASE_SERVICE_KEY not set — skipping DB upsert.")
        return

    from supabase import create_client

    sb = create_client(url, key)

    records = []
    for r in rows:
        d = r.model_dump()
        d["price"] = float(d["price"]) if d["price"] is not None else None
        d["scraped_at"] = d["scraped_at"].isoformat()
        records.append(d)

    # Upsert in batches of 500 to avoid request size limits.
    batch_size = 500
    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        sb.table("raw_products").upsert(
            batch, on_conflict="store,store_product_id",
        ).execute()
        log.info("Upserted batch %d–%d", i + 1, min(i + batch_size, len(records)))


# ── Main entry point ────────────────────────────────────────────────────

async def async_main(limit: int | None = None) -> None:
    """Async entry point: sitemap → fetch → parse → export → upsert.

    Args:
        limit: If set, only process the first N products (for testing).
               Pass None to scrape all products.
    """
    log.info("=== Bionic Cyprus scraper starting ===")

    # 1. Parse sitemap to get all English product URLs.
    product_urls = fetch_product_urls()

    if limit:
        log.info("TEST MODE: limiting to first %d products.", limit)
        product_urls = product_urls[:limit]

    # 2. Fetch all product pages and parse immediately.
    log.info("Fetching %d product pages…", len(product_urls))
    fetcher = AdaptiveFetcher(max_concurrency=INITIAL_CONCURRENCY)
    rows, failures, success_count, skip_count = await fetch_and_parse_all(
        product_urls, fetcher,
    )
    log.info(
        "Parsed %d rows — %d successes, %d skipped, %d failures.",
        len(rows), success_count, skip_count, len(failures),
    )

    # 3. Log extraction stats for MPN, EAN, and brand coverage.
    mpn_present = sum(1 for r in rows if r.mpn is not None)
    # Count how many MPNs could have come from the title regex alone.
    # If the MPN matches what the title regex would produce, it might be
    # from either source (attribute takes priority in the code).
    mpn_also_in_title = sum(
        1 for r in rows
        if r.mpn is not None and r.mpn == extract_mpn_from_title(r.title)
    )
    ean_present = sum(1 for r in rows if r.ean is not None)
    apple_pn = sum(1 for r in rows if r.mpn is not None and r.mpn_root != r.mpn)
    vendor_present = sum(1 for r in rows if r.vendor is not None)
    log.info("MPNs — total: %d | also in title: %d | attribute-only: %d",
             mpn_present, mpn_also_in_title, mpn_present - mpn_also_in_title)
    log.info("EANs — present: %d", ean_present)
    log.info("mpn_root — Apple PN shortened: %d", apple_pn)
    log.info("Vendors — present: %d", vendor_present)

    # 4. Dump all rows to a local JSON file.
    out_path = Path("data/bionic.json")
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

    # 5. Save failed URLs for potential retry.
    if failures:
        errors_path = Path("data/bionic_errors.json")
        errors_path.write_text(
            json.dumps(failures, indent=2), encoding="utf-8",
        )
        log.info("Saved %d failed URLs to %s", len(failures), errors_path)

    # 6. Upsert to Supabase (no-op if credentials aren't configured).
    upsert_to_supabase(rows)

    # 7. Print sample rows for quick visual verification.
    log.info("=== Sample rows ===")
    # Try to show rows from different categories for variety.
    seen_categories: set[str | None] = set()
    sample_rows: list[VariantRow] = []
    for r in rows:
        if r.product_type not in seen_categories:
            seen_categories.add(r.product_type)
            sample_rows.append(r)
            if len(sample_rows) >= 5:
                break
    # Fill up to 5 if we didn't get enough categories
    if len(sample_rows) < 5:
        for r in rows:
            if r not in sample_rows:
                sample_rows.append(r)
                if len(sample_rows) >= 5:
                    break

    for r in sample_rows:
        log.info(
            "  %s | €%s | sku=%s | mpn=%s | ean=%s | cat=%s | avail=%s | vendor=%s",
            r.title[:50], r.price, r.sku, r.mpn, r.ean,
            r.product_type, r.available, r.vendor,
        )

    log.info("=== Done ===")


def main() -> None:
    """Synchronous entry point — runs the async scraper.

    Pass an integer argument to limit the number of products scraped:
        python bionic_scraper.py 200
    """
    import sys

    if len(sys.argv) > 1 and not sys.argv[1].isdigit():
        log.error("Invalid argument '%s' — expected a numeric limit. Exiting.", sys.argv[1])
        return
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    asyncio.run(async_main(limit=limit))


if __name__ == "__main__":
    main()
