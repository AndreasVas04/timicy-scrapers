"""Public Cyprus (public.cy) product scraper.

Targets Public, a Greek electronics and media retailer operating in
Cyprus (~88k products including electronics, books, music, toys, etc.).

The site is an Angular SPA — no product data is in the raw HTML.
However, Public exposes a clean JSON REST API that returns all product
details in a single call, so we use plain async httpx against the API
directly (no browser rendering needed).

Strategy:
  1. Parse the gzipped XML sitemap to collect all product page URLs.
  2. Extract the numeric product ID from each URL's last path segment.
  3. Call the JSON API /public/v2/sku/{id} for each product to get:
     name, brand, category (breadcrumb), image, price, availability,
     EAN (from topSpecs), and other metadata.
  4. Extract MPN from parenthetical patterns in the product title.
  5. Apply Apple part-number root extraction for cross-store matching.
  6. Upsert into Supabase `raw_products` and dump to data/public.json.

API endpoints discovered via Playwright network interception:
  - /public/v2/sku/{id}        → product details, price, availability
  - /public/v1/mm/productPage  → (alternative) price + stock only

Data sources per field:
  - title:            sku.displayName
  - vendor (brand):   sku.brand.displayName
  - product_type:     breadcrumb second-to-last item (category, not product name)
  - sku:              sku.id  (same as store_product_id)
  - price:            sku.priceInfoDto.salePrice
  - available:        sku.availability == "Άμεσα Διαθέσιμο" (or similar in-stock text)
  - image_url:        sku.media.heroImage.large
  - product_url:      constructed from sku.url
  - ean:              from topSpecs where displayName == "EAN"
  - mpn:              parenthetical regex on title
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

# ── Constants ──────────────────────────────────────────────────────────────

BASE_URL = "https://www.public.cy"
SITEMAP_INDEX_URL = f"{BASE_URL}/sitemap.xml"

# XML namespace used in sitemap files.
SITEMAP_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# The JSON API endpoint that returns full product details including
# name, brand, category, image, price, availability, and specs.
# Takes a numeric product ID (from the sitemap URL's last path segment).
PRODUCT_API_URL = f"{BASE_URL}/public/v2/sku/{{product_id}}"

# Concurrency and rate-limiting settings.
# Public.cy is behind Akamai with a token-bucket rate limit (capacity
# ~120 requests).  At >4 req/sec the bucket drains and Akamai responds
# with 403.  Measured safe sustained rates from probing: 1.76 and 3.98
# req/sec both produced zero 403s over 300 unique SKUs each.  We target
# 2.5 req/sec as a conservative CI operating point.
INITIAL_CONCURRENCY = 5       # Max in-flight HTTP requests (semaphore cap)
MIN_CONCURRENCY = 1           # Floor when auto-throttling on 429s
MAX_RETRIES = 3               # Retry attempts on 429 / 403 / 5xx responses
PROGRESS_INTERVAL = 500       # Log progress every N products

# Global rate cap: maximum requests per second across ALL coroutines.
# This is the single pacing mechanism — the semaphore only caps in-flight
# requests, while the RateLimiter enforces a minimum interval between
# request starts.  Can be overridden via the PUBLIC_SCRAPER_RATE env var.
TARGET_REQUESTS_PER_SEC = 2.5

# 403 cooldown: Akamai returns 403 (not 429) when the token bucket is
# exhausted.  A burst of consecutive 403s means the bucket is empty and
# we must pause to let it refill.  Measured recovery: ~27s of silence
# refills the bucket; 45s adds margin.
FORBIDDEN_STREAK_THRESHOLD = 10   # consecutive 403s that trigger a cooldown
FORBIDDEN_COOLDOWN_SECONDS = 45   # pause duration to let the bucket refill

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

# Full browser header set for all HTTP requests.
# Public.cy is behind Akamai, which blocks datacenter IPs unless the
# request carries a complete, consistent set of browser headers.  A bare
# User-Agent is not enough — Akamai fingerprints the full header profile
# (sec-ch-ua, Sec-Fetch-*, etc.) and challenges requests that look
# automated.  These headers match a real Chrome 125 on Windows and were
# validated via curl from a GitHub Actions datacenter IP (HTTP 200).
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
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

# Greek text values that indicate the product is in stock.
# Used as a fallback when the numeric offerCount field is missing.
# Normalized to lowercase for safer comparison.
IN_STOCK_TEXTS = {"άμεσα διαθέσιμο", "διαθέσιμο"}


# ── Pydantic model ────────────────────────────────────────────────────────

class VariantRow(BaseModel):
    """One normalised row per product, ready for DB upsert.

    Mirrors the schema used by the other scrapers (istorm, kotsovolos,
    stephanis, electroline) so that all stores land in the same
    raw_products table with a consistent structure.
    """

    store: str = "public"                  # Fixed identifier for this retailer
    store_product_id: str                  # Public numeric product ID (e.g. "2061014")
    title: str                             # Product name from API
    vendor: str | None = None              # Brand name from API
    product_type: str | None = None        # Category from breadcrumb
    sku: str | None = None                 # Same as store_product_id
    price: Decimal | None = None           # Current listed price in EUR
    available: bool                        # Whether the product is in stock
    image_url: str | None = None           # Product image URL
    product_url: str                       # Full URL to the product page
    mpn: str | None = None                 # Manufacturer Part Number (from title regex)
    ean: str | None = None                 # EAN/GTIN from topSpecs if available
    mpn_root: str | None = None            # Region-independent MPN root
    identifier_source: str = "none"        # How the MPN was obtained: "sku", "api", "title_regex", or "none"
    scraped_at: datetime = Field(          # UTC timestamp of when this row was scraped
        default_factory=lambda: datetime.now(timezone.utc),
    )


# ── MPN extraction ────────────────────────────────────────────────────────

def extract_mpn_from_title(title: str) -> str | None:
    """Extract a manufacturer part number from parenthetical text in the title.

    Public.cy titles sometimes include model numbers in parentheses,
    e.g. "iPhone 15 Pro 256GB (MTUX3ZD/A) Natural Titanium".

    We take the *last* parenthetical match that looks like a part number
    (5+ alphanumeric/dash/slash characters).  Filters out pure digits
    (years) and pure letters (acronyms).

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


