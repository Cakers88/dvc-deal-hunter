"""The DVC Store (dvcstore.com, The Timeshare Store, Inc.).

The site is WordPress; listings are a `resale-listing` custom post type whose ACF
fields are exposed read-only through the public REST API:

    /wp-json/wp/v2/resale-listing?per_page=100&page=N

That gives every contract (active, sale pending, sold) with points, price, $/pt,
use year, status, date listed, per-use-year availability rows, banked counts,
notes, closing costs and "annual dues at closing". Resort names come from the
`resort` taxonomy (one extra small request). A full fetch is 1 + ceil(N/100)
requests (3 for ~150 listings).

Detail pages add only Expiration (deed year) and Dues Per Point (the per-resort
rate, or the subsidized rate for some Aulani contracts).
"""
from __future__ import annotations

import html as H
import logging
import re
from datetime import date, datetime

from ..model import blank, classify_status, money, to_int
from ..resorts import normalize_resort, month_num

log = logging.getLogger(__name__)

NAME = "dvcstore"
BASE = "https://www.dvcstore.com"
API = BASE + "/wp-json/wp/v2/resale-listing"
RESORT_API = BASE + "/wp-json/wp/v2/resort?per_page=100&_fields=id,name,slug"
FIELDS = "id,slug,link,date,modified,acf,resort,class_list"
PER_PAGE = 100

_STATUS_PREFIX = re.compile(r"^\s*(SOLD|SALE\s*PENDING|PENDING|UNDER\s*CONTRACT)\s*[-–:]?\s*", re.I)


def _get_json(session, url):
    r = session.get(url)
    if r.status_code in (401, 403) or "json" not in r.headers.get("Content-Type", ""):
        log.warning("dvcstore: %s -> HTTP %s (%s); possibly blocked", url, r.status_code,
                    r.headers.get("Content-Type"))
        return None, r
    r.raise_for_status()
    return r.json(), r


def _resort_names(session) -> dict[int, str]:
    try:
        data, _ = _get_json(session, RESORT_API)
        return {t["id"]: H.unescape(t["name"]) for t in data or []}
    except Exception as e:  # names are a nicety; slugs work as a fallback
        log.warning("dvcstore: resort taxonomy fetch failed: %s", e)
        return {}


def fetch_listings(session) -> list[dict]:
    names = _resort_names(session)
    raw: list[dict] = []
    page, pages = 1, 1
    while page <= pages:
        url = f"{API}?per_page={PER_PAGE}&page={page}&orderby=date&order=desc&_fields={FIELDS}"
        data, r = _get_json(session, url)
        if data is None:
            return []
        raw.extend(data)
        pages = int(r.headers.get("X-WP-TotalPages", "1") or 1)
        page += 1
    out = [_normalize(x, names) for x in raw]
    return [d for d in out if d]


def _uy_key(yy: str, uy: int | None) -> str | None:
    m = re.search(r"\d+", str(yy or ""))
    if not m or not uy:
        return None
    y = int(m.group(0))
    y = y + 2000 if y < 100 else y
    return f"{y}-{uy:02d}"


def _clean_notes(s: str | None) -> str:
    s = H.unescape(re.sub(r"<[^>]+>", " ", s or "")).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\s*\n\s*", "\n", s).strip()


