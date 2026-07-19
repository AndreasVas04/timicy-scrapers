"""iStorm Cyprus (istorm.com.cy) Shopify scraper.

Targets the iStorm Apple-authorised reseller store in Cyprus.
Uses the public Shopify /products.json endpoint to paginate through
the full product catalogue, flattens each product into one row per
variant (size / colour / storage, etc.), extracts manufacturer part
numbers (MPNs) from SKUs, and upserts the results into a Supabase
`raw_products` table for downstream price-comparison.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

BASE_URL = "https://istorm.com.cy"
PRODUCTS_ENDPOINT = f"{BASE_URL}/products.json"
PAGE_LIMIT = 250          # Shopify max items per page
REQUEST_DELAY = 1.0       # Seconds between paginated requests (polite crawling)
MAX_RETRIES = 6           # Retry attempts on 429 / 5xx before giving up
BASE_BACKOFF = 2.0        # Base seconds for exponential back-off (2, 4, 8, …)
MAX_BACKOFF = 60.0        # Hard cap on any single back-off wait (safety ceiling)
BACKOFF_JITTER = 1.0      # Max extra random seconds added to each back-off wait

# Matches Apple-style part numbers like "MH344TY/A" or "MQDT3ZM/A".
# Captures the region-independent root (e.g. "MH344") before the
# locale suffix (e.g. "TY/A").  Non-matching SKUs are left as-is.
APPLE_PN_RE = re.compile(r"^([A-Z0-9]{5,6})[A-Z]{1,3}/[A-Z]$")


# ── Pydantic model ──────────────────────────────────────────────

class VariantRow(BaseModel):
    """One normalised row per product variant, ready for DB upsert."""

    store: str = "istorm"                    # Fixed identifier for this retailer
    store_product_id: str                    # Shopify variant ID (unique per variant)
    title: str                               # Combined product + variant title
    vendor: str | None = None                # Brand / manufacturer (e.g. "Apple")
    product_type: str | None = None          # Shopify product type (e.g. "iPhone")
    sku: str | None = None                   # Store-assigned SKU
    price: Decimal | None = None             # Current listed price in EUR
    available: bool                          # Whether the variant is in stock
    image_url: str | None = None             # URL of the first product image
    product_url: str                         # Canonical URL to the product page
    mpn: str | None = None                   # Manufacturer Part Number (derived from SKU)
    mpn_root: str | None = None              # Region-independent MPN root for cross-store matching
    identifier_source: str = "none"          # How the MPN was obtained: "sku", "api", "title_regex", or "none"
    scraped_at: datetime = Field(            # UTC timestamp of when this row was scraped
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ── MPN extraction ──────────────────────────────────────────────

def extract_mpn(sku: str | None) -> str | None:
    """Derive the Manufacturer Part Number (MPN) from the variant SKU.

    iStorm populates the Shopify SKU field with the manufacturer part
    number, so the SKU is used directly as the MPN.  Returns None when
    the SKU is missing or empty (some accessories lack SKUs).
    """
    return sku if sku else None


def extract_mpn_root(mpn: str | None) -> str | None:
    """Derive mpn_root for cross-store matching.

    Apple part numbers like MH344TY/A have a region suffix (TY/A);
    the root (MH344) is the region-independent identifier.
    Non-Apple SKUs pass through unchanged.
    """
    if mpn is None:
        return None
    # Try to match an Apple-style part number (e.g. "MH344TY/A").
    # If it matches, return only the root portion (e.g. "MH344"),
    # stripping the region/locale suffix so the same product from
    # different regional stores can be matched together.
    m = APPLE_PN_RE.match(mpn)
    if m:
        return m.group(1)
    # Non-Apple SKUs pass through unchanged — they already serve
    # as a stable cross-store identifier.
    return mpn


# ── Fetcher ─────────────────────────────────────────────────────

def _parse_retry_after(value: str | None) -> float | None:
    """Interpret a Retry-After header and return the wait in seconds.

    RFC 9110 allows two forms, and Shopify may send either:
      * delta-seconds — a plain integer, e.g. "5"
      * HTTP-date     — an absolute timestamp, e.g. "Wed, 21 Oct 2026 07:28:00 GMT"

    For the HTTP-date form the remaining wait is the difference between
    that timestamp and now, computed in UTC so a machine running in a
    non-UTC local zone does not skew the result.

    Returns None when the header is absent, malformed, or resolves to a
    time already in the past.  The caller then falls back to its own
    exponential back-off, so a bad header can never stall the scrape.
    """
    if not value:
        return None

    # Form 1: delta-seconds.  Cheapest to check, and the common case.
    try:
        seconds = float(value.strip())
    except ValueError:
        pass
    else:
        # Negative or zero means "retry immediately"; treat as no hint.
        return seconds if seconds > 0 else None

    # Form 2: HTTP-date.  parsedate_to_datetime raises on malformed input
    # and may return a naive datetime if the string carries no zone, in
    # which case RFC 9110 says to read it as UTC.
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    delta = (when - datetime.now(timezone.utc)).total_seconds()
    return delta if delta > 0 else None


def _request_with_retry(client: httpx.Client, url: str, params: dict[str, Any]) -> httpx.Response:
    """Perform a GET request, retrying rate-limits and server errors.

    Retryable conditions:
      * HTTP 429 — rate-limited by the origin or its CDN
      * HTTP 5xx — server-side error, usually transient

    Any other non-200 status (a 4xx that is not 429) is a genuine client
    error that retrying cannot fix, so it raises immediately.  Transport
    errors (timeouts, connection resets) are deliberately NOT caught here
    and propagate to the caller — the observed failure mode is 429, and
    widening the retry net is out of scope for this pass.

    Wait time per attempt is the larger of the server's Retry-After hint
    and our own exponential back-off (BASE_BACKOFF * 2**attempt), then
    clamped to MAX_BACKOFF and given up to BACKOFF_JITTER extra random
    seconds.  Honouring Retry-After keeps us inside the limit the server
    actually asked for; the exponential floor covers servers that send no
    hint; the cap bounds the worst case so a single page cannot hang the
    run for minutes; and the jitter de-synchronises retries so repeated
    attempts do not land in lockstep with other traffic.
    """
    for attempt in range(MAX_RETRIES):
        resp = client.get(url, params=params)

        if resp.status_code == 200:
            return resp

        if resp.status_code == 429 or resp.status_code >= 500:
            # Retryable HTTP status.  A 429 normally carries Retry-After;
            # 5xx sometimes does.
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            log.warning(
                "HTTP %s on attempt %d/%d (Retry-After: %s)",
                resp.status_code, attempt + 1, MAX_RETRIES,
                resp.headers.get("Retry-After", "none"),
            )
        else:
            # Non-retryable client error (4xx other than 429) — fail fast
            # rather than burning attempts on a request that cannot succeed.
            resp.raise_for_status()

        # Exponential back-off floor: 2s, 4s, 8s, 16s, 32s, 60s (capped).
        backoff = BASE_BACKOFF * (2 ** attempt)
        # Respect the server's hint when it asks for longer than our floor.
        if retry_after is not None:
            backoff = max(backoff, retry_after)
        # Clamp before jitter so the ceiling is never exceeded by much.
        backoff = min(backoff, MAX_BACKOFF)
        wait = backoff + random.uniform(0.0, BACKOFF_JITTER)

        # Do not sleep after the final attempt — nothing follows it.
        if attempt == MAX_RETRIES - 1:
            break

        log.warning("Retrying in %.1fs (attempt %d/%d)…", wait, attempt + 1, MAX_RETRIES)
        time.sleep(wait)

    # All retries exhausted — re-raise the final retryable HTTP status.
    resp.raise_for_status()
    return resp  # unreachable for non-200, but keeps the type checker happy


def fetch_all_products() -> list[dict[str, Any]]:
    """Paginate through Shopify's /products.json and return every product dict.

    Shopify's public product API returns up to PAGE_LIMIT products per
    page.  We increment the page number until an empty list is returned,
    which signals the end of the catalogue.  A short delay between
    requests avoids tripping Shopify's rate limiter.
    """
    all_products: list[dict[str, Any]] = []
    page = 1
    with httpx.Client(
        headers={"User-Agent": "timicy-scraper/1.0 (price comparison project)"},
        timeout=30.0,
    ) as client:
        # Pagination loop: keep fetching until Shopify returns an empty page
        while True:
            log.info("Fetching page %d …", page)
            resp = _request_with_retry(client, PRODUCTS_ENDPOINT, {"limit": PAGE_LIMIT, "page": page})
            data = resp.json()
            products = data.get("products", [])
            if not products:
                # Empty page means we've passed the last product
                log.info("Page %d empty — pagination complete.", page)
                break
            all_products.extend(products)
            log.info("Page %d: %d products (running total: %d)", page, len(products), len(all_products))
            page += 1
            time.sleep(REQUEST_DELAY)  # Polite delay between requests
    return all_products


# ── Normalizer ──────────────────────────────────────────────────

def normalize(products: list[dict[str, Any]]) -> list[VariantRow]:
    """Flatten Shopify product dicts into one VariantRow per variant.

    A single Shopify product (e.g. "iPhone 15 Pro") may have many
    variants (storage sizes, colours).  This function explodes the
    product list so that every variant becomes its own row, carrying
    the parent product's metadata (title, vendor, image, URL) along.
    """
    rows: list[VariantRow] = []
    for p in products:
        # -- Product-level fields (shared across all variants) --
        product_title: str = p.get("title", "")
        vendor = p.get("vendor")
        product_type = p.get("product_type") or None  # Coerce empty string → None
        handle = p.get("handle", "")
        product_url = f"{BASE_URL}/products/{handle}"
        images = p.get("images") or []
        image_url = images[0]["src"] if images else None  # Use the first image only

        for v in p.get("variants", []):
            # Shopify sets variant title to "Default Title" when there's
            # only one variant — skip appending it to avoid clutter.
            variant_title = v.get("title", "")
            if variant_title and variant_title != "Default Title":
                full_title = f"{product_title} – {variant_title}"
            else:
                full_title = product_title

            # SKU → MPN → MPN root pipeline
            sku = v.get("sku") or None  # Coerce empty string → None
            mpn = extract_mpn(sku)
            mpn_root = extract_mpn_root(mpn)

            # iStorm uses the Shopify SKU field as the MPN directly
            identifier_source = "sku" if mpn else "none"

            rows.append(VariantRow(
                store_product_id=str(v["id"]),
                title=full_title,
                vendor=vendor,
                product_type=product_type,
                sku=sku,
                price=Decimal(v["price"]) if v.get("price") is not None else None,
                available=v.get("available", False),
                image_url=image_url,
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
    # same store + variant ID already exists, update it in place.
    batch_size = 500
    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        sb.table("raw_products").upsert(batch, on_conflict="store,store_product_id").execute()
        log.info("Upserted batch %d–%d", i + 1, min(i + batch_size, len(records)))


# ── Main ────────────────────────────────────────────────────────

def main() -> None:
    """Entry point: fetch → normalize → export JSON → upsert to Supabase.

    Pass an integer argument to limit the number of products processed
    (useful for testing), e.g.:
        python istorm_scraper.py 10
    """
    import sys

    if len(sys.argv) > 1 and not sys.argv[1].isdigit():
        log.error("Invalid argument '%s' — expected a numeric limit. Exiting.", sys.argv[1])
        return
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None

    log.info("=== iStorm scraper starting ===")

    # 1. Fetch the full product catalogue from Shopify
    products = fetch_all_products()
    log.info("Total products fetched: %d", len(products))

    # Optionally limit the number of products for testing
    if limit:
        log.info("TEST MODE: limiting to first %d products.", limit)
        products = products[:limit]

    # 2. Flatten into one row per variant
    rows = normalize(products)
    log.info("Total variant rows: %d", len(rows))

    # 3. Log MPN extraction stats for sanity-checking
    mpn_present = sum(1 for r in rows if r.mpn is not None)
    mpn_missing = sum(1 for r in rows if r.mpn is None)
    # "Apple PN" = SKUs where mpn_root differs from mpn (region suffix stripped)
    apple_pn = sum(1 for r in rows if r.mpn is not None and r.mpn_root != r.mpn)
    # "passthrough" = non-Apple SKUs where mpn_root == mpn (no transformation)
    passthrough = sum(1 for r in rows if r.mpn is not None and r.mpn_root == r.mpn)
    log.info("MPNs — present: %d | missing: %d", mpn_present, mpn_missing)
    log.info("mpn_root — Apple PN (shortened): %d | passthrough: %d", apple_pn, passthrough)

    # 4. Dump all rows to a local JSON file (useful for debugging / offline analysis)
    out_path = Path("data/istorm.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    serializable = []
    for r in rows:
        d = r.model_dump()
        d["price"] = float(d["price"]) if d["price"] is not None else None
        d["scraped_at"] = d["scraped_at"].isoformat()
        serializable.append(d)
    out_path.write_text(json.dumps(serializable, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote %d rows to %s", len(serializable), out_path)

    # 5. Upsert to Supabase (no-op if credentials aren't configured)
    upsert_to_supabase(rows)

    # 6. Print a few sample rows for quick visual verification
    log.info("=== Sample rows ===")
    for r in rows[:5]:
        log.info("  %s | €%s | mpn=%s | mpn_root=%s", r.title[:60], r.price, r.mpn, r.mpn_root)

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
