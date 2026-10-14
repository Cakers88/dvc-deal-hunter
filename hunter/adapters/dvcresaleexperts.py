"""DVC Resale Experts (dvcresaleexperts.com).

Webflow CMS site. /dvc-resale-listings renders every contract as a table row
(100 per page, Webflow pagination "?<hash>_page=N"), so a full fetch is
usually 2 requests. The index already has: listing id, resort, points, $/pt,
price, use year, 3-year availability ("26 | 27 | 28"), deed expiration, and a
marketing "status" pill (New / Reduced / Subsidized Dues / Small Contract /
International Seller / Extended / Double Points ...).

The site only shows live inventory: no pending/sold rows are published, so
availability is always "available" unless a pill ever says otherwise.

Detail page adds: annual dues total (-> dues_per_point), closing costs, CAF,
seller comments. It does NOT show who pays current-year dues (that is an
offer-form choice), banked/borrowed breakdown, or a listed date.
"""
from __future__ import annotations

import html as H
import logging
import re
from urllib.parse import urljoin

from ..model import blank, classify_status, money, to_int
from ..resorts import normalize_resort, month_num

log = logging.getLogger(__name__)

NAME = "dvcresaleexperts"
BASE = "https://www.dvcresaleexperts.com"
INDEX = BASE + "/dvc-resale-listings"
MAX_PAGES = 10

_ROW = re.compile(r'<div role="listitem" class="listing-contract-row w-dyn-item">(.*?)<div class="color-fill">', re.S)
_CELL = re.compile(r'<div[^>]*class="table-data[^"]*"[^>]*>([^<]*)</div>')
_NEXT = re.compile(r'<a href="(\?[0-9a-f]+_page=\d+)"[^>]*class="w-pagination-next"')
# header "PTS. Available | 26 | 27 | 28"
_HDR_YEARS = re.compile(r"PTS\. Available.*?<div[^>]*>\s*(\d{2})\s*</div>\s*<div[^>]*>\s*(\d{2})\s*</div>\s*<div[^>]*>\s*(\d{2})\s*</div>", re.S)


def _blocked(page: str) -> bool:
    return "listing-contract-row" not in page and re.search(r"captcha|cf-chl|access denied", page, re.I) is not None


def _parse_row(body: str, years: list[int]) -> dict | None:
    href = re.search(r'href="(/dvc-listing/[^"]+)"', body)
    cells = [H.unescape(c).strip() for c in _CELL.findall(body)]
    field = lambda name: (lambda m: H.unescape(m.group(1)).strip() if m else None)(
        re.search(rf'fs-list-field="{name}"[^>]*>([^<]*)<', body))
    if not href or not cells:
        return None
    lid = cells[0]
    status = field("status") or ""
    resort_raw = field("resort") or (cells[1] if len(cells) > 1 else "")
    uy = month_num(field("use-year"))
    d = blank(
        source=NAME,
        listing_id=lid,
        url=BASE + href.group(1),
        resort_raw=resort_raw,
        resort=normalize_resort(resort_raw),
        points=to_int(field("total-points")),
        price=money(field("price")),
        price_per_point=money(field("points")),  # their $/pt field is named "points"
        use_year=uy,
        status_raw=status,
        availability=classify_status(status),
    )
    # Subsidized Aulani contracts: status pill "Subsidized Dues" and an "AULS" id prefix.
    if d["resort"] == "aulani":
        d["subsidized_dues"] = "subsidized" in status.lower() or lid.upper().startswith("AULS")
    # 3-year availability: the run of numbers separated by "|" cells after the use year.
    try:
        i = cells.index("|")
        nums = [to_int(cells[i - 1]), to_int(cells[i + 1]), to_int(cells[i + 3])]
        tail = cells[i + 4:]
    except (ValueError, IndexError):
        nums, tail = [], []
    if uy and years and all(n is not None for n in nums):
        d["points_available"] = [
            {"uy": f"{y}-{uy:02d}", "points": n, "banked": 0} for y, n in zip(years, nums)
        ]
        d["availability_text"] = " | ".join(f"{y}: {n}" for y, n in zip(years, nums))
        d["extra"]["availability_from"] = "index"
    # tail: [deed exp, '$', price, savings]
    if tail and re.fullmatch(r"20\d\d", tail[0]):
        d["expiration"] = int(tail[0])
    sav = next((c for c in tail if c.startswith("$") and len(c) > 1), None)
    if sav:
        d["extra"]["savings_vs_direct"] = money(sav)
    if d["price"] is None and d["points"] and d["price_per_point"]:
        d["price"] = d["points"] * d["price_per_point"]
    return d


