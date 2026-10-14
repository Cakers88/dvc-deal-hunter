"""DVC Resale Market (dvcresalemarket.com).

One index page lists every contract with structured data-* attributes.
Detail pages give exact per-use-year availability and dues terms.
"""
from __future__ import annotations

import html as H
import re
from datetime import date

from ..model import blank, classify_status, money, to_int, parse_availability_lines
from ..resorts import normalize_resort, month_num

NAME = "dvcresalemarket"
INDEX = "https://www.dvcresalemarket.com/listings/"

_ROW = re.compile(r"<tr\b([^>]*\bdata-href=\"[^\"]+\"[^>]*)>(.*?)</tr>", re.S)
_ATTR = re.compile(r'data-([a-z\-]+)="([^"]*)"')


def current_uy_start(uy_month: int, today: date | None = None) -> tuple[int, int]:
    today = today or date.today()
    y = today.year if today.month >= uy_month else today.year - 1
    return y, uy_month


def fetch_listings(session) -> list[dict]:
    page = session.text(INDEX)
    out = []
    for attrs, body in _ROW.findall(page):
        a = dict(_ATTR.findall(attrs))
        if "id" not in a:
            continue
        status = H.unescape(a.get("statusname", "")).replace("​", "")
        uy = month_num(a.get("useyear"))
        d = blank(
            source=NAME,
            listing_id=a["id"],
            url=a.get("href"),
            resort_raw=H.unescape(a.get("resort", "")),
            points=to_int(a.get("points")),
            price=money(a.get("price")),
            price_per_point=money(a.get("priceperpoint")),
            use_year=uy,
            status_raw=status,
            availability=classify_status(status),
        )
        d["resort"] = normalize_resort(d["resort_raw"])
        d["subsidized_dues"] = "subsidized" in status.lower() or None
        # index availability: "220 | 220 | 220" = current UY, next, next+1
        m = re.search(r'data-column="point-availability"[^>]*>(.*?)</td>', body, re.S)
        if m and uy:
            nums = [int(x) for x in re.findall(r"\d+", re.sub(r"<[^>]+>", " ", m.group(1)))]
            y, mo = current_uy_start(uy)
            d["points_available"] = [
                {"uy": f"{y + i}-{mo:02d}", "points": n, "banked": 0} for i, n in enumerate(nums[:3])
            ]
            d["extra"]["availability_from"] = "index"
        out.append(d)
    return out


def _text(page: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
    t = H.unescape(re.sub(r"<[^>]+>", "\n", t)).replace("​", "")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t)


def _section(t: str, label: str, stops: list[str]) -> str | None:
    i = t.find("\n" + label + "\n")
    if i < 0:
        return None
    rest = t[i + len(label) + 2:]
    end = min([rest.find("\n" + s) for s in stops if rest.find("\n" + s) >= 0] or [len(rest)])
    return rest[:end].strip()


def fetch_detail(session, d: dict) -> dict:
    t = _text(session.text(d["url"]))
    anchor = t.find("\nPoints on Contract\n")
    if anchor > 0:
        t = t[anchor:]
    stops = ["Annual Dues", "Closing Costs", "Disney (CAF) Fee", "Total Cost", "Make an Offer",
             "Date Listed", "Aulani Closing", "Transient", "All Listings"]
    avail = _section(t, "Points Available", stops)
    if avail:
        d["availability_text"] = avail
        parsed = parse_availability_lines(avail)
        if parsed:
            d["points_available"] = parsed
            d["extra"]["availability_from"] = "detail"
    dues = _section(t, "Annual Dues", ["Closing Costs", "Disney (CAF) Fee", "Total Cost", "Prorated",
                                       "Aulani Closing", "Can Close After", "Make an Offer"])
    if dues:
        d["dues_terms"] = dues
    m = re.search(r"\nDues per Point\n\$?([\d.]+)", t)
    if m:
        d["dues_per_point"] = float(m.group(1))
    m = re.search(r"\nDeed Expiration\n(\d{4})", t)
    if m:
        d["expiration"] = int(m.group(1))
    m = re.search(r"\nDate Listed\n([A-Z][a-z]+ \d{1,2}, \d{4})", t)
    if m:
        from datetime import datetime
        d["date_listed"] = datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
    m = re.search(r"\nCan Close After\n([\d/]+)", t)
    if m:
        d["extra"]["can_close_after"] = m.group(1)
    m = re.search(r"\nClosing Costs\*?\n\$([\d,]+)", t)
    if m:
        d["extra"]["closing_cost"] = money(m.group(1))
    if re.search(r"subsidized", t, re.I) and d.get("resort") == "aulani":
        if re.search(r"(has|with) (the )?(Aulani )?(Disney )?Subsidized", t, re.I):
            d["subsidized_dues"] = True
    d["detail_fetched"] = True
    return d
