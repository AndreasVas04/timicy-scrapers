#!/usr/bin/env python3
"""
run_nightly.py
--------------
Canonical nightly pipeline orchestrator for timicy-scrapers.

Coordinates the full data pipeline end-to-end:
  1. Scrape   — run all 6 store scrapers concurrently as subprocesses.
  2. Validate — verify each store's JSON output exists and is sane.
  3. Ingest   — bulk-load validated stores into the database.
  4. Match    — run the matching writer to build/update canonical products.
  5. Revalidate — poke the web app's cache revalidation endpoint.
  6. Alerts   — notify the web app to dispatch price-alert emails.

Per-store failure isolation: a scraper crash or suspiciously small output
excludes that store from ingestion but does NOT block the rest of the
pipeline.  This is critical because absence of data is NOT the same as
unavailability — if a store's scraper fails, we must leave its existing
DB rows untouched rather than marking all its offers as disappeared.

Designed to run both locally (with .env for secrets) and inside GitHub
Actions (where secrets come from the environment).

Usage:
  python run_nightly.py                       # full pipeline, all stores
  python run_nightly.py --dry-run             # writer rolls back, no revalidation
  python run_nightly.py --skip-scrape         # use existing data/*.json files
  python run_nightly.py --stores istorm public
"""

import argparse
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Store registry
# ---------------------------------------------------------------------------
# Each entry is (store_name, scraper_script_filename, expected_product_count).
# expected_count is the approximate number of products a healthy full scrape
# should return.  The validation step uses 50% of this as a floor to catch
# silent layout changes that would otherwise cause --mark-disappeared to
# mass-mark offers as unavailable downstream.

STORE_REGISTRY: list[tuple[str, str, int]] = [
    ("istorm",      "istorm_scraper.py",      2048),
    ("kotsovolos",  "kotsovolos_scraper.py",   8907),
    ("stephanis",   "stephanis_scraper.py",    26266),
    ("electroline", "electroline_scraper.py",  9354),
    ("public",      "public_scraper.py",       11792),
    ("bionic",      "bionic_scraper.py",       5180),
]

# Per-stage subprocess timeouts (seconds).  Normal runtimes are ~19s each,
# so 1800s is ~90× headroom.  The purpose is to convert an infinite hang
# (e.g. a stalled DB connection) into a fast, clearly-labeled stage failure
# instead of waiting for the CI job-level timeout hours later.
#
# Scrapers intentionally have NO per-process timeout — their runtime varies
# legitimately (longest store ~1h40m) and the CI job timeout remains their
# backstop.
INGEST_TIMEOUT_S = 1800
WRITER_TIMEOUT_S = 1800

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
LOGS_DIR = REPO_ROOT / "logs"


# ---------------------------------------------------------------------------
# Phase 1: Scrape — launch all selected scrapers concurrently
# ---------------------------------------------------------------------------

def run_scrapers(selected_stores: list[tuple[str, str, int]]) -> dict[str, int]:
    """Launch scrapers concurrently, wait for all, return {store: exit_code}.

    Each scraper runs as a subprocess with stdout+stderr redirected to
    logs/<store>.log (overwritten per run).  Parallel execution between
    stores is safe: each scraper is a single serial HTTP client with its
    own polite delays built in — they do not share sessions or rate limits.

    After all scrapers finish, their log files are printed sequentially
    with clear separators so CI output is readable and never interleaved.
    """
    LOGS_DIR.mkdir(exist_ok=True)

    # Launch all scrapers concurrently.
    processes: list[tuple[str, subprocess.Popen, Path, io.TextIOWrapper]] = []
    for store, script, _expected in selected_stores:
        log_path = LOGS_DIR / f"{store}.log"
        log_file = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, script],
            cwd=str(REPO_ROOT),
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        processes.append((store, proc, log_path, log_file))

    print(f"  Launched {len(processes)} scrapers concurrently, waiting...")

    # Wait for all to finish.  No timeout here — the CI job-level timeout
    # is the backstop (typically 4-5 hours).
    exit_codes: dict[str, int] = {}
    for store, proc, _log_path, log_file in processes:
        proc.wait()
        log_file.close()
        exit_codes[store] = proc.returncode

    # Print each store's log sequentially so CI output is verbatim-verifiable
    # and never interleaved between stores.
    for store, _proc, log_path, _lf in processes:
        print(f"\n{'=' * 60}")
        print(f"  {store} (exit code: {exit_codes[store]})")
        print(f"{'=' * 60}")
        if log_path.exists():
            print(log_path.read_text(encoding="utf-8", errors="replace"))
        else:
            print("  (no log file)")

    return exit_codes