# ── Sitemap parsing ───────────────────────────────────────────────────────

# TimiCY-relevant top-level URL path segments.
# These are the category prefixes in the sitemap URLs that correspond
# to electronics, appliances, and tech products.  Everything else
# (books, music, comics, toys, stationery, gifts, etc.) is excluded.
TIMICY_CATEGORIES = {
    "tilefonia",                      # smartphones, wearables
    "computers-and-software",         # laptops, desktops
    "tablets",                        # tablets
    "perifereiaka",                   # monitors, peripherals
    "tileoraseis",                    # TVs, TV accessories
    "ihos",                           # headphones, speakers, audio
    "gaming",                         # consoles, games, accessories
    "fotografia",                     # cameras, lenses
    "home",                           # smart-home, lighting, security
    "oikiakes-syskeyes",              # refrigerators, washing machines, ovens
    "oikiakes-mikrosyskeyes",         # vacuum cleaners, coffee machines, air fryers
    "thermansi-klimatismos",          # air conditioners, heating
}


def _url_matches_category(url: str) -> bool:
    """Check if a product URL belongs to a TimiCY-relevant category.

    Product URLs look like:
      https://www.public.cy/product/tilefonia/kinita-smartphones/...
    We check the first path segment after '/product/' against TIMICY_CATEGORIES.
    """
    # Strip the base and split: ['', 'product', 'tilefonia', 'kinita-smartphones', ...]
    path = url.replace(BASE_URL, "").strip("/")
    parts = path.split("/")
    # parts[0] == "product", parts[1] == top-level category
    if len(parts) >= 2:
        return parts[1] in TIMICY_CATEGORIES
    return False