def _availability(acf: dict, uy: int | None) -> list[dict]:
    """ACF rows [{year '26', points '181'|'BKD'}] -> buckets keyed by UY start.

    'BKD' means that use year's points were banked forward (0 left). The banked
    count (`current_banked`) lands in the following use year. Use-year rows beyond
    the three ACF rows are picked up from the notes ("2029- 115 points coming ...").
    """
    buckets: dict[str, dict] = {}
    bkd_years: list[int] = []
    for row in acf.get("points_available") or []:
        key = _uy_key(row.get("year_for_points_available"), uy)
        if not key:
            continue
        v = str(row.get("points_available_by_year") or "").strip()
        if v.upper().startswith("BKD"):
            buckets[key] = {"uy": key, "points": 0, "banked": 0}
            bkd_years.append(int(key[:4]))
        elif re.match(r"^\d", v):
            buckets[key] = {"uy": key, "points": int(re.match(r"\d+", v).group(0)), "banked": 0}
    notes = _clean_notes(acf.get("notes"))
    # extra years only mentioned in notes, e.g. "2029- 115 points coming on 3/1/29"
    for line in notes.splitlines():
        m = re.match(r"\s*(20\d\d)\s*[-–:]\s*(\d[\d,]*)\s*(?:banked\s+)?points?\b", line, re.I)
        if not m or re.search(r"banked\s+into|banked\s*$", line, re.I):
            continue
        key = f"{m.group(1)}-{uy:02d}" if uy else None
        if key and key not in buckets:
            buckets[key] = {"uy": key, "points": int(m.group(2).replace(",", "")), "banked": 0}
    if not buckets and uy:
        # legacy/imported records: only prose like "77 points currently available and
        # 300 points coming on 8/1/26"
        text = " ".join([acf.get("points_available_notes") or "", notes])
        for n, yy in re.findall(r"(\d[\d,]*)\s*(?:banked\s+)?points?\s*coming\s*(?:on\s*)?\d{1,2}/\d{1,2}/(\d{2,4})", text, re.I):
            y = int(yy) + (2000 if len(yy) == 2 else 0)
            key = f"{y}-{uy:02d}"
            buckets.setdefault(key, {"uy": key, "points": int(n.replace(",", "")), "banked": 0})
        m = re.search(r"(\d[\d,]*)\s*points?\s*currently\s*available", text, re.I)
        if m:
            today = date.today()
            y = today.year if today.month >= uy else today.year - 1
            key = f"{y}-{uy:02d}"
            buckets.setdefault(key, {"uy": key, "points": int(m.group(1).replace(",", "")), "banked": 0})
    # where did banked points land?
    banked = to_int(acf.get("current_banked")) or 0
    if banked and buckets:
        text = " ".join([acf.get("points_available_notes") or "", notes])
        m = re.search(r"banked\s*(?:points?\s*)?(?:from|in)\s*(20\d\d)", text, re.I)
        if m:
            dest = int(m.group(1)) + 1
        elif bkd_years:
            dest = max(bkd_years) + 1
        else:
            dest = None
        key = f"{dest}-{uy:02d}" if dest and uy else None
        if key not in buckets:
            # "457 points currently available (300 + 157 banked ...)": first bucket that
            # holds the banked points on top of the allotment
            pts = to_int(acf.get("points")) or 0
            key = next((b["uy"] for b in sorted(buckets.values(), key=lambda b: b["uy"])
                        if b["points"] >= banked and b["points"] > pts), None)
        if key in buckets:
            buckets[key]["banked"] = min(banked, buckets[key]["points"])
    return sorted(buckets.values(), key=lambda b: b["uy"])


