"""DVC Market (dvcmarket.com) -- aggregator / MLS of many DVC resale brokers.

How the site serves data
------------------------
Server-rendered Laravel + Alpine, no XHR listing API.  The listings page
https://dvcmarket.com/dvc-resale-listings embeds the ENTIRE active inventory
as one JS literal:

    const ALL_LISTINGS = [{...}, {...}, ...];      (~700 rows, ~0.9 MB page)
    const POINT_YEAR_LABELS = ["26", "27", "28"];

so a full fetch is ONE request.  Each row carries the underlying broker
(`broker_name`) and that broker's own listing URL (`listing_url`), which we
expose in extra[...] for de-duplication against direct broker scrapes.

Only ACTIVE listings are published (pending / under-contract contracts are
dropped from the index; sold ones move to sitemap-listings-sold.xml and their
detail page carries schema.org availability "SoldOut").

Availability years: points_available_year1..3 correspond to the use years that
START in calendar 20{POINT_YEAR_LABELS[i]} (verified against DVC Sales,
DVC Resale Market detail pages and Buy & Sell DVC).  NOTE: the per-listing
detail page labels the same three numbers one year earlier ("DEC 2025") --
that label is stale/wrong, so we never take years from the detail page.
dvcmarket stores "unknown" as 0, so a 0 in the furthest bucket is dropped
(extra["availability_incomplete"]) -- see normalize().

"own" rows (source == "own") are DVC Sales' own inventory (same company);
their dvcmarket listing_id (e.g. "SS1274") is DVC Sales' listing number.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from urllib.parse import urlparse, parse_qs

from ..model import blank, classify_status, money, to_int
from ..resorts import normalize_resort, month_num

log = logging.getLogger(__name__)

NAME = "dvcmarket"
BASE = "https://dvcmarket.com"
INDEX = BASE + "/dvc-resale-listings"

# broker_name on dvcmarket -> short key (matches the host / our adapter NAMEs)
BROKER_KEYS = {
    "dvc resale market": "dvcresalemarket",
    "fidelity real estate": "fidelityrealestate",
    "dvc resale experts": "dvcresaleexperts",
    "dvc shop": "dvcshop",
    "dvc sales": "dvcsales",
    "dvc store": "dvcstore",
    "vacation club life": "vacationclublife",
    "vacatia": "vacatia",
    "buy and sell dvc": "buyandselldvc",
    "dvc by resale": "dvcbyresale",
    "dvc advantages": "dvcadvantages",
}


def _broker_key(name: str | None, url: str | None) -> str | None:
    if name and name.strip().lower() in BROKER_KEYS:
        return BROKER_KEYS[name.strip().lower()]
    host = urlparse(url or "").netloc.lower()
    if not host:
        return None
    host = re.sub(r"^(www\.|resales\.)", "", host)
    return host.split(".")[0]


def broker_listing_id(key: str | None, url: str | None) -> str | None:
    """Best-effort broker-side id from the broker's listing URL."""
    if not url:
        return None
    u = urlparse(url)
    q = parse_qs(u.query)
    if "listid" in q:                               # dvcbyresale buy.php?listid=...
        return q["listid"][0]
    seg = [s for s in u.path.split("/") if s]
    if not seg:
        return None
    last = seg[-1]
    if key == "dvcresalemarket":                    # /listings/<resort>/ak26450/
        return last.upper()
    return last


def _extract_js_value(page: str, name: str):
    m = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*", page)
    if not m:
        return None
    val, _ = json.JSONDecoder().raw_decode(page[m.end():])
    return val


def _year_labels(page: str) -> list[int]:
    labels = None
    try:
        labels = _extract_js_value(page, "POINT_YEAR_LABELS")
    except ValueError:
        pass
    if labels and len(labels) >= 3:
        return [2000 + int(x) if int(x) < 100 else int(x) for x in labels[:3]]
    y = date.today().year                           # fallback: current calendar year
    log.warning("dvcmarket: POINT_YEAR_LABELS missing, assuming %s..%s", y, y + 2)
    return [y, y + 1, y + 2]