# ---------------------------------------------------------------------------
# Phase 2: Validate — check each store's JSON output
# ---------------------------------------------------------------------------

def validate_stores(
    selected_stores: list[tuple[str, str, int]],
    exit_codes: dict[str, int] | None,
    skip_scrape: bool,
) -> tuple[list[str], dict[str, str], dict[str, int]]:
    """Validate each store's data file and return (passed, failures, row_counts).

    A store PASSES only if ALL of the following hold:
      a. Scraper exit code == 0 (skipped under --skip-scrape).
      b. data/<store>.json exists and parses as valid JSON.
      c. Row count >= 50% of expected_count.

    The 50% threshold guards against silent website layout changes that
    would cause a scraper to return a near-empty result.  Without this
    check, --mark-disappeared would interpret the missing products as
    removed from the catalog and mass-mark them unavailable, corrupting
    price comparisons.

    Failed stores are EXCLUDED from ingestion entirely — their DB rows
    must not be touched at all, because absence of scraped data does not
    mean the products are unavailable; it means we failed to observe them.
    """
    passed: list[str] = []
    failures: dict[str, str] = {}
    row_counts: dict[str, int] = {}

    for store, _script, expected in selected_stores:
        # (a) Check scraper exit code (unless --skip-scrape).
        if not skip_scrape and exit_codes is not None:
            code = exit_codes.get(store)
            if code != 0:
                failures[store] = f"scraper exit code {code}"
                row_counts[store] = 0
                continue

        # (b) Check that data/<store>.json exists and parses.
        json_path = DATA_DIR / f"{store}.json"
        if not json_path.exists():
            failures[store] = f"{json_path.name} not found"
            row_counts[store] = 0
            continue

        try:
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            failures[store] = f"JSON parse error: {e}"
            row_counts[store] = 0
            continue

        count = len(data) if isinstance(data, list) else 0
        row_counts[store] = count

        # (c) Row count must be at least 50% of expected.
        # This sanity threshold catches scrapers that "succeed" (exit 0)
        # but return a near-empty result due to a site redesign or
        # anti-bot block returning HTML instead of product data.
        threshold = expected // 2
        if count < threshold:
            failures[store] = (
                f"row count {count} < 50% of expected {expected} "
                f"(threshold {threshold})"
            )
            continue

        passed.append(store)

    return passed, failures, row_counts


# ---------------------------------------------------------------------------
# Phase 3: Ingest — bulk-load validated stores into the database
# ---------------------------------------------------------------------------

