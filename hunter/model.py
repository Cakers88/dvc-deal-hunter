"""Normalized listing schema shared by every adapter.

Every adapter returns a list of dicts with these keys (None when unknown):

    source            str   adapter name, e.g. "dvcresalemarket"
    listing_id        str   broker's id (unique within source)
    url               str   listing page
    resort_raw        str   resort text as shown by the broker
    resort            str   canonical key from hunter.resorts (None if unrecognized)
    points            int   points on contract (annual allotment)
    price             float asking price, USD
    price_per_point   float asking $/pt
    use_year          int   use-year month 1-12
    status_raw        str   broker's status text
    availability      str   "available" | "pending" | "sold" | "unknown"
    date_listed       str   ISO date if shown
    points_available  list  [{"uy": "2026-08", "points": 220, "banked": 0}] per use year bucket,
                            uy = YYYY-MM the use year STARTS. "banked" = how many of the
                            bucket's points are already banked/borrowed in (not re-bankable).
    availability_text str   raw availability text (kept for review)
    dues_terms        str   raw text of who pays which dues
    dues_per_point    float
    subsidized_dues   bool  Aulani subsidized-dues contracts
    expiration        int   deed expiration year
    detail_fetched    bool  whether a detail page was parsed
    extra             dict  anything else useful
"""
from __future__ import annotations

import re
from .resorts import month_num

FIELDS = [
    "source", "listing_id", "url", "resort_raw", "resort", "points", "price",
    "price_per_point", "use_year", "status_raw", "availability", "date_listed",
    "points_available", "availability_text", "dues_terms", "dues_per_point",
    "subsidized_dues", "expiration", "detail_fetched", "extra",
]


def blank(**kw) -> dict:
    d = {k: None for k in FIELDS}
    d["points_available"] = []
    d["extra"] = {}
    d["detail_fetched"] = False
    d.update(kw)
    return d


PENDING_WORDS = ("pending", "offer accepted", "under contract", "in rofr", "rofr", "contingent")
SOLD_WORDS = ("sold", "closed")


def classify_status(status: str | None) -> str:
    if not status:
        return "available"
    s = status.lower()
    if any(w in s for w in SOLD_WORDS):
        return "sold"
    if any(w in s for w in PENDING_WORDS):
        return "pending"
    return "available"


def money(text) -> float | None:
    if text is None:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", str(text).replace("$", ""))
    return float(m.group(0).replace(",", "")) if m else None


def to_int(text) -> int | None:
    v = money(text)
    return int(round(v)) if v is not None else None


_AVAIL_LINE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*(20\d\d)\s*[-–:]\s*([\d,]+)\s*(?:points|pts)",
    re.I,
)


def parse_availability_lines(text: str) -> list[dict]:
    """Parse 'August 2026 - 220 points' style lines (most brokers use some variant)."""
    out: dict[str, dict] = {}
    for mon, yr, pts in _AVAIL_LINE.findall(text or ""):
        m = month_num(mon)
        key = f"{yr}-{m:02d}"
        if key not in out:
            out[key] = {"uy": key, "points": int(pts.replace(",", "")), "banked": 0}
    # banked hints: "these 111 points were banked from Oct. 2025"
    for n, mon, yr in re.findall(
        r"(?<![\d,])(\d[\d,]*)\s*(?:points?|pts)?[^.\d]{0,40}?banked\s+(?:from|in)\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*(20\d\d)",
        text or "", re.I,
    ):
        m = month_num(mon)
        # banked from UY Y lands in UY Y+1
        key = f"{int(yr) + 1}-{m:02d}"
        if key in out:
            out[key]["banked"] = min(out[key]["points"], int(n.replace(",", "")))
    return sorted(out.values(), key=lambda x: x["uy"])
