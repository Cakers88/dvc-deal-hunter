"""DVC Shop (dvcshop.com -> resales live on resales.dvcshop.com).

dvcshop.com 301s to the blog; the resale broker is resales.dvcshop.com (WordPress).
Its theme registers a custom REST route that returns EVERY listing in one JSON
response (no pagination; per_page is ignored):

    https://resales.dvcshop.com/wp-json/wp/v2/resalelistings

Each record: listing_id, resort_name/resort_id/resort_abbr, status, use_month,
points, price, price_per_point, banked_points (free-text availability), maintenance
(dues/pt, subsidized rate for subsidized Aulani contracts), expires (deed year),
post_date. That is a superset of the detail page (detail pages show the same
fields plus contact forms), so a full fetch is 1 request and fetch_detail only
re-reads the same record from the HTML page when called.

(market.dvcshop.com is a stickers store behind a Cloudflare challenge; unrelated.)
"""
from __future__ import annotations

import html as H
import logging
import re
from datetime import date

from ..model import blank, classify_status, money, to_int
from ..resorts import normalize_resort, month_num

log = logging.getLogger(__name__)

NAME = "dvcshop"
BASE = "https://resales.dvcshop.com"
API = BASE + "/wp-json/wp/v2/resalelistings"
INDEX_HTML = BASE + "/listings/"


def _current_uy_year(uy: int, today: date | None = None) -> int:
    today = today or date.today()
    return today.year if today.month >= uy else today.year - 1


_NUM = r"(\d[\d,]*)"
_PTS = r"(?:points?|pts)?"


def parse_shop_availability(text: str, uy: int | None, annual: int | None,
                            today: date | None = None) -> list[dict]:
    """Normalize DVC Shop's free text, e.g.

    "46 points banked for 2025, 210 points for 2026 and beyond"
    "156 points for 2026 (78 points are banked from 2025), 78 points for 2027 and beyond."
    "0 points for 2027, 136 points borrowed from 2028 into 2027, 264 points for 2028, 400 points for 2029 and beyond"

    Convention (inferred from listings dated after the banking deadline): "N points
    banked for/from Y" = Y's points were banked forward into Y+1, so they show up in
    the Y+1 bucket (marked banked) and Y itself is 0. "M points for Y and beyond"
    is expanded to Y..(current UY + 2) with M points each. "M points for Y" plus
    "N banked for Y-1" are summed (Y bucket = M + N, banked = N); an explicit
    parenthetical "(N banked ...)" is treated as already included in M. Use years
    from the current UY up to the first year mentioned get 0-point buckets.
    """
    if not text or not uy:
        return []
    t = H.unescape(text).replace("\xa0", " ")
    # drop parentheticals but remember explicit "(N banked ... Y)" inclusions
    inc_banked: dict[int, int] = {}
    for inner in re.findall(r"\(([^)]*)\)", t):
        m = re.search(_NUM + r"\s*(?:banked\s*)?" + _PTS + r"[^)]*?banked[^)]*?(20\d\d)", inner, re.I)
        if m:
            inc_banked[int(m.group(2)) + 1] = int(m.group(1).replace(",", ""))
    t = re.sub(r"\([^)]*\)", " ", t)

    buckets: dict[int, dict] = {}

    def put(y: int, pts: int, banked: int = 0, add: bool = False):
        b = buckets.setdefault(y, {"uy": f"{y}-{uy:02d}", "points": 0, "banked": 0})
        if add:
            b["points"] += pts
            b["banked"] += banked
        else:
            b["points"] = max(b["points"], pts) if b["points"] else pts
            b["banked"] = max(b["banked"], banked)

    pending_banked: dict[int, int] = {}
    # borrowed: "136 points borrowed from 2028 into 2027"
    for n, frm, into in re.findall(_NUM + r"\s*" + _PTS + r"\s*borrowed\s*from\s*(20\d\d)\s*into\s*(20\d\d)", t, re.I):
        pending_banked[int(into)] = pending_banked.get(int(into), 0) + int(n.replace(",", ""))
    t = re.sub(_NUM + r"\s*" + _PTS + r"\s*borrowed\s*from\s*20\d\d\s*into\s*20\d\d", " ", t, flags=re.I)
    # banked: "46 points banked for 2025" / "200 points banked from 2025"
    for n, y in re.findall(_NUM + r"\s*" + _PTS + r"\s*banked\s*(?:for|from|in)\s*(20\d\d)", t, re.I):
        y = int(y)
        pending_banked[y + 1] = pending_banked.get(y + 1, 0) + int(n.replace(",", ""))
        put(y, 0)
    t = re.sub(_NUM + r"\s*" + _PTS + r"\s*banked\s*(?:for|from|in)\s*20\d\d", " ", t, flags=re.I)

    # "N points for Y [and beyond]" / "N available for Y and beyond" / "N for Y"
    cur = _current_uy_year(uy, today)
    for n, y, beyond in re.findall(
        _NUM + r"\s*(?:points?|pts|available)?\s*(?:available\s*)?for\s*(20\d\d)(\s*and\s*beyond)?", t, re.I
    ):
        n, y = int(n.replace(",", "")), int(y)
        put(y, n)
        if beyond:
            for yy in range(y + 1, max(y + 1, cur + 2) + 1):  # at least Y+1, up to current+2
                if yy not in buckets:
                    put(yy, n)

    # fold banked/borrowed points into their destination bucket
    for y, n in pending_banked.items():
        if y in inc_banked:
            continue
        if y in buckets:
            b = buckets[y]
            # "0 points for 2027, 136 borrowed into 2027" / "46 banked for 2025, 210 for 2026"
            b["points"] += n
            b["banked"] += n
        else:
            put(y, n, n)
    for y, n in inc_banked.items():
        if y in buckets:
            buckets[y]["banked"] = min(n, buckets[y]["points"])
    # years from the current UY up to the first one mentioned have nothing for the buyer
    if buckets:
        for yy in range(cur, min(buckets)):
            put(yy, 0)
    return [buckets[k] for k in sorted(buckets)]