def run_ingest(passed_stores: list[str]) -> bool:
    """Run ingest.py for the passed stores with --mark-disappeared.

    Returns True on success, False on failure.  Output is streamed live
    to stdout so CI can see progress.

    subprocess.run(timeout=...) kills the child on expiry.  A killed
    ingest is upsert-idempotent and reconciles on the next successful
    night.  Returning False feeds the existing FAILED/exit-2 flow
    unchanged.
    """
    print(f"  Stage timeout: {INGEST_TIMEOUT_S}s")
    cmd = [sys.executable, "ingest.py"] + passed_stores + ["--mark-disappeared"]
    try:
        result = subprocess.run(cmd, cwd=str(REPO_ROOT), timeout=INGEST_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        print(f"\n[INGEST] TIMEOUT after {INGEST_TIMEOUT_S}s — process killed.")
        return False
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Phase 4: Writer — build/update canonical products
# ---------------------------------------------------------------------------

def run_writer(dry_run: bool) -> bool:
    """Run the matching writer.  Under --dry-run the writer rolls back.

    Returns True on success, False on failure.  Output is streamed live.

    subprocess.run(timeout=...) kills the child on expiry.  A killed
    writer drops its connection and PostgreSQL rolls back its single
    open transaction server-side, so no partial writes are possible.
    Returning False feeds the existing FAILED/exit-2 flow unchanged.
    """
    print(f"  Stage timeout: {WRITER_TIMEOUT_S}s")
    cmd = [sys.executable, "-m", "matching.writer"]
    if not dry_run:
        cmd.append("--write")
    try:
        result = subprocess.run(cmd, cwd=str(REPO_ROOT), timeout=WRITER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        print(f"\n[WRITER] TIMEOUT after {WRITER_TIMEOUT_S}s — process killed.")
        return False
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Phase 5: Revalidation — poke the web app's cache revalidation endpoint
# ---------------------------------------------------------------------------

def call_revalidate() -> bool | None:
    """POST to the revalidation endpoint to bust stale caches.

    Returns True on success, False on HTTP/network error, or None if the
    required env vars are not configured (which is NOT treated as a failure —
    local dev environments typically don't have the web app running).

    Reads REVALIDATE_URL (the full endpoint URL) and REVALIDATE_SECRET from
    the environment.  The request body/response contract is intentionally
    minimal so this function is trivial to adjust when the web route changes.
    """
    url = os.environ.get("REVALIDATE_URL")
    secret = os.environ.get("REVALIDATE_SECRET")

    if not url or not secret:
        print("  Revalidation skipped (env not configured).")
        return None

    req = urllib.request.Request(
        url,
        method="POST",
        headers={"Authorization": f"Bearer {secret}"},
        data=b"",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            print(f"  Revalidation response: HTTP {resp.status}")
            print(f"  Body: {body}")
            return resp.status == 200
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(f"  Revalidation failed: HTTP {e.code}")
        print(f"  Body: {body}")
        return False
    except (urllib.error.URLError, OSError) as e:
        print(f"  Revalidation failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Phase 6: Alerts — notify the web app to dispatch price-alert emails
# ---------------------------------------------------------------------------

def call_alerts_notify() -> bool | None:
    """POST to the alerts notification endpoint to trigger price-alert emails.

    Called after a successful writer run so that alert evaluation runs against
    freshly-ingested prices.  The web app's /api/alerts/notify route checks
    every active alert, compares current prices against thresholds, and queues
    emails for any that fire.

    Returns True on HTTP 200, False on any HTTP/network error, or None if the
    required env vars are not configured (which is NOT treated as a failure —
    local dev environments won't have the web app's alerts endpoint).

    Reads ALERTS_NOTIFY_URL (the full endpoint URL) and ALERTS_CRON_SECRET
    from the environment.  Timeout is 90s because the route may take up to
    ~60s when a large batch of alert emails is queued for sending.
    """
    url = os.environ.get("ALERTS_NOTIFY_URL")
    secret = os.environ.get("ALERTS_CRON_SECRET")

    if not url or not secret:
        print("  Alerts skipped (env not configured).")
        return None

    req = urllib.request.Request(
        url,
        method="POST",
        headers={"Authorization": f"Bearer {secret}"},
        data=b"",
    )

    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            print(f"  Alerts response: HTTP {resp.status}")
            print(f"  Body: {body}")
            return resp.status == 200
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(f"  Alerts failed: HTTP {e.code}")
        print(f"  Body: {body}")
        return False
    except (urllib.error.URLError, OSError) as e:
        print(f"  Alerts failed: {e}")
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Nightly pipeline orchestrator: scrape → ingest → match → revalidate → alerts.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Writer rolls back instead of committing; revalidation and alerts are skipped.",
    )
    parser.add_argument(
        "--skip-scrape",
        action="store_true",
        default=False,
        help="Skip the scraper phase and use existing data/*.json files.",
    )
    parser.add_argument(
        "--stores",
        nargs="+",
        default=None,
        help="Subset of stores to process (default: all six).",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv()
    args = parse_args()

    # Resolve which stores to run.
    valid_store_names = {s[0] for s in STORE_REGISTRY}
    if args.stores:
        for s in args.stores:
            if s not in valid_store_names:
                print(f"ERROR: Unknown store '{s}'. "
                      f"Valid: {', '.join(sorted(valid_store_names))}")
                sys.exit(2)
        selected = [entry for entry in STORE_REGISTRY if entry[0] in args.stores]
    else:
        selected = list(STORE_REGISTRY)

    print(f"Pipeline starting: {len(selected)} store(s), "
          f"dry_run={args.dry_run}, skip_scrape={args.skip_scrape}")
    pipeline_start = time.monotonic()

    # -- Phase 1: Scrape --
    scrape_exit_codes: dict[str, int] | None = None
    scrape_elapsed = 0.0

    if args.skip_scrape:
        print("\n[SCRAPE] Skipped (--skip-scrape).")
    else:
        print("\n[SCRAPE] Launching scrapers...")
        t0 = time.monotonic()
        scrape_exit_codes = run_scrapers(selected)
        scrape_elapsed = time.monotonic() - t0
        print(f"\n[SCRAPE] Done in {scrape_elapsed:.1f}s.")

    # -- Phase 2: Validate --
    print("\n[VALIDATE] Checking store outputs...")
    t0 = time.monotonic()
    passed_stores, failures, row_counts = validate_stores(
        selected, scrape_exit_codes, args.skip_scrape,
    )
    validate_elapsed = time.monotonic() - t0

    for store in passed_stores:
        entry = next(e for e in selected if e[0] == store)
        print(f"  PASS  {store}: {row_counts[store]} rows "
              f"(expected ~{entry[2]})")
    for store, reason in failures.items():
        print(f"  FAIL  {store}: {reason}")

    print(f"\n[VALIDATE] {len(passed_stores)} passed, "
          f"{len(failures)} failed, {validate_elapsed:.1f}s.")

    # -- Phase 3: Ingest --
    ingest_ok = False
    ingest_elapsed = 0.0

    if not passed_stores:
        print("\n[INGEST] Skipped — no stores passed validation.")
    else:
        print(f"\n[INGEST] Ingesting {len(passed_stores)} store(s): "
              f"{', '.join(passed_stores)}...")
        t0 = time.monotonic()
        ingest_ok = run_ingest(passed_stores)
        ingest_elapsed = time.monotonic() - t0
        status = "OK" if ingest_ok else "FAILED"
        print(f"\n[INGEST] {status} in {ingest_elapsed:.1f}s.")

    # -- Phase 4: Writer --
    writer_ok = False
    writer_elapsed = 0.0

    if not ingest_ok:
        print("\n[WRITER] Skipped — ingest did not succeed.")
    else:
        label = "dry-run" if args.dry_run else "write"
        print(f"\n[WRITER] Running matching writer ({label})...")
        t0 = time.monotonic()
        writer_ok = run_writer(args.dry_run)
        writer_elapsed = time.monotonic() - t0
        status = "OK" if writer_ok else "FAILED"
        print(f"\n[WRITER] {status} in {writer_elapsed:.1f}s.")

    # -- Phase 5: Revalidation --
    # Only called when ingest+writer succeeded AND at least one store passed.
    # Under --dry-run, revalidation is skipped because the writer rolled back
    # and there are no new product rows to revalidate.
    revalidate_result: bool | None = None
    revalidate_elapsed = 0.0

    if not writer_ok:
        print("\n[REVALIDATE] Skipped — writer did not succeed.")
    elif args.dry_run:
        print("\n[REVALIDATE] Skipped (dry-run).")
    else:
        print("\n[REVALIDATE] Calling revalidation endpoint...")
        t0 = time.monotonic()
        revalidate_result = call_revalidate()
        revalidate_elapsed = time.monotonic() - t0
        print(f"  {revalidate_elapsed:.1f}s elapsed.")

    # -- Phase 6: Alerts --
    # Only called when the writer succeeded and this is not a dry-run.
    # Same gating as revalidation: if the writer failed or rolled back,
    # there are no fresh prices to evaluate alerts against.
    alerts_result: bool | None = None
    alerts_elapsed = 0.0

    if not writer_ok:
        print("\n[ALERTS] Skipped — writer did not succeed.")
    elif args.dry_run:
        print("\n[ALERTS] Skipped (dry-run).")
    else:
        print("\n[ALERTS] Calling alerts notification endpoint...")
        t0 = time.monotonic()
        alerts_result = call_alerts_notify()
        alerts_elapsed = time.monotonic() - t0
        print(f"  {alerts_elapsed:.1f}s elapsed.")

    # -- Final summary --
    total_elapsed = time.monotonic() - pipeline_start

    print(f"\n{'=' * 60}")
    print("  FINAL SUMMARY")
    print(f"{'=' * 60}")

    # Per-store results.
    for store, _script, expected in selected:
        count = row_counts.get(store, 0)
        if store in passed_stores:
            print(f"  {store:<14s}  PASS  {count:>6d} rows  (expected ~{expected})")
        else:
            reason = failures.get(store, "unknown")
            print(f"  {store:<14s}  FAIL  {count:>6d} rows  (expected ~{expected})  "
                  f"— {reason}")

    # Phase statuses.
    print()
    if not args.skip_scrape:
        print(f"  Scrape:       {scrape_elapsed:.1f}s")
    else:
        print(f"  Scrape:       skipped")
    print(f"  Validate:     {validate_elapsed:.1f}s")
    print(f"  Ingest:       {'OK' if ingest_ok else 'FAILED / skipped'}"
          f"  ({ingest_elapsed:.1f}s)")
    print(f"  Writer:       {'OK' if writer_ok else 'FAILED / skipped'}"
          f"  ({writer_elapsed:.1f}s)")

    if args.dry_run:
        print(f"  Revalidate:   skipped (dry-run)")
    elif revalidate_result is None:
        print(f"  Revalidate:   skipped (env not configured)")
    elif revalidate_result:
        print(f"  Revalidate:   OK  ({revalidate_elapsed:.1f}s)")
    else:
        print(f"  Revalidate:   FAILED  ({revalidate_elapsed:.1f}s)")

    if args.dry_run:
        print(f"  Alerts:       skipped (dry-run)")
    elif alerts_result is None:
        print(f"  Alerts:       skipped (env not configured)")
    elif alerts_result:
        print(f"  Alerts:       OK  ({alerts_elapsed:.1f}s)")
    else:
        print(f"  Alerts:       FAILED  ({alerts_elapsed:.1f}s)")

    print(f"\n  Total elapsed: {total_elapsed:.1f}s")
    print(f"{'=' * 60}")

    # -- Exit code --
    # 0 = all stores passed, all phases succeeded.
    # 1 = partial success: some stores failed, OR revalidation failed,
    #     OR alerts failed; data was written, but CI must alert so
    #     someone investigates.
    #     revalidate_result=None / alerts_result=None (env not configured)
    #     are NOT failures.
    # 2 = pipeline failure: ingest or writer errored out.
    if not ingest_ok or not writer_ok:
        sys.exit(2)
    elif failures or revalidate_result is False or alerts_result is False:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