def fetch_product_ids() -> list[str]:
    """Fetch the sitemap index and extract product IDs from product URLs.

    Public.cy uses a standard sitemap index that points to gzipped
    sub-sitemaps.  Product sitemaps have filenames containing "products".
    Each product URL ends with a numeric ID, e.g.:
      https://www.public.cy/product/tilefonia/.../smartphone-name/1929671

    We extract these numeric IDs since we'll call the API directly
    rather than scraping HTML pages.

    Only includes products from TimiCY-relevant categories (electronics,
    appliances, tech).  Books, music, comics, toys, etc. are excluded.

    Returns a deduplicated list of product ID strings.
    """
    log.info("Fetching sitemap index: %s", SITEMAP_INDEX_URL)

    with httpx.Client(headers=DEFAULT_HEADERS, timeout=30, follow_redirects=True) as client:
        # Step 1: fetch the sitemap index
        resp = client.get(SITEMAP_INDEX_URL)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        all_sitemaps = [loc.text for loc in root.findall(".//s:sitemap/s:loc", SITEMAP_NS)]

        # Filter to product sitemaps only
        product_sitemaps = [s for s in all_sitemaps if "products" in s]
        log.info("Found %d sub-sitemaps (%d are product sitemaps).",
                 len(all_sitemaps), len(product_sitemaps))

        # Step 2: fetch each product sub-sitemap and extract product IDs
        product_ids: list[str] = []
        total_urls = 0
        skipped_urls = 0
        for sm_url in product_sitemaps:
            resp = client.get(sm_url)
            resp.raise_for_status()

            # Handle gzipped or plain XML (the .xml.gz files may not be gzipped)
            try:
                xml_text = gzip.decompress(resp.content).decode("utf-8")
            except gzip.BadGzipFile:
                xml_text = resp.text

            sm_root = ET.fromstring(xml_text)
            urls = [loc.text for loc in sm_root.findall(".//s:url/s:loc", SITEMAP_NS)]
            total_urls += len(urls)

            # Extract the numeric product ID from the last URL path segment,
            # but only for URLs in TimiCY-relevant categories.
            sm_matched = 0
            sm_skipped = 0
            for url in urls:
                pid = url.rstrip("/").split("/")[-1]
                if pid.isdigit():
                    if _url_matches_category(url):
                        product_ids.append(pid)
                        sm_matched += 1
                    else:
                        sm_skipped += 1

            skipped_urls += sm_skipped
            log.info("  %s: %d URLs, %d matched TimiCY categories, %d skipped",
                     sm_url.split("/")[-1], len(urls), sm_matched, sm_skipped)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for pid in product_ids:
        if pid not in seen:
            seen.add(pid)
            unique.append(pid)

    log.info("Total sitemap URLs: %d — filtered to %d TimiCY-relevant products (skipped %d)",
             total_urls, len(unique), skipped_urls)
    return unique


# ── API response parsing ──────────────────────────────────────────────────

def _extract_ean(top_specs: list[dict]) -> str | None:
    """Extract EAN/GTIN from the product's topSpecs list.

    Public.cy includes EAN as a spec with displayName "EAN" for some
    products (mostly music/media).  Returns the first valid EAN found,
    or None if not present.
    """
    for spec in top_specs:
        if spec.get("displayName") == "EAN":
            values = spec.get("values", [])
            if values:
                ean = values[0].strip()
                # Validate: EAN should be 8-14 digits
                if ean.isdigit() and 8 <= len(ean) <= 14:
                    return ean
    return None


def _extract_category(breadcrumb: list[dict]) -> str | None:
    """Extract the product category from the breadcrumb trail.

    The breadcrumb array looks like:
      [{"displayName": "Τηλεοράσεις & Ήχος"}, {"displayName": "Τηλεοράσεις"},
       {"displayName": "Product Name"}]

    The last item is the product name itself, so we take the second-to-last
    as the category.  If there's only one breadcrumb (the product name),
    we return None.
    """
    if not breadcrumb or len(breadcrumb) < 2:
        return None
    return breadcrumb[-2].get("displayName")


