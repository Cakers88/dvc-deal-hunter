"""Fidelity Real Estate (fidelityrealestate.com) -- intentionally disabled.

Investigated 2026-10-13:
- The DVC brand page (/brand/disney-vacation-club/) renders no listings server-side;
  they are loaded client-side via admin-ajax.php (nonce-protected "nexus" search).
- The WordPress REST API (/wp-json/wp/v2/properties) returns a Cloudflare 403 page:
  "API Access Restricted ... Scraping is prohibited and violates our Terms."
- robots.txt itself returns 403.
- Terms & Conditions section 1.3 ("No Scraping or Automated Data Collection")
  explicitly forbids robots/scrapers/crawlers collecting listings, pricing,
  availability or market data.

Per the adapter rules (never bypass bot protection; if a site blocks us, return []
and log a warning), this adapter makes no requests. Re-enable only with written
permission / API access from Fidelity (they say to contact them for API access),
or use their own "saved search" email alerts manually.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

NAME = "fidelity"
ENABLED = False


def fetch_listings(session) -> list[dict]:
    log.warning("fidelity: disabled -- site Terms prohibit automated collection and its API "
                "blocks scrapers; returning no listings")
    return []


def fetch_detail(session, listing: dict) -> dict:
    return listing
