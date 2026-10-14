"""DVC Sales (dvcsales.com) - Mark & Lori Webb, Winter Garden FL.

/dvc-resale-listings is one server-rendered page (~3 MB) with every contract
as a <tr class="listing-row" data-*> row, so a full fetch is 1 request.

The same table also embeds ~700 hidden "external-row" rows that DVC Sales
aggregates from OTHER brokers (DVC Resale Market, Fidelity, DVC Resale
Experts, DVC Shop, ...). Those are skipped here: they duplicate the other
adapters' inventory. Set INCLUDE_EXTERNAL = True to keep them (source_broker
goes into extra).

robots.txt disallows /api/ (used by the page's JS), so we stay on HTML.

Index has: code, resort, use year, contract points, 3-year availability
(header "2026 | 2027 | 2028" = use-year START years), $/pt, days on market.
Detail adds: banked/borrowed/holding per use year, dues per point, deed
expiry, listed date, schedule of fees (dues reimbursement => dues terms),
US seller, fixed-week notes. Only live inventory is published.
"""
from __future__ import annotations

import html as H
import logging
import re
from datetime import date, datetime, timedelta

from ..model import blank, classify_status, money, to_int
from ..resorts import normalize_resort, month_num

log = logging.getLogger(__name__)

NAME = "dvcsales"
BASE = "https://dvcsales.com"
INDEX = BASE + "/dvc-resale-listings"
INCLUDE_EXTERNAL = False

_ROW = re.compile(r'<tr class="listing-row( external-row)?"([^>]*)>(.*?)</tr>', re.S)
_ATTR = re.compile(r'data-([a-z0-9\-]+)="([^"]*)"')
# Broker-specific abbreviations the shared normalizer doesn't know.
_ALIASES = {"grand ca": "Grand Californian"}

_HDR_YEARS = re.compile(r"(20\d\d)\s*\|\s*(20\d\d)\s*\|\s*(20\d\d)\s*</th>")


def _text(page: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
    t = H.unescape(re.sub(r"<[^>]+>", "\n", t))
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t)


def fetch_listings(session) -> list[dict]:
    r = session.get(INDEX)
    page = r.text
    if r.status_code in (401, 403) or ("listing-row" not in page and re.search(r"captcha|cf-chl|access denied", page, re.I)):
        log.warning("%s: blocked or unexpected index (HTTP %s)", NAME, r.status_code)
        return []
    r.raise_for_status()
    m = _HDR_YEARS.search(page)
    if m:
        years = [int(x) for x in m.groups()]
    else:
        y = date.today().year
        years = [y, y + 1, y + 2]

    out, seen = [], set()
    for ext, attrs, body in _ROW.findall(page):
        if ext and not INCLUDE_EXTERNAL:
            continue
        a = {k: H.unescape(v) for k, v in _ATTR.findall(attrs)}
        href = re.search(r'href="([^"]+)"[^>]*class="view-btn', body)
        if ext:
            lid = "ext_" + a.get("extid", a.get("code", ""))
            url = a.get("link") or (href.group(1) if href else None)
        else:
            # numeric id from the SEO URL /dvc-resale/<resort>/<n>-points/<id>
            m2 = re.search(r"/(\d+)$", href.group(1)) if href else None
            lid = a.get("code") or (m2.group(1) if m2 else None)
            url = BASE + href.group(1) if href else None
        if not lid or lid in seen:
            continue
        seen.add(lid)
        uy = month_num(a.get("useyear"))
        pts = to_int(a.get("contract"))
        ppp = money(a.get("ppp"))
        resort_raw = a.get("resortname") or a.get("resort", "")
        d = blank(
            source=NAME,
            listing_id=lid,
            url=url,
            resort_raw=resort_raw,
            resort=normalize_resort(_ALIASES.get(resort_raw.strip().lower(), resort_raw)),
            points=pts,
            price_per_point=ppp,
            price=round(pts * ppp, 2) if pts and ppp else None,
            use_year=uy,
            status_raw="Live",
            availability=classify_status(None),
        )
        yr = [to_int(a.get(f"yr{i}")) for i in (1, 2, 3)]
        if uy and all(v is not None for v in yr):
            d["points_available"] = [
                {"uy": f"{y}-{uy:02d}", "points": n, "banked": 0} for y, n in zip(years, yr)
            ]
            d["availability_text"] = " | ".join(f"{y}: {n}" for y, n in zip(years, yr))
            d["extra"]["availability_from"] = "index"
        days = to_int(a.get("days"))
        if days is not None:
            d["extra"]["days_on_market"] = days
            d["date_listed"] = (date.today() - timedelta(days=days)).isoformat()
            d["extra"]["date_listed_from"] = "index_days"
        if href:
            m3 = re.search(r"/(\d+)$", href.group(1))
            if m3:
                d["extra"]["internal_id"] = m3.group(1)
        if a.get("score"):
            d["extra"]["value_score"] = to_int(a["score"])
        if "(57)" in resort_raw:
            d["extra"]["note"] = "Old Key West extended (2057) contract"
        if ext:
            d["extra"]["source_broker"] = a.get("brokername") or a.get("broker")
        out.append(d)
    return out