def parse_api_response(data: dict, product_id: str) -> VariantRow | None:
    """Parse the /public/v2/sku/{id} API response into a VariantRow.

    Returns None if the response is missing essential fields (e.g. no
    sku data, indicating the product may have been removed).
    """
    sku = data.get("sku")
    if not sku or not isinstance(sku, dict):
        log.debug("No sku data for product %s", product_id)
        return None

    # Product name
    title = sku.get("displayName", "").strip()
    if not title:
        log.debug("Empty title for product %s", product_id)
        return None

    # Brand from the brand object
    brand_obj = sku.get("brand")
    vendor = None
    if isinstance(brand_obj, dict):
        vendor = brand_obj.get("displayName")
    elif isinstance(brand_obj, str):
        vendor = brand_obj

    # Category from breadcrumb (second-to-last item)
    breadcrumb = data.get("breadcrumb", [])
    product_type = _extract_category(breadcrumb)

    # Price from priceInfoDto
    price_info = sku.get("priceInfoDto", {})
    sale_price = price_info.get("salePrice")
    price = Decimal(str(sale_price)) if sale_price and sale_price > 0 else None

    # Availability: prefer the numeric offerCount field (locale-independent).
    # offerCount > 0 means the product has purchasable offers.
    # Fall back to Greek availability text only if offerCount is missing.
    offer_count = sku.get("offerCount")
    if offer_count is not None:
        available = int(offer_count) > 0
    else:
        availability_text = sku.get("availability", "").strip().lower()
        available = availability_text in IN_STOCK_TEXTS

    # Image URL from media.heroImage
    media = sku.get("media", {})
    hero = media.get("heroImage", {})
    image_url = hero.get("large") or hero.get("url1")

    # Product URL (constructed from the relative path in the API response)
    url_path = sku.get("url", "")
    product_url = BASE_URL + url_path if url_path else f"{BASE_URL}/product/{product_id}"

    # EAN from topSpecs
    top_specs = sku.get("topSpecs", [])
    ean = _extract_ean(top_specs)

    # MPN from title regex (Public has no structured MPN field)
    mpn = extract_mpn_from_title(title)
    mpn_root = extract_mpn_root(mpn)
    identifier_source = "title_regex" if mpn else "none"

    return VariantRow(
        store="public",
        store_product_id=product_id,
        title=title,
        vendor=vendor,
        product_type=product_type,
        sku=product_id,
        price=price,
        available=available,
        image_url=image_url,
        product_url=product_url,
        mpn=mpn,
        ean=ean,
        mpn_root=mpn_root,
        identifier_source=identifier_source,
    )


# ── Global rate limiter ───────────────────────────────────────────────────

class RateLimiter:
    """Asyncio-safe pacer that enforces a maximum global request rate.

    Maintains a "next allowed" timestamp.  Each call to acquire() sleeps
    until its time slot, then advances the timestamp by 1/rate seconds.
    This guarantees that no matter how many coroutines call acquire()
    concurrently, the true request-start rate never exceeds `rate` per
    second.

    The semaphore in AdaptiveFetcher caps *in-flight* requests; this
    class caps *request starts*.  Both are needed: the semaphore prevents
    memory bloat from thousands of pending responses, while the rate
    limiter prevents the token-bucket drain that triggers Akamai 403s.
    """

    def __init__(self, rate: float) -> None:
        self._interval = 1.0 / rate        # minimum seconds between request starts
        self._lock = asyncio.Lock()         # serialises slot assignment
        self._next_allowed = 0.0            # monotonic timestamp of next open slot

    async def acquire(self) -> None:
        """Wait until the next available time slot, then claim it."""
        async with self._lock:
            now = asyncio.get_event_loop().time()
            # If we're ahead of schedule, sleep until the slot opens.
            if now < self._next_allowed:
                await asyncio.sleep(self._next_allowed - now)
            # Advance the slot for the next caller.
            self._next_allowed = max(now, self._next_allowed) + self._interval


