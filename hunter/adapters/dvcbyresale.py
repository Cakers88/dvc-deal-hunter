"""DVC by Resale (dvcbyresale.com; dvcresales.com redirects here).

The home page carries one static HTML table (<tr class="currentresales">) with
EVERY contract, available and pending: property code, resort, points, use
year, deed expiration, availability free text, $/pt, price, status.  One
request.  Only "Buy Now" rows link to a detail page (buy.php?listid=...);
pending rows have no link, so their URL is the index with a #code fragment.

dvcmarket.com only carries the few available rows (the Buy Now ones), and
identifies them by listid.  We expose that as extra["listid"] for dedup.
"""
from __future__ import annotations

import html as H
import logging
import re
from datetime import date

from ..model import blank, classify_status, money, to_int
from ..resorts import normalize_resort, month_num

log = logging.getLogger(__name__)

NAME = "dvcbyresale"
BASE = "https://www.dvcbyresale.com"
INDEX = BASE + "/"

_ROW = re.compile(r'<tr class="currentresales">(.*?)</tr>', re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _uy_start_now(uy: int, today: date) -> int:
    return today.year if today.month >= uy else today.year - 1


def parse_availability(text: str, uy: int | None, contract: int | None, today: date | None = None) -> list[dict]:
    """Free text such as
         "428 points available now! (214 banked from '25, 214 from '26) All 214 coming in '27"
         "204 points coming on October 1st! (54 banked from '25, 150 from '26) All 150 coming in 2027"
         "5 points available for 2026 and all 100 coming in 2027"
         "All 230 points coming in 2028 or borrow for a trip after 06/27."
       -> [{"uy": "2026-10", "points": 204, "banked": 54}, {"uy": "2027-10", ...}]"""
    if not text or not uy:
        return []
    today = today or date.today()
    cur = _uy_start_now(uy, today)
    t = text.replace("’", "'")
    yr = r"(?:'|20)(\d\d)\b"
    # "(14 banked from '26, 150 from '27)" -> the bucket is the UY starting in '27
    hint_years = [2000 + int(y) for y in re.findall(r"from\s+'(\d\d)\b", t)]

    def next_start() -> int:
        # "coming on December 1st" / "coming in February" with no explicit year
        if hint_years:
            return max(hint_years)
        if today.month < uy:
            return today.year
        if today.month - uy <= 1:      # start date just passed; text is a few weeks old
            return today.year
        return today.year + 1

    out: dict[int, dict] = {}
    seg = r"((?:(?!\b(?:and|all|or)\b)[^!.;+*(])*)"
    pat = re.compile(r"\b([\d,]+)\s*(?:points?\s+)?(available now|available for|coming)" + seg, re.I)
    for m in pat.finditer(t):
        pts = int(m.group(1).replace(",", ""))
        kind, tail = m.group(2).lower(), m.group(3)
        my = re.search(yr, tail)
        if kind == "available now":
            year = cur
        elif my:
            year = 2000 + int(my.group(1))
        elif kind == "coming":
            year = next_start()
        else:
            continue
        banked = 0
        mb = re.match(r"\s*!?\s*\(\s*([\d,]+)\s*(?:points?\s*)?(?:banked|borrowed)", t[m.end(): m.end() + 80], re.I)
        if mb:
            banked = int(mb.group(1).replace(",", ""))
        elif contract and pts > contract:
            banked = pts - contract
        if year not in out:
            out[year] = {"uy": f"{year}-{uy:02d}", "points": pts, "banked": min(banked, pts)}
    return [out[k] for k in sorted(out)]


def fetch_listings(session) -> list[dict]:
    r = session.get(INDEX)
    if r.status_code != 200 or "currentresales" not in r.text:
        log.warning("dvcbyresale: index returned %s without listings table; skipping", r.status_code)
        return []
    out = []
    for body in _ROW.findall(r.text):
        tds = _TD.findall(body)
        if len(tds) < 9:
            continue
        c = [_clean(x) for x in tds]
        code, resort_raw, pts, uy_txt, deed, avail, ppp, price, status = c[:9]
        m = re.search(r"listid=(\d+)", body)
        listid = m.group(1) if m else None
        status = status or ("Available" if listid else "")
        uy = month_num(uy_txt)
        d = blank(
            source=NAME,
            listing_id=code,
            url=f"{BASE}/buy.php?listid={listid}" if listid else f"{INDEX}#{code}",
            resort_raw=resort_raw,
            resort=normalize_resort(resort_raw),
            points=to_int(pts),
            price=money(price),
            price_per_point=money(ppp),
            use_year=uy,
            status_raw=status or None,
            availability=classify_status(status),
            availability_text=avail or None,
            expiration=to_int(deed) if deed and deed.isdigit() else None,
        )
        d["points_available"] = parse_availability(avail, uy, d["points"])
        d["extra"].update({"broker": "DVC By Resale", "listid": listid, "availability_from": "index"})
        if d["resort"] == "okw":
            d["extra"]["okw_2057"] = "2057" in resort_raw
        m = re.search(r"credit buyer \$([\d,]+)[^.]*?(20\d\d) (?:annual )?dues", avail or "", re.I)
        if m:
            d["dues_terms"] = f"seller credits buyer ${m.group(1)} toward {m.group(2)} dues"
        m = re.search(r"(?:can'?t|may not) close before (\d{1,2}/\d{1,2}/\d{2,4})", avail or "", re.I)
        if m:
            d["extra"]["can_close_after"] = m.group(1)
        out.append(d)
    return out


def fetch_detail(session, d: dict) -> dict:
    """Only 'Buy Now' rows have a page; it has an exact per-UY table
    (Allotted / Banked In / Borrowed In / Transferred In / Hold / Available)."""
    if not d["extra"].get("listid"):
        return d
    r = session.get(d["url"])
    if r.status_code != 200:
        log.warning("dvcbyresale detail %s -> %s", d["url"], r.status_code)
        return d
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", r.text, flags=re.S)
    t = H.unescape(re.sub(r"<[^>]+>", "\n", t))
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    uy = d.get("use_year")
    rows = re.findall(r"(?:Previous|Current|Next) Use Year: (20\d\d)\n((?:[\d,]+\n){1,7})", t)
    if rows and uy:
        buckets = []
        for year, nums in rows:
            n = [int(x.replace(",", "")) for x in nums.split()]
            # columns: Allotted, Banked In, Borrowed In, Transferred In, Hold, Available, Bankable
            # the page prints only non-empty leading cells; "Available" is the last-but-one
            # when 7 columns are present, else fall back to Allotted.
            avail = n[5] if len(n) >= 6 else n[0]
            banked = (n[1] if len(n) > 1 else 0) + (n[2] if len(n) > 2 else 0)
            buckets.append({"uy": f"{year}-{uy:02d}", "points": avail, "banked": min(banked, avail)})
        d["extra"]["detail_uy_table"] = rows and [(y, s.split()) for y, s in rows]
        # the table is often just Allotted + Available; only trust it when it has the full row
        if all(len(s.split()) >= 6 for _, s in rows):
            d["points_available"] = buckets
            d["extra"]["availability_from"] = "detail"
    m = re.search(r"closing costs of \$\n?([\d,.]+)", t)
    if m:
        d["extra"]["closing_cost"] = money(m.group(1))
    m = re.search(r"at \$([\d.]+) per point", t)
    if m:
        d["dues_per_point"] = float(m.group(1))
    m = re.search(r"Deed Expiration:\n\d{1,2}/\d{1,2}/(\d{4})", t)
    if m:
        d["expiration"] = int(m.group(1))
    m = re.search(r"Title Company:\n([^\n]+)", t)
    if m:
        d["extra"]["title_company"] = m.group(1).strip()
    d["detail_fetched"] = True
    return d
