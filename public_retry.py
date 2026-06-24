"""Retry failed Public.cy product IDs with conservative rate limiting.

Reads IDs from data/public_errors.json, fetches each via the same
/public/v2/sku/{id} API, merges recovered products into data/public.json
(deduped by store_product_id), and saves still-failing IDs to
data/public_errors_retry.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

# Reuse all parsing logic from the main scraper
from public_scraper import (
    DEFAULT_HEADERS,
    PRODUCT_API_URL,
    VariantRow,
    parse_api_response,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# Conservative rate limiting
CONCURRENCY = 1
REQUEST_DELAY = 3.0
MAX_RETRIES = 3
PROGRESS_INTERVAL = 100


async def fetch_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    product_id: str,
) -> tuple[str, dict | None]:
    """Fetch a single product with retries and slow pacing."""
    url = PRODUCT_API_URL.format(product_id=product_id)

    async with semaphore:
        await asyncio.sleep(REQUEST_DELAY)

        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.get(url)

                if resp.status_code == 200:
                    if not resp.text.strip():
                        return (product_id, None)
                    try:
                        return (product_id, resp.json())
                    except (json.JSONDecodeError, ValueError):
                        return (product_id, None)

                if resp.status_code == 404:
                    return (product_id, None)

                if resp.status_code == 429 or resp.status_code >= 500:
                    wait = (2 ** attempt) * 5  # longer backoff
                    log.warning(
                        "HTTP %s for %s (attempt %d), retrying in %ds…",
                        resp.status_code, product_id, attempt + 1, wait,
                    )
                    await asyncio.sleep(wait)
                    continue

                log.debug("HTTP %s for %s — skipping", resp.status_code, product_id)
                return (product_id, None)

            except (
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.RemoteProtocolError,
                httpx.ReadError,
                httpx.WriteError,
                httpx.CloseError,
            ) as e:
                wait = (2 ** attempt) * 5
                log.warning(
                    "%s for %s (attempt %d), retrying in %ds…",
                    type(e).__name__, product_id, attempt + 1, wait,
                )
                await asyncio.sleep(wait)

        log.error("All retries failed for %s", product_id)
        return (product_id, None)


async def async_main() -> None:
    # Check that required input files exist before proceeding
    errors_path = Path("data/public_errors.json")
    if not errors_path.exists():
        log.warning("data/public_errors.json not found — nothing to retry.")
        return

    existing_path = Path("data/public.json")
    if not existing_path.exists():
        log.warning("data/public.json not found — run the main Public scraper first.")
        return

    product_ids: list[str] = json.loads(errors_path.read_text())
    log.info("Loaded %d failed IDs to retry", len(product_ids))

    if not product_ids:
        log.info("No failed IDs to retry — exiting.")
        return

    # Load existing data for merge
    existing: list[dict] = json.loads(existing_path.read_text())
    existing_count = len(existing)
    log.info("Existing public.json has %d rows", existing_count)

    rows: list[VariantRow] = []
    failures: list[str] = []
    skip_count = 0
    total = len(product_ids)
    completed = 0
    start_time = time.monotonic()

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient(
        headers=DEFAULT_HEADERS,
        timeout=30.0,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
    ) as client:
        # Process sequentially in small batches (concurrency=1 anyway)
        batch_size = 10
        for batch_start in range(0, total, batch_size):
            batch_ids = product_ids[batch_start : batch_start + batch_size]
            tasks = [fetch_one(client, semaphore, pid) for pid in batch_ids]
            results = await asyncio.gather(*tasks)

            for product_id, data in results:
                completed += 1
                if data is not None:
                    row = parse_api_response(data, product_id)
                    if row is not None:
                        rows.append(row)
                    else:
                        skip_count += 1
                else:
                    failures.append(product_id)

                if completed % PROGRESS_INTERVAL == 0 or completed == total:
                    elapsed = time.monotonic() - start_time
                    rate = completed / elapsed if elapsed > 0 else 0
                    log.info(
                        "Progress: %d/%d (%.1f%%) — %.2f/sec — "
                        "%d recovered, %d skipped, %d still failing",
                        completed, total, 100 * completed / total,
                        rate, len(rows), skip_count, len(failures),
                    )

    log.info("Retry complete: %d recovered, %d skipped, %d still failing",
             len(rows), skip_count, len(failures))

    # Merge into existing data, dedup by store_product_id
    existing_ids = {r["store_product_id"] for r in existing}
    new_records = []
    for r in rows:
        d = r.model_dump()
        d["price"] = float(d["price"]) if d["price"] is not None else None
        d["scraped_at"] = d["scraped_at"].isoformat()
        if d["store_product_id"] not in existing_ids:
            new_records.append(d)
            existing_ids.add(d["store_product_id"])
        else:
            # Update existing record
            for i, ex in enumerate(existing):
                if ex["store_product_id"] == d["store_product_id"]:
                    existing[i] = d
                    break

    merged = existing + new_records
    existing_path.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    log.info("Merged: %d existing + %d new = %d total in public.json",
             existing_count, len(new_records), len(merged))

    # Save still-failing IDs
    retry_errors_path = Path("data/public_errors_retry.json")
    retry_errors_path.write_text(json.dumps(failures, indent=2), encoding="utf-8")
    log.info("Saved %d still-failing IDs to %s", len(failures), retry_errors_path)

    # Estimated time info
    elapsed = time.monotonic() - start_time
    log.info("Total time: %.0f seconds (%.1f minutes)", elapsed, elapsed / 60)


if __name__ == "__main__":
    asyncio.run(async_main())