def _normalize(x: dict, today: date | None = None) -> dict:
    status = (x.get("status") or "").strip()
    uy = month_num(x.get("use_month"))
    avail = (x.get("banked_points") or "").strip()
    resort_raw = H.unescape(x.get("resort_name") or x.get("title") or "")
    lid = (x.get("listing_id") or x.get("internal_listing_id") or str(x.get("id"))).strip()
    rid = x.get("resort_id") or ""
    d = blank(
        source=NAME,
        listing_id=lid,
        url=f"{BASE}/listings/{rid}-{lid.lower()}/" if rid else None,
        resort_raw=resort_raw,
        resort=normalize_resort(resort_raw) or normalize_resort(rid.replace("-", " ")),
        points=to_int(x.get("points")),
        price=money(x.get("price")),
        price_per_point=money(x.get("price_per_point")),
        use_year=uy,
        status_raw=status,
        availability=classify_status(status),
        availability_text=avail or None,
        dues_per_point=money(x.get("maintenance")),
    )
    if not d["price"]:  # source sometimes has price ".00"
        d["price"] = None
        if d["price_per_point"] and d["points"]:
            d["price"] = d["price_per_point"] * d["points"]
            d["extra"]["price_derived"] = True
    if d["price_per_point"] is None and d["price"] and d["points"]:
        d["price_per_point"] = round(d["price"] / d["points"], 2)
    exp = to_int(x.get("expires"))
    d["expiration"] = exp if exp and exp > 2000 else None
    pd = (x.get("post_date") or "")[:10]
    if re.fullmatch(r"\d{4}-\d\d-\d\d", pd):
        d["date_listed"] = pd
    subsidized = bool(re.search(r"subsidi[sz]ed", status + " " + avail, re.I))
    d["subsidized_dues"] = True if subsidized else (False if d["resort"] == "aulani" else None)
    d["points_available"] = parse_shop_availability(avail, uy, d["points"], today)
    ex = d["extra"]
    if d["points_available"]:
        ex["availability_from"] = "index"
    ex["post_id"] = x.get("id")
    ex["modified"] = x.get("last_modified_date")
    ex["resort_abbr"] = x.get("resort_abbr")
    m = re.search(r"can\s*close\s*(?:after|on|by)?\s*([\d/]+\d)", avail, re.I)
    if m:
        ex["can_close_after"] = m.group(1)
    if re.search(r"low\s*but\s*firm", status + " " + avail, re.I):
        ex["low_but_firm"] = True
    m = re.search(r"(best\s*(?:&|and)\s*(?:highest|final)\s*offer[^*]*)", avail, re.I)
    if m:
        ex["offer_deadline"] = m.group(1).strip()
    m = re.search(r"(\d+)\s*FREE\s*POINTS", avail, re.I)
    if m:
        ex["free_points"] = int(m.group(1))
    if subsidized:
        d["dues_terms"] = f"Subsidized dues ${d['dues_per_point']}/pt" if d["dues_per_point"] else "Subsidized dues"
    return d


def fetch_listings(session) -> list[dict]:
    r = session.get(API)
    ctype = r.headers.get("Content-Type", "")
    if r.status_code != 200 or "json" not in ctype:
        log.warning("dvcshop: %s -> HTTP %s (%s); blocked or API changed, returning []",
                    API, r.status_code, ctype)
        return []
    data = r.json()
    if not isinstance(data, list):
        log.warning("dvcshop: unexpected API payload %r", str(data)[:200])
        return []
    return [_normalize(x) for x in data if isinstance(x, dict)]


def _text(page: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
    t = H.unescape(re.sub(r"<[^>]+>", "\n", t)).replace("\xa0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t)


def fetch_detail(session, d: dict) -> dict:
    """The API already carries every detail-page field. This re-reads the listing
    page (1 request) to confirm status / availability / dues / expiration."""
    if not d.get("url"):
        return d
    r = session.get(d["url"])
    if r.status_code != 200:
        log.warning("dvcshop: detail %s -> HTTP %s", d["url"], r.status_code)
        return d
    t = _text(r.text)
    i = t.find("\nListing ID#:\n")
    if i >= 0:
        t = t[i:]

    def field(label):
        m = re.search(r"\n" + re.escape(label) + r"\n\s*([^\n]+)", t)
        return m.group(1).strip() if m else None

    v = field("Maintenance:")
    if v and money(v):
        d["dues_per_point"] = money(v)
    v = field("Expires:")
    if v and to_int(v):
        d["expiration"] = to_int(v)
    v = field("Status:")
    if v:
        d["status_raw"] = v
        d["availability"] = classify_status(v)
    v = field("Points Available:")
    if v and v != d.get("availability_text"):
        d["availability_text"] = v
        pa = parse_shop_availability(v, d.get("use_year"), d.get("points"))
        if pa:
            d["points_available"] = pa
    v = field("Listing Price:")
    if v and money(v):
        d["price"] = money(v)
    if re.search(r"subsidi[sz]ed", (d.get("status_raw") or "") + (d.get("availability_text") or ""), re.I):
        d["subsidized_dues"] = True
    d["extra"]["availability_from"] = "detail"
    d["detail_fetched"] = True
    return d