class ForbiddenTracker:
    """Asyncio-safe tracker for consecutive HTTP 403 responses.

    Akamai returns 403 (not 429) when its token bucket is exhausted.
    A burst of consecutive 403s means the bucket is empty and we must
    pause to let it refill.  Any non-403 outcome resets the counter,
    because isolated 403s can be per-product blocks (not rate walls).
    """

    def __init__(self, threshold: int, cooldown: float) -> None:
        self._threshold = threshold          # consecutive 403s that trigger a cooldown
        self._cooldown = cooldown            # seconds to sleep when the wall is hit
        self._lock = asyncio.Lock()          # protects _streak and _cooling_down
        self._streak = 0                     # current consecutive-403 count
        self._cooling_down = False           # True while a cooldown sleep is active

    async def record_403(self) -> bool:
        """Record a 403 response.  Returns True if a cooldown was triggered.

        When the streak reaches the threshold AND no cooldown is already
        in progress, logs a warning and sleeps for the cooldown duration.
        Other coroutines that hit 403 during the cooldown will see
        _cooling_down=True and wait for it to finish rather than
        triggering additional overlapping cooldowns.
        """
        async with self._lock:
            self._streak += 1
            if self._streak >= self._threshold and not self._cooling_down:
                self._cooling_down = True
                log.warning(
                    "403 wall detected (%d consecutive) — cooling down for %ds.",
                    self._streak, self._cooldown,
                )
                # Release the lock during sleep so other coroutines can
                # check _cooling_down and skip duplicate cooldowns.
                self._lock.release()
                try:
                    await asyncio.sleep(self._cooldown)
                finally:
                    await self._lock.acquire()
                self._streak = 0
                self._cooling_down = False
                return True
            return False

    async def reset(self) -> None:
        """Reset the streak counter on any non-403 outcome."""
        async with self._lock:
            self._streak = 0


# ── Adaptive async fetcher ────────────────────────────────────────────────

