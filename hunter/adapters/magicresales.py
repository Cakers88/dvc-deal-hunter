"""Magic Resales (magicresales.com; dvcbroker.com redirects here) -- DVC iBuyer.

/available-contracts/ is one server-rendered table (~1,200 rows, ~2 MB) that
mixes Magic Resales' OWN contracts (data-mr="1") with rows scraped from other
brokers (DVC Resale Market, DVC Shop, Fidelity, VCL as "Resales DVC", ...,
pending included).  We only return the OWN rows by default: everything else
is already covered by the direct broker adapters, and Magic Resales' own
inventory is NOT on dvcmarket.com.  Set INCLUDE_THIRD_PARTY = True to also
get the others (with extra["broker"] / extra["broker_url"] for de-dup).

robots.txt: Crawl-delay 10.  One request per run; there is no detail page
(the row expands in place with dues / closing terms, which we parse).
"""
from __future__ import annotations

import html as H
import logging
import re

from ..model import blank, classify_status, money, to_int
from ..resorts import normalize_resort, month_num

log = logging.getLogger(__name__)

NAME = "magicresales"
BASE = "https://magicresales.com"
INDEX = BASE + "/available-contracts/"
INCLUDE_THIRD_PARTY = False

_ROW = re.compile(r'<tr class="mrdm-row[^"]*"(.*?)>(.*?)</tr>\s*(?:<tr class="mrdm-cardrow"[^>]*>(.*?)</tr>)?', re.S)
_ATTR = re.compile(r'data-([a-z-]+)="([^"]*)"')


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _year_labels(page: str) -> list[int] | None:
    m = re.search(r'mrdm-avail-years">\s*(\d\d)\s*\|\s*(\d\d)\s*\|\s*(\d\d)', page)
    return [2000 + int(x) for x in m.groups()] if m else None


def fetch_listings(session) -> list[dict]:
    r = session.get(INDEX)
    if r.status_code != 200 or "mrdm-row" not in r.text:
        log.warning("magicresales: index returned %s without listing table; skipping", r.status_code)
        return []
    page = r.text
    years = _year_labels(page)
    out = []
    for attrs, body, card in _ROW.findall(page):
        a = {k: H.unescape(v) for k, v in _ATTR.findall(attrs)}
        own = a.get("mr") == "1"
        if not own and not INCLUDE_THIRD_PARTY:
            continue
        uy = month_num(a.get("uy"))
        status = a.get("status") or ""
        facts = dict(re.findall(r"<div><span>([^<]+)</span><span>([^<]*)</span></div>", card or ""))
        lid = facts.get("Listing")
        link = re.search(r'class="mrdm-view" href="([^"]+)"', body)
        d = blank(
            source=NAME,
            listing_id=lid or f"{a.get('broker')}|{link.group(1) if link else ''}",
            url=(f"{INDEX}#listing-{lid}" if own else (link.group(1) if link else INDEX)),
            resort_raw=a.get("resort"),
            resort=normalize_resort(a.get("resort")),
            points=to_int(a.get("points")),
            price=money(a.get("price")),
            price_per_point=money(a.get("ppp")),
            use_year=uy,
            status_raw=status or None,
            availability=classify_status(None if status.lower() in ("available", "new") else status),
            expiration=to_int(a.get("deed")),
        )
        m = re.search(r'class="mrdm-pts">([^<]*)<', body)
        if m and uy and years:
            vals = [v.strip() for v in m.group(1).split("|")]
            contract = d["points"] or 0
            d["points_available"] = [
                {"uy": f"{y}-{uy:02d}", "points": int(v), "banked": max(0, int(v) - contract) if contract else 0}
                for y, v in zip(years, vals) if v.isdigit()          # "?" = unknown
            ]
            d["availability_text"] = m.group(1).strip()
        dues = money(facts.get("Annual dues"))
        if dues is not None:
            d["extra"]["yearly_dues"] = dues
            if d["points"]:
                d["dues_per_point"] = round(dues / d["points"], 2)
        if facts.get("Closing"):
            d["dues_terms"] = H.unescape(facts["Closing"])
        desc = _clean(re.search(r'mrdm-card-desc">(.*?)</div>', card or "", re.S).group(1)) if card and "mrdm-card-desc" in card else ""
        if desc:
            d["extra"]["notes"] = desc
        d["extra"]["broker"] = a.get("broker")
        d["extra"]["own_inventory"] = own
        if not own and link:
            d["extra"]["broker_url"] = link.group(1)
        d["extra"]["availability_from"] = "index"
        d["detail_fetched"] = True          # nothing more to fetch
        out.append(d)
    return out


def fetch_detail(session, d: dict) -> dict:
    return d