def _normalize(x: dict, names: dict[int, str]) -> dict | None:
    acf = x.get("acf") or {}
    if not isinstance(acf, dict) or not acf:
        return None
    link = x.get("link") or ""
    m = re.search(r"/resort/([^/]+)/", link)
    rslug = m.group(1) if m else ""
    rid = (x.get("resort") or [None])[0]
    resort_raw = names.get(rid) or rslug.replace("-", " ")
    uy = month_num(acf.get("use_year") or acf.get("use_year_abbreviation"))
    status = (acf.get("custom_status") or acf.get("status") or "").strip()
    shown_id = (acf.get("listing_id") or "").strip()
    avail_notes = _clean_notes(acf.get("points_available_notes"))
    notes = _clean_notes(acf.get("notes"))
    alltext = avail_notes + "\n" + notes

    d = blank(
        source=NAME,
        # slug is stable across status changes; the shown id gains "SOLD-" etc.
        listing_id=(x.get("slug") or str(x.get("id"))).upper(),
        url=link,
        resort_raw=resort_raw,
        resort=normalize_resort(resort_raw) or normalize_resort(rslug.replace("-", " ")),
        points=to_int(acf.get("points")),
        price=money(acf.get("price")),
        price_per_point=money(acf.get("price_per_point")),
        use_year=uy,
        status_raw=status,
        availability=classify_status(status),
        availability_text=avail_notes or None,
    )
    if d["price_per_point"] is None and d["price"] and d["points"]:
        d["price_per_point"] = round(d["price"] / d["points"], 2)
    dl = str(acf.get("date_listed") or "")
    if re.fullmatch(r"\d{8}", dl):
        iso = f"{dl[:4]}-{dl[4:6]}-{dl[6:]}"
        # imported legacy records carry junk dates (e.g. 2031-10-01); drop future ones
        if iso <= date.today().isoformat():
            d["date_listed"] = iso
    d["points_available"] = _availability(acf, uy)
    if d["points_available"]:
        d["extra"]["availability_from"] = "index"

    # dues
    override = money(acf.get("dues_per_point_override"))
    if override:
        d["dues_per_point"] = override
    subsidized = bool(re.search(r"subsidi[sz]ed", alltext, re.I))
    d["subsidized_dues"] = True if subsidized else (False if d["resort"] == "aulani" else None)
    if subsidized and not d["dues_per_point"]:
        m = re.search(r"subsidi[sz]ed[^$]{0,60}\$\s*([\d.]+)\s*per\s*point", alltext, re.I)
        if m:
            d["dues_per_point"] = float(m.group(1))
    terms = []
    adc = acf.get("annual_dues_closing")
    if adc not in (None, ""):
        terms.append(f"Annual dues at closing (buyer): ${money(adc) or 0:,.2f}")
    m = re.search(r"NO ANNUAL DUES UNTIL\s*(20\d\d)", alltext, re.I)
    if m:
        terms.append(f"No annual dues until {m.group(1)}")
        d["extra"]["no_dues_until"] = int(m.group(1))
    if subsidized:
        terms.append("Subsidized dues" + (f" (${d['dues_per_point']}/pt)" if d["dues_per_point"] else ""))
    d["dues_terms"] = "; ".join(terms) or None

    ex = d["extra"]
    ex["post_id"] = x.get("id")
    ex["shown_listing_id"] = shown_id
    ex["broker_ref"] = _STATUS_PREFIX.sub("", shown_id)
    for k in ("closing_costs", "caf", "total_price"):
        v = money(acf.get(k))
        if v is not None:
            ex[k] = v
    ex["annual_dues_closing"] = money(adc) if adc not in (None, "") else None
    if acf.get("current_banked") not in (None, ""):
        ex["current_banked"] = to_int(acf.get("current_banked"))
    if acf.get("current_borrowed") not in (None, ""):
        ex["current_borrowed"] = to_int(acf.get("current_borrowed"))
    if (acf.get("international_seller") or "").lower() == "yes" or re.search(r"international seller", alltext, re.I):
        ex["international_seller"] = True
    m = re.search(r"(?:CANNOT|CAN'?T|CAN NOT) CLOSE (?:UNTIL|BEFORE)\s*([\d/]+)", alltext, re.I)
    if m:
        ex["can_close_after"] = m.group(1)
    for k in ("date_sold", "closing_date", "rofr_date"):
        if acf.get(k):
            ex[k] = acf.get(k)
    if notes:
        ex["notes"] = notes
    if "2057" in rslug:
        ex["expiration_hint"] = 2057
    ex["modified"] = x.get("modified")
    return d


def _text(page: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
    t = H.unescape(re.sub(r"<[^>]+>", "\n", t)).replace("\xa0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t)


def fetch_detail(session, d: dict) -> dict:
    r = session.get(d["url"])
    if r.status_code != 200:
        log.warning("dvcstore: detail %s -> HTTP %s", d["url"], r.status_code)
        return d
    t = _text(r.text)
    anchor = t.find("\nPrice Per Point\n")
    if anchor > 0:
        t = t[anchor:]
    m = re.search(r"\nExpiration\n\s*(20\d\d)", t)
    if m:
        d["expiration"] = int(m.group(1))
    m = re.search(r"\nDues Per Point\n\s*\$?\s*([\d.]+)", t)
    if m:
        dpp = float(m.group(1))
        # an explicit subsidized override from the API wins over the page's resort rate
        if not (d.get("subsidized_dues") and d.get("dues_per_point")):
            d["dues_per_point"] = dpp
        d["extra"]["dues_per_point_page"] = dpp
    m = re.search(r"\nDate Listed\n\s*([A-Z][a-z]+ \d{1,2}, \d{4})", t)
    if m and not d.get("date_listed"):
        d["date_listed"] = datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
    m = re.search(r"\nAnnual Dues at Closing\n\s*\$([\d,.]+)", t)
    if m:
        d["extra"]["annual_dues_closing"] = money(m.group(1))
    d["detail_fetched"] = True
    return d