class AdaptiveFetcher:
    """Async HTTP fetcher that automatically reduces concurrency on 429s.

    Manages a semaphore-bounded pool of concurrent requests.  When the
    server returns HTTP 429, the concurrency limit is halved (down to
    MIN_CONCURRENCY) and the request is retried with exponential backoff.
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

    async def fetch_json(
        self,
        client: httpx.AsyncClient,
        product_id: str,
        rate_limiter: RateLimiter,
        forbidden_tracker: ForbiddenTracker,
    ) -> tuple[str, dict | None]:
        """Fetch a product's JSON from the API with retries.

        Returns (product_id, json_data) on success, or (product_id, None)
        if all retries fail.  Handles 403s (Akamai rate wall with cooldown),
        429s (explicit rate-limit with concurrency reduction), 5xx errors,
        timeouts, and connection errors with exponential backoff.

        The rate_limiter.acquire() call immediately before each client.get()
        is the single global pacing mechanism: it enforces a minimum interval
        between request starts across all coroutines.  The semaphore caps
        in-flight requests but does not pace them.
        """
        url = PRODUCT_API_URL.format(product_id=product_id)

        async with self._semaphore:
            for attempt in range(MAX_RETRIES):
                try:
                    # Wait for the global rate limiter before sending.
                    # This is the only pacing mechanism — it enforces the
                    # target req/sec across all concurrent coroutines.
                    await rate_limiter.acquire()
                    resp = await client.get(url)

                    if resp.status_code == 200:
                        # Reset the 403 streak on any successful response.
                        await forbidden_tracker.reset()
                        # Some products return empty body or non-JSON on 200
                        if not resp.text.strip():
                            return (product_id, None)
                        try:
                            return (product_id, resp.json())
                        except (json.JSONDecodeError, ValueError):
                            return (product_id, None)

                    if resp.status_code == 404:
                        # Product doesn't exist — skip silently.
                        # Still counts as a non-403 outcome for streak purposes.
                        await forbidden_tracker.reset()
                        return (product_id, None)

                    if resp.status_code == 403:
                        # Akamai token-bucket exhausted — retryable with
                        # cooldown awareness.  If the streak reaches the
                        # threshold, the tracker pauses all coroutines for
                        # FORBIDDEN_COOLDOWN_SECONDS to let the bucket refill.
                        triggered = await forbidden_tracker.record_403()
                        if not triggered:
                            # Below threshold — normal exponential backoff.
                            wait = 2 ** attempt
                            log.warning(
                                "HTTP 403 for %s (attempt %d), retrying in %ds…",
                                product_id, attempt + 1, wait,
                            )
                            await asyncio.sleep(wait)
                        # If triggered, the tracker already slept the cooldown.
                        continue

                    if resp.status_code == 429 or resp.status_code >= 500:
                        # Reset 403 streak — this is a different error class.
                        await forbidden_tracker.reset()
                        if resp.status_code == 429:
                            self._reduce_concurrency()
                        wait = 2 ** attempt
                        log.warning(
                            "HTTP %s for %s (attempt %d), retrying in %ds…",
                            resp.status_code, product_id, attempt + 1, wait,
                        )
                        await asyncio.sleep(wait)
                        continue

                    # Other non-retryable errors (e.g. 400, 405).
                    await forbidden_tracker.reset()
                    log.debug("HTTP %s for %s — skipping", resp.status_code, product_id)
                    return (product_id, None)

                except httpx.TooManyRedirects:
                    await forbidden_tracker.reset()
                    log.debug("Redirect loop for %s — skipping", product_id)
                    return (product_id, None)

                except (
                    httpx.TimeoutException,
                    httpx.ConnectError,
                    httpx.RemoteProtocolError,
                    httpx.ReadError,
                    httpx.WriteError,
                    httpx.CloseError,
                ) as e:
                    await forbidden_tracker.reset()
                    wait = 2 ** attempt
                    log.warning(
                        "%s for %s (attempt %d), retrying in %ds…",
                        type(e).__name__, product_id, attempt + 1, wait,
                    )
                    await asyncio.sleep(wait)

            # All retries exhausted
            log.error("All retries failed for %s", product_id)
            return (product_id, None)


# ── Fetch + parse pipeline ────────────────────────────────────────────────

async def fetch_and_parse_all(
    product_ids: list[str],
    fetcher: AdaptiveFetcher,
    rate_limiter: RateLimiter,
    forbidden_tracker: ForbiddenTracker,
) -> tuple[list[VariantRow], list[str], int, int]:
    """Fetch all products from the API and parse them immediately.

    Each API response is parsed right after fetching — the raw JSON is
    discarded so memory stays bounded.

    The rate_limiter and forbidden_tracker are shared across all
    coroutines to enforce global pacing and 403 cooldown behaviour.

    Returns:
      - rows: list of parsed VariantRow objects
      - failures: list of product IDs that failed after all retries
      - success_count: products with valid API data
      - skip_count: products where API returned no data (404, empty, etc.)
    """
    rows: list[VariantRow] = []
    failures: list[str] = []
    success_count = 0
    skip_count = 0
    total = len(product_ids)
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
            batch_ids = product_ids[idx : idx + batch_size]
            tasks = [
                fetcher.fetch_json(client, pid, rate_limiter, forbidden_tracker)
                for pid in batch_ids
            ]
            results = await asyncio.gather(*tasks)
            idx += len(batch_ids)

            for product_id, data in results:
                completed += 1
                if data is not None:
                    row = parse_api_response(data, product_id)
                    if row is not None:
                        rows.append(row)
                        success_count += 1
                    else:
                        skip_count += 1
                else:
                    failures.append(product_id)

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


# ── Supabase upsert ───────────────────────────────────────────────────────

def upsert_to_supabase(rows: list[VariantRow]) -> None:
    """Upsert variant rows into the Supabase `raw_products` table.

    Uses INSERT … ON CONFLICT UPDATE keyed on (store, store_product_id).
    Silently skips if credentials are missing.
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

    batch_size = 500
    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        sb.table("raw_products").upsert(
            batch, on_conflict="store,store_product_id",
        ).execute()
        log.info("Upserted batch %d–%d", i + 1, min(i + batch_size, len(records)))


# ── Main entry point ──────────────────────────────────────────────────────

