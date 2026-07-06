"""Reusable proxy-pool helper — builds proxy URLs from environment variables.

Reads residential/ISP proxy configuration from the environment and returns
a list of proxy URLs that httpx clients can use.  Designed so any scraper
can opt in by calling build_proxy_urls() — currently only stephanis uses
proxies (Cloudflare IP-based challenge), but other stores can adopt the
same helper if they ever need residential IPs.

Environment variables (all four required, else no proxies):
  SCRAPER_PROXY_HOST  — proxy hostname (e.g. isp.decodo.com)
  SCRAPER_PROXY_USER  — authentication username
  SCRAPER_PROXY_PASS  — authentication password
  SCRAPER_PROXY_PORTS — comma-separated port list (e.g. 10001,10002,10003)

IMPORTANT: proxy URLs contain credentials.  This module never logs, prints,
or includes the URLs in any error message.  Only the count of configured
endpoints is logged.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def build_proxy_urls() -> list[str]:
    """Build a list of proxy URLs from environment variables.

    Each URL has the form http://USER:PASS@HOST:PORT, with one entry per
    port listed in SCRAPER_PROXY_PORTS.

    Returns an empty list if any of the four required environment variables
    is missing or empty.  An empty list means "no proxy available — run
    direct", which is the normal local-development path.
    """
    host = os.environ.get("SCRAPER_PROXY_HOST", "").strip()
    user = os.environ.get("SCRAPER_PROXY_USER", "").strip()
    password = os.environ.get("SCRAPER_PROXY_PASS", "").strip()
    ports_raw = os.environ.get("SCRAPER_PROXY_PORTS", "").strip()

    # All four must be present and non-empty for proxies to be usable.
    if not host or not user or not password or not ports_raw:
        return []

    # Parse the comma-separated port list, stripping whitespace around
    # each entry and ignoring empty segments (e.g. trailing commas).
    ports = [p.strip() for p in ports_raw.split(",") if p.strip()]
    if not ports:
        return []

    # Build one proxy URL per port.  The URL uses the standard HTTP proxy
    # userinfo format: http://user:pass@host:port
    urls = [f"http://{user}:{password}@{host}:{port}" for port in ports]

    # Log that proxies are configured (count only — NEVER log the URLs).
    log.info("Proxy pool: %d endpoint(s) configured.", len(urls))

    return urls