def _resort(row: dict) -> tuple[str, str | None]:
    short = row.get("resort_short_name") or ""
    full = row.get("resort_name") or ""
    # short name first: the full name "Copper Creek Villas & Cabins" would
    # otherwise hit the Cabins-at-Fort-Wilderness pattern.
    key = normalize_resort(short) or normalize_resort(full)
    return full or short, key


def normalize(row: dict, years: list[int]) -> dict:
    status = (row.get("status") or "").strip()
    uy = month_num(row.get("use_year"))
    pts = row.get("points_on_contract")
    resort_raw, resort_key = _resort(row)
    d = blank(
        source=NAME,
        listing_id=str(row["id"]),
        url=BASE + row["seo_url"] if row.get("seo_url") else f"{BASE}/listing-info/{row.get('listing_id')}",
        resort_raw=resort_raw,
        resort=resort_key,
        points=to_int(pts),
        price=money(row.get("total_price")),
        price_per_point=money(row.get("price_per_point")),
        use_year=uy,
        status_raw=status or None,
        availability=classify_status("sold" if status.lower() in ("sold", "removed") else
                                     (None if status.lower() == "active" else status)),
        dues_per_point=money(row.get("dues_per_point")),
        expiration=to_int(row.get("expiration_year")),
    )
    created = row.get("created_at") or ""
    if re.match(r"\d{4}-\d{2}-\d{2}", created):
        d["date_listed"] = created[:10]             # = "Date Listed" shown on the page

    # per-use-year availability
    if uy:
        contract = d["points"] or 0
        buckets = []
        for i, yr in enumerate(years):
            v = row.get(f"points_available_year{i + 1}")
            if v is None:
                continue
            v = int(v)
            buckets.append({
                "uy": f"{yr}-{uy:02d}",
                "points": v,
                # anything above the annual allotment must have been banked/borrowed in
                "banked": max(0, v - contract) if contract else 0,
            })
        # dvcmarket stores "unknown" as 0.  Brokers that only publish free text
        # ("320 points for 2027 and beyond") come through as [0, 320, 0].  The
        # furthest year can't really be 0 (you can only borrow from the NEXT use
        # year), so a trailing 0 means "not stated": drop it and flag it.
        if contract and len(buckets) == 3 and buckets[2]["points"] == 0:
            buckets = buckets[:2]
            d["extra"]["availability_incomplete"] = True
        d["points_available"] = buckets
        d["availability_text"] = " | ".join(f"{b['uy']}: {b['points']}" for b in buckets) or None

    key = _broker_key(row.get("broker_name"), row.get("listing_url"))
    x = d["extra"]
    x["broker"] = row.get("broker_name")
    x["broker_key"] = key
    x["broker_url"] = row.get("listing_url")
    x["broker_listing_id"] = broker_listing_id(key, row.get("listing_url"))
    x["dvcmarket_listing_id"] = row.get("listing_id")       # "dvcs-ext-13383" or DVC Sales id "SS1274"
    x["dvcmarket_source"] = row.get("source")               # "external" | "own" (DVC Sales)
    if row.get("source") == "own":
        x["broker_ref"] = row.get("listing_id")             # DVC Sales' visible "Listing ID"
    x["closing_cost"] = money(row.get("closing_costs"))
    x["yearly_dues"] = money(row.get("yearly_dues"))
    x["days_on_market"] = row.get("days_on_market")
    x["deal_score"] = row.get("score")
    x["star_rating"] = row.get("star_rating")
    x["rvc_ppp"] = money(row.get("rvc"))                    # dvcmarket's reference/appraised $/pt
    if row.get("appraised_ppt"):
        x["appraised_ppp"] = money(row.get("appraised_ppt"))
    if row.get("minimum_offer_price"):
        x["minimum_offer_price"] = money(row.get("minimum_offer_price"))
    x["resort_short_name"] = row.get("resort_short_name")
    if resort_key == "okw":
        x["okw_2057"] = bool(re.search(r"57", (row.get("resort_short_name") or "") + (row.get("resort_name") or "")))
        # expiration_year is often blank for OKW; the short name ("OKW(42)" / "OKW(57)") says which deed it is
        if not d["expiration"]:
            if x["okw_2057"]:
                d["expiration"] = 2057
            elif re.search(r"42", row.get("resort_short_name") or ""):
                d["expiration"] = 2042
    x["synced_at"] = row.get("synced_at")
    x["updated_at"] = row.get("updated_at")
    x["sync_hash"] = row.get("sync_hash")
    x["availability_from"] = "index"
    x["availability_years"] = years
    return d