async def async_main(limit: int | None = None) -> None:
    """Async entry point: sitemap → API fetch → parse → export → upsert.

    Args:
        limit: If set, only process the first N products (for testing).
    """
    log.info("=== Public.cy scraper starting ===")

    # 1. Determine the effective global rate cap.
    # The default (TARGET_REQUESTS_PER_SEC = 2.5) is conservative for
    # Akamai's token bucket.  An optional env var allows tuning without
    # code changes (e.g. for testing a faster rate in a controlled run).
    effective_rate = TARGET_REQUESTS_PER_SEC
    rate_override = os.environ.get("PUBLIC_SCRAPER_RATE", "").strip()
    if rate_override:
        try:
            effective_rate = float(rate_override)
        except ValueError:
            log.warning("Invalid PUBLIC_SCRAPER_RATE=%r — using default %.1f",
                        rate_override, TARGET_REQUESTS_PER_SEC)
    log.info("Global rate cap: %.1f req/sec", effective_rate)

    # 2. Parse sitemap to get all product IDs
    product_ids = fetch_product_ids()

    if limit:
        log.info("TEST MODE: limiting to first %d products.", limit)
        product_ids = product_ids[:limit]

    # 3. Create the shared rate limiter and 403 tracker.
    # - RateLimiter enforces a minimum interval between request starts
    #   across ALL coroutines (the single global pacing mechanism).
    # - ForbiddenTracker detects Akamai's 403 rate wall and triggers a
    #   cooldown pause to let the token bucket refill.
    rate_limiter = RateLimiter(rate=effective_rate)
    forbidden_tracker = ForbiddenTracker(
        threshold=FORBIDDEN_STREAK_THRESHOLD,
        cooldown=FORBIDDEN_COOLDOWN_SECONDS,
    )

    # 4. Fetch all products from API and parse immediately
    log.info("Fetching %d products from API…", len(product_ids))
    fetcher = AdaptiveFetcher(max_concurrency=INITIAL_CONCURRENCY)
    rows, failures, success_count, skip_count = await fetch_and_parse_all(
        product_ids, fetcher, rate_limiter, forbidden_tracker,
    )
    log.info(
        "Parsed %d rows — %d API successes, %d skipped, %d failures.",
        len(rows), success_count, skip_count, len(failures),
    )

    # 3. Log extraction stats
    mpn_present = sum(1 for r in rows if r.mpn is not None)
    mpn_missing = sum(1 for r in rows if r.mpn is None)
    apple_pn = sum(1 for r in rows if r.mpn is not None and r.mpn_root != r.mpn)
    passthrough = sum(1 for r in rows if r.mpn is not None and r.mpn_root == r.mpn)
    ean_present = sum(1 for r in rows if r.ean is not None)
    log.info("MPNs — from title regex: %d | missing: %d", mpn_present, mpn_missing)
    log.info("mpn_root — Apple PN (shortened): %d | passthrough: %d", apple_pn, passthrough)
    log.info("EANs — present: %d", ean_present)

    # 4. Dump all rows to a local JSON file
    out_path = Path("data/public.json")
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

    # 5. Save failed IDs for potential retry
    if failures:
        errors_path = Path("data/public_errors.json")
        errors_path.write_text(
            json.dumps(failures, indent=2), encoding="utf-8",
        )
        log.info("Saved %d failed product IDs to %s", len(failures), errors_path)

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
            "  %s | €%s | sku=%s | mpn=%s | ean=%s | cat=%s | avail=%s",
            r.title[:50], r.price, r.sku, r.mpn, r.ean,
            r.product_type, r.available,
        )

    log.info("=== Done ===")


def main() -> None:
    """Synchronous entry point — runs the async scraper.

    Pass an integer argument to limit the number of products scraped:
        python public_scraper.py 200
    """
    import sys

    if len(sys.argv) > 1 and not sys.argv[1].isdigit():
        log.error("Invalid argument '%s' — expected a numeric limit. Exiting.", sys.argv[1])
        return
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    asyncio.run(async_main(limit=limit))


if __name__ == "__main__":
    main()
