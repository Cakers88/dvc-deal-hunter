"""Vacation Club Life (vacationclublife.com) -- Garden Views Realty.

The /listings page is an empty shell; its own JS (theme dist/main.js) pulls the
inventory from a public, unauthenticated JSON endpoint (a Zoho CRM proxy on
AWS API Gateway):

    GET {API}/coql/listings?per_page=..&page=1&filter=..&fields=..   -> {"data": [...], "meta": {...}}
    GET {API}/listings/<id>?fields=..                                   -> one record (dues, closing, deed)

We send exactly the query the page sends (status Active/Pending/Reduced), with
per_page large enough to get everything in ONE request.

dvcmarket.com only carries VCL's Active/Reduced listings (~40%); the Pending
ones (more than half the inventory) are only visible here.  dvcmarket's
broker_url for VCL is https://vacationclublife.com/listings/<id>, so
listing_id here == dvcmarket extra["broker_listing_id"].

robots.txt asks for Crawl-delay: 10, so detail calls are spaced >= 10 s.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date

from ..model import blank, classify_status, money, to_int
from ..resorts import normalize_resort, month_num

log = logging.getLogger(__name__)

NAME = "vacationclublife"
SITE = "https://vacationclublife.com"
API = "https://1i58yxus93.execute-api.us-east-1.amazonaws.com/production"
INDEX_FIELDS = "Web_ID_Number, No_of_Points, Price_Per_Point, Use_Year, Points_Available_Desc, Price, Status, Resort, Special_Tag"
DETAIL_FIELDS = ("Web_ID_Number,Name,id,No_of_Points,Price_Per_Point,Use_Year,Points_Available_Desc,Price,"
                 "Closing_Costs,Annual_Dues,Expiration_Date1,Status,Resort,Special_Tag,Additional_Listing_Info,Description")
STATUSES = ("Active", "Pending", "Reduced")
PER_PAGE = 200
CRAWL_DELAY = 10.0
_HEADERS = {"Origin": SITE, "Referer": SITE + "/listings", "Accept": "application/json"}
_last_detail = 0.0


def _current_uy_start(uy: int, today: date) -> int:
    return today.year if today.month >= uy else today.year - 1


_MON = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"


def parse_points_desc(text: str, uy: int | None, contract: int | None, today: date | None = None) -> list[dict]:
    """'477 points available now (250 + 227 banked) + 250 points coming Jun 1, 2027'
       '49 points coming Aug 1, 2027 + 250 points for 2028'  -> UY buckets."""
    if not text or not uy:
        return []
    today = today or date.today()
    out: dict[str, dict] = {}
    for part in re.split(r"\s\+\s(?![^()]*\))", text):
        m = re.match(r"\s*([\d,]+)\s*points?\b(.*)", part, re.I)
        if not m:
            continue
        pts = int(m.group(1).replace(",", ""))
        rest = m.group(2)
        year = None
        mm = re.search(_MON + r"\s*\d{1,2}(?:st|nd|rd|th)?,?\s*(20\d\d)", rest, re.I)
        if mm:
            year = int(mm.group(2))
        elif re.search(r"available now|now available", rest, re.I):
            year = _current_uy_start(uy, today)
        else:
            mm = re.search(r"\b(?:for|in)\s+(20\d\d)\b", rest, re.I)
            if mm:
                year = int(mm.group(1))
        if year is None:
            continue
        banked = 0
        mb = re.search(r"\(\s*[\d,]+\s*\+\s*([\d,]+)\s*(?:banked|borrowed)", rest, re.I)
        if mb:
            banked = int(mb.group(1).replace(",", ""))
        elif contract and pts > contract:
            banked = pts - contract
        key = f"{year}-{uy:02d}"
        out.setdefault(key, {"uy": key, "points": pts, "banked": min(banked, pts)})
    return sorted(out.values(), key=lambda b: b["uy"])


def _normalize(r: dict) -> dict:
    status = r.get("Status") or ""
    tag = r.get("Special_Tag")
    uy = month_num(r.get("Use_Year"))
    resort_raw = r.get("Resort") or ""
    d = blank(
        source=NAME,
        listing_id=str(r["id"]),
        url=f"{SITE}/listings/{r['id']}",
        resort_raw=resort_raw,
        resort=normalize_resort(resort_raw),
        points=to_int(r.get("No_of_Points")),
        price=money(r.get("Price")),
        price_per_point=money(r.get("Price_Per_Point")),
        use_year=uy,
        status_raw=status + (f" ({tag})" if tag else ""),
        availability=classify_status(status),
        availability_text=r.get("Points_Available_Desc"),
    )
    if d["price"] and d["points"] and not d["price_per_point"]:
        d["price_per_point"] = round(d["price"] / d["points"], 2)
    d["points_available"] = parse_points_desc(r.get("Points_Available_Desc"), uy, d["points"])
    d["extra"]["web_id"] = r.get("Web_ID_Number")          # the "#6AK176W" shown on the site
    if tag:
        d["extra"]["special_tag"] = tag
        if d["resort"] == "okw":
            d["extra"]["okw_2057"] = "2057" in tag
    d["extra"]["broker"] = "Vacation Club Life"
    d["extra"]["availability_from"] = "index"
    return d


def fetch_listings(session) -> list[dict]:
    flt = "((Status in (" + ", ".join(f'"{s}"' for s in STATUSES) + ")))"
    out, page = [], 1
    while True:
        params = {
            "per_page": PER_PAGE, "page": page,
            "sort_by": "Resort, No_of_Points, Use_Year", "sort_order": "desc",
            "filter": flt, "fields": INDEX_FIELDS,
        }
        r = session.get(API + "/coql/listings", params=params, headers=_HEADERS)
        if r.status_code != 200:
            log.warning("vacationclublife: API returned %s; skipping", r.status_code)
            return out
        try:
            js = r.json()
        except ValueError:
            log.warning("vacationclublife: API returned non-JSON; skipping")
            return out
        rows = js.get("data") or []
        for row in rows:
            try:
                out.append(_normalize(row))
            except Exception as e:
                log.warning("vacationclublife: bad row %s: %s", row.get("id"), e)
        if not (js.get("meta") or {}).get("more_records") or not rows or page >= 10:
            break
        page += 1
    return out


def fetch_detail(session, d: dict) -> dict:
    global _last_detail
    gap = time.time() - _last_detail
    if gap < CRAWL_DELAY:
        time.sleep(CRAWL_DELAY - gap)
    _last_detail = time.time()
    r = session.get(f"{API}/listings/{d['listing_id']}", params={"fields": DETAIL_FIELDS}, headers=_HEADERS)
    if r.status_code != 200:
        log.warning("vacationclublife detail %s -> %s", d["listing_id"], r.status_code)
        return d
    js = r.json()
    rec = js.get("data", js) if isinstance(js, dict) else {}
    if isinstance(rec, list):
        rec = rec[0] if rec else {}
    dues = money(rec.get("Annual_Dues"))
    if dues is not None:
        d["extra"]["yearly_dues"] = dues
        if d["points"]:
            d["dues_per_point"] = round(dues / d["points"], 2)
    if rec.get("Closing_Costs") is not None:
        d["extra"]["closing_cost"] = money(rec.get("Closing_Costs"))
    exp = to_int(rec.get("Expiration_Date1"))
    if exp and exp > 2000:
        d["expiration"] = exp
    if rec.get("Name"):
        d["extra"]["listing_number"] = rec.get("Name")
    notes = " ".join(x for x in (rec.get("Additional_Listing_Info"), rec.get("Description")) if x)
    if notes:
        d["extra"]["notes"] = notes
        if re.search(r"dues", notes, re.I):
            d["dues_terms"] = notes
        if d.get("resort") == "aulani" and re.search(r"subsidi[sz]ed", notes, re.I):
            d["subsidized_dues"] = True
    d["detail_fetched"] = True
    return d