def fetch_listings(session) -> list[dict]:
    r = session.get(INDEX)
    if r.status_code != 200 or "ALL_LISTINGS" not in r.text:
        log.warning("dvcmarket: index returned %s without ALL_LISTINGS (blocked or changed); skipping",
                    r.status_code)
        return []
    page = r.text
    try:
        rows = _extract_js_value(page, "ALL_LISTINGS") or []
    except ValueError as e:
        log.warning("dvcmarket: could not decode ALL_LISTINGS (%s)", e)
        return []
    years = _year_labels(page)
    out = []
    for row in rows:
        try:
            out.append(normalize(row, years))
        except Exception as e:  # one bad row must not kill the batch
            log.warning("dvcmarket: skipping row %s: %s", row.get("id"), e)
    return out


def _text(page: str) -> str:
    import html as H
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
    t = H.unescape(re.sub(r"<[^>]+>", "\n", t))
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t)


def fetch_detail(session, d: dict) -> dict:
    """One request to the dvcmarket listing page: current status (InStock /
    SoldOut), dues, closing breakdown.  Per-use-year availability is NOT
    re-read here (detail-page year labels are off by one; index is right)."""
    r = session.get(d["url"])
    if r.status_code == 404:
        d["status_raw"] = "removed"
        d["availability"] = "sold"
        d["detail_fetched"] = True
        return d
    if r.status_code != 200:
        log.warning("dvcmarket detail %s -> %s", d["url"], r.status_code)
        return d
    page = r.text
    m = re.search(r'"availability"\s*:\s*"https?://schema\.org/(\w+)"', page)
    if m:
        av = m.group(1)
        d["extra"]["schema_availability"] = av
        if av in ("SoldOut", "Discontinued", "OutOfStock"):
            d["status_raw"] = "sold"
            d["availability"] = "sold"
    t = _text(page)
    m = re.search(r"Annual Dues:\n\$([\d.]+) Per Point\n\$([\d,.]+) Per Year", t)
    if m:
        d["dues_per_point"] = float(m.group(1))
        d["dues_terms"] = f"${m.group(1)}/pt, ${m.group(2)}/yr"
    m = re.search(r"Date Listed:\n([A-Z][a-z]{2} \d{1,2}, \d{4})", t)
    if m:
        from datetime import datetime
        d["date_listed"] = datetime.strptime(m.group(1), "%b %d, %Y").date().isoformat()
    m = re.search(r"Dues Reimbursement \((\d{4})\)\n\$([\d,.]+)", t)
    if m:
        d["extra"]["dues_reimbursement"] = {"year": int(m.group(1)), "amount": money(m.group(2))}
        terms = d.get("dues_terms") or ""
        d["dues_terms"] = (terms + "; " if terms else "") + f"buyer reimburses {m.group(1)} dues ${m.group(2)}"
    m = re.search(r"Est\. Closing Costs\n\$([\d,.]+)", t)
    if m:
        d["extra"]["closing_cost"] = money(m.group(1))
    m = re.search(r"Total Due at Closing\n\$([\d,.]+)", t)
    if m:
        d["extra"]["total_due_at_closing"] = money(m.group(1))
    m = re.search(r"Contract Point Status:\n([^\n]+)", t)
    if m:
        d["extra"]["contract_point_status"] = m.group(1).strip()
    d["detail_fetched"] = True
    return d