def fetch_listings(session) -> list[dict]:
    out: dict[str, dict] = {}
    url = INDEX
    years: list[int] = []
    for _ in range(MAX_PAGES):
        r = session.get(url)
        if r.status_code in (401, 403) or _blocked(r.text):
            log.warning("%s: blocked or unexpected page at %s (HTTP %s)", NAME, url, r.status_code)
            return list(out.values()) if out else []
        r.raise_for_status()
        page = r.text
        m = _HDR_YEARS.search(page)
        if m:
            years = [2000 + int(x) for x in m.groups()]
        elif not years:
            from datetime import date
            y = date.today().year
            years = [y, y + 1, y + 2]
        for body in _ROW.findall(page):
            d = _parse_row(body, years)
            if d and d["listing_id"] not in out:
                out[d["listing_id"]] = d
        nxt = _NEXT.search(page)
        if not nxt:
            break
        url = urljoin(INDEX, nxt.group(1))
    return list(out.values())


def _sidebar(page: str, label: str) -> str | None:
    """Value next to a sidebar label, e.g. 'Annual dues' -> '1625.60'."""
    m = re.search(
        rf'<div class="side-bar-info">{label}</div>.*?<div class="flex-horizontal _2px-gap">(.*?)</div></div>',
        page, re.S | re.I)
    if not m:
        return None
    return " ".join(x.strip() for x in re.findall(r">([^<]+)<", m.group(1) + "<") if x.strip())


_BANKED = re.compile(r"([\d,]+) points (?:were|are|have been) banked from [A-Z][a-z]+ 20\d\d to (20\d\d)")


def _apply_banked(d: dict, comments: str) -> None:
    """Seller comments say e.g. "150 points were banked from June 2025 to 2026";
    the availability columns don't separate banked points, so mark them on the
    bucket they were banked INTO (only when that bucket visibly holds them)."""
    buckets = {b["uy"][:4]: b for b in d.get("points_available") or []}
    for n, into in _BANKED.findall(comments):
        n = int(n.replace(",", ""))
        b = buckets.get(into)
        if b and b["points"] >= n and (not d.get("points") or b["points"] > d["points"]):
            b["banked"] = n


def fetch_detail(session, d: dict) -> dict:
    page = session.text(d["url"])
    # Cut off the "similar contracts" table at the bottom.
    cut = page.find("view other similar contracts")
    main = page[:cut] if cut > 0 else page

    # Per-year availability: "2026:" ... "0" "pts"
    av = re.findall(
        r'<div class="side-bar-info">(20\d\d):</div>.*?<div class="sidebar-info-small medium">([\d,]+)</div>',
        main, re.S)
    if av and d.get("use_year"):
        uy = d["use_year"]
        d["points_available"] = [
            {"uy": f"{y}-{uy:02d}", "points": int(p.replace(",", "")), "banked": 0} for y, p in av
        ]
        d["availability_text"] = " | ".join(f"{y}: {p}" for y, p in av)
        d["extra"]["availability_from"] = "detail"

    dues = money(_sidebar(main, "Annual dues"))
    if dues:
        d["extra"]["annual_dues_total"] = dues
        if d.get("points"):
            d["dues_per_point"] = round(dues / d["points"], 2)
    exp = _sidebar(main, "Deed Expiration")
    if exp and re.fullmatch(r"20\d\d", exp.strip()):
        d["expiration"] = int(exp)
    cc = money(_sidebar(main, "Closing costs"))
    if cc is not None:
        d["extra"]["closing_cost"] = cc
    caf = re.search(r"Contract<br/>Administration[\s\xa0]Fee</div>.*?medium\">([\d,.]+)</div>", main, re.S)
    if caf:
        d["extra"]["caf"] = money(caf.group(1))
    cm = re.search(r'Comments:</div><p[^>]*>(.*?)</p>', main, re.S)
    comments = H.unescape(re.sub(r"<[^>]+>", " ", cm.group(1))).strip() if cm else ""
    if comments:
        d["extra"]["comments"] = comments
        _apply_banked(d, comments)
    # Dues terms are not stated per listing; the offer form lets the buyer propose
    # Buyer Pays / Seller Pays / Pro-rate on available points.
    d["dues_terms"] = "Not stated; negotiable in offer (Buyer Pays / Seller Pays / Pro-rate on available points)"
    if d.get("resort") == "aulani" and re.search(r"subsidized\s+dues", comments, re.I):
        d["subsidized_dues"] = True
    pill = re.search(r'<div class="contract-status[^"]*">([^<]+)<|fs-list-field="status"[^>]*>([^<]+)<', main)
    if pill:
        s = (pill.group(1) or pill.group(2) or "").strip()
        if s and classify_status(s) != "available":
            d["status_raw"], d["availability"] = s, classify_status(s)
    d["detail_fetched"] = True
    return d