_AVAIL_ROW = re.compile(r"\n(20\d\d)\n(\d+)\n(\d+)\n(\d+)\n\s*(\d+)\n(\d+)")


def fetch_detail(session, d: dict) -> dict:
    if d["listing_id"].startswith("ext_"):
        return d  # external broker page; leave to that broker's adapter
    t = _text(session.text(d["url"]))

    # Available Points table: Year / Banked / Borrow / Holding / Available / Bankable
    i = t.find("Available Points\nYear\nBanked")
    if i >= 0 and d.get("use_year"):
        seg = t[i:i + 600]
        rows = _AVAIL_ROW.findall(seg)
        if rows:
            uy = d["use_year"]
            d["points_available"] = [
                {"uy": f"{y}-{uy:02d}", "points": int(av), "banked": int(bk) + int(bo)}
                for y, bk, bo, hold, av, bankable in rows
            ]
            d["extra"]["availability_table"] = [
                {"year": int(y), "banked": int(bk), "borrowed": int(bo), "holding": int(h),
                 "available": int(av), "bankable": int(ba)} for y, bk, bo, h, av, ba in rows
            ]
            d["extra"]["availability_from"] = "detail"
        j = t.find("There are", i)
        if j > 0:
            k = t.find("each year.", j)
            d["availability_text"] = re.sub(r"\s+", " ", t[j:k + 10 if k > 0 else j + 400]).strip()

    m = re.search(r"\nDeed Expiration\n(20\d\d)", t) or re.search(r"\nDeed Expiry\n(20\d\d)", t)
    if m:
        d["expiration"] = int(m.group(1))
    m = re.search(r"\n(20\d\d) Dues/Pt\n\$([\d.]+)", t)
    if m:
        d["dues_per_point"] = float(m.group(2))
        d["extra"]["dues_year"] = int(m.group(1))
    else:
        m = re.search(r"\nAnnual Dues\n\$([\d.]+)\s*\n/pt", t)
        if m:
            d["dues_per_point"] = float(m.group(1))
    m = re.search(r"listed ([A-Z][a-z]{2} \d{1,2}, 20\d\d)\.", t)
    if m:
        d["date_listed"] = datetime.strptime(m.group(1), "%b %d, %Y").date().isoformat()
        d["extra"]["date_listed_from"] = "detail"

    fees = t[t.find("SCHEDULE OF FEES"):]
    fees = fees[:fees.find("Make Offer")] if "Make Offer" in fees else fees[:800]
    reimb = re.search(r"Dues Reimbursement \((20\d\d)\)\n([^\n]+)\n\$([\d,.]+)", fees)
    if reimb:
        d["dues_terms"] = f"Buyer reimburses seller {reimb.group(1)} dues: {reimb.group(2).strip()} = ${reimb.group(3)}"
        d["extra"]["dues_reimbursement"] = money(reimb.group(3))
    elif "SCHEDULE OF FEES" in t:
        d["dues_terms"] = "No dues reimbursement listed in buyer's schedule of fees (seller pays current-year dues)"
    m = re.search(r"Est\. Closing Costs\n\$([\d,.]+)", fees)
    if m:
        d["extra"]["closing_cost"] = money(m.group(1))
    m = re.search(r"Club Activation Fee\n\$([\d,.]+)", fees)
    if m:
        d["extra"]["caf"] = money(m.group(1))
    m = re.search(r"Total Due at Closing\n\$([\d,.]+)", fees)
    if m:
        d["extra"]["total_due_at_closing"] = money(m.group(1))
    m = re.search(r"Closing Date:\n([A-Z][a-z]+ \d{1,2}, 20\d\d)", fees)
    if m:
        d["extra"]["est_closing_date"] = datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
    m = re.search(r"US Seller:?\n\s*(Yes|No)", t)
    if m:
        d["extra"]["us_seller"] = m.group(1) == "Yes"
    m = re.search(r"Fixed Week Ownership · ([^\n]+)", t)
    if m:
        d["extra"]["fixed_week"] = m.group(1).strip()

    # Status line: "Live | listed 3 days ago, still available"
    m = re.search(r"\n(Live|Pending|Under Contract|Sold|In ROFR)[^\n]*\|[^\n]*", t)
    if m:
        d["status_raw"] = m.group(0).strip()
        d["availability"] = classify_status(m.group(1) if m.group(1) != "Live" else None)
    if d.get("resort") == "aulani" and re.search(r"subsidi[sz]ed\s+(annual\s+)?dues", t, re.I):
        d["subsidized_dues"] = True
    elif d.get("resort") == "aulani":
        d["subsidized_dues"] = False
    d["detail_fetched"] = True
    return d
