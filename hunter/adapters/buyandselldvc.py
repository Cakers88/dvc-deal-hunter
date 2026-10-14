"""Buy & Sell DVC (buyandselldvc.com; thedvcstore.com redirects here).

Next.js site, but /listings is server-rendered: every contract (available,
reduced, sale pending) is an <a href="/listings/<CODE>"> card containing
resort, status, points, a 3-year availability strip "0 | 29 | 50" with its
year labels "2026 · 2027 · 2028" (= use-year START years; confirmed against
the detail page text "50 points coming on 2/1/27 and 550 points coming on
2/1/28"), use year, $/pt and price.  One request.

robots.txt disallows /api/, /_next/ and ?page=/?sort= variants, so we only
read the plain /listings page and /listings/<CODE> detail pages.

dvcmarket.com carries only the available/reduced ones; its broker_url is
https://buyandselldvc.com/listings/<CODE>, so listing_id == broker_listing_id.
"""
from __future__ import annotations

import html as H
import logging
import re

from ..model import blank, classify_status, money, to_int
from ..resorts import normalize_resort, month_num

log = logging.getLogger(__name__)

NAME = "buyandselldvc"
BASE = "https://buyandselldvc.com"
INDEX = BASE + "/listings"

_CARD = re.compile(r'<a class="group[^"]*" href="/listings/([A-Za-z0-9_-]+)">(.*?)</a>', re.S)


def _cells(body: str) -> list[str]:
    body = body.replace("<!-- -->", "")
    t = H.unescape(re.sub(r"<[^>]+>", "\n", body)).replace("\u2009", " ").replace("\xa0", " ")
    return [re.sub(r"\s+", " ", s).strip() for s in t.split("\n") if s.strip()]


def _parse_card(code: str, body: str) -> dict | None:
    c = _cells(body)
    if not c:
        return None
    resort_raw = c[0]
    status = c[1] if len(c) > 1 and not c[1].startswith("#") else ""
    j = "\n".join(c)
    pts = re.search(r"^([\d,]+) pts$", j, re.M)
    strip = re.search(r"^(\d[\d,]*) \| (\d[\d,]*) \| (\d[\d,]*)$", j, re.M)
    years = re.search(r"^(20\d\d) · (20\d\d) · (20\d\d)$", j, re.M)
    uy = re.search(r"^Use Year\n(\w+)", j, re.M)
    ppp = re.search(r"^\$/pt\n\$([\d,.]+)", j, re.M)
    price = re.search(r"^Asking\n\$([\d,.]+)", j, re.M)
    d = blank(
        source=NAME,
        listing_id=code,
        url=f"{BASE}/listings/{code}",
        resort_raw=resort_raw,
        resort=normalize_resort(resort_raw),
        points=to_int(pts.group(1)) if pts else None,
        price=money(price.group(1)) if price else None,
        price_per_point=money(ppp.group(1)) if ppp else None,
        use_year=month_num(uy.group(1)) if uy else None,
        status_raw=status or None,
        availability=classify_status(status if status.lower() not in ("available", "reduced") else None),
    )
    if strip and years and d["use_year"]:
        contract = d["points"] or 0
        d["points_available"] = [
            {"uy": f"{y}-{d['use_year']:02d}", "points": int(p.replace(",", "")),
             "banked": max(0, int(p.replace(",", "")) - contract) if contract else 0}
            for y, p in zip(years.groups(), strip.groups())
        ]
        d["availability_text"] = " | ".join(f"{y}: {p}" for y, p in zip(years.groups(), strip.groups()))
        d["extra"]["availability_from"] = "index"
    m = re.search(r"Save \$([\d,]+) vs\. Disney", j)
    if m:
        d["extra"]["savings_vs_direct"] = money(m.group(1))
    d["extra"]["broker"] = "Buy And Sell DVC"
    if status.lower() == "reduced":
        d["extra"]["reduced"] = True
    return d


def fetch_listings(session) -> list[dict]:
    r = session.get(INDEX)
    if r.status_code != 200:
        log.warning("buyandselldvc: index returned %s; skipping", r.status_code)
        return []
    out, seen = [], set()
    for code, body in _CARD.findall(r.text):
        if code in seen:
            continue
        seen.add(code)
        try:
            d = _parse_card(code, body)
        except Exception as e:
            log.warning("buyandselldvc: bad card %s: %s", code, e)
            continue
        if d:
            out.append(d)
    if not out:
        log.warning("buyandselldvc: no listing cards found (layout change?)")
    return out


def _text(page: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
    t = H.unescape(re.sub(r"<[^>]+>", "\n", t.replace("<!-- -->", "")))
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t)


def fetch_detail(session, d: dict) -> dict:
    r = session.get(d["url"])
    if r.status_code != 200:
        log.warning("buyandselldvc detail %s -> %s", d["url"], r.status_code)
        return d
    t = _text(r.text)
    uy = d.get("use_year")
    rows = re.findall(r"(?:Previous|Current|Next) Year: ?\n(20\d\d)\n([\d,]+)\npts", t)
    if rows and uy:
        contract = d["points"] or 0
        d["points_available"] = [
            {"uy": f"{y}-{uy:02d}", "points": int(p.replace(",", "")),
             "banked": max(0, int(p.replace(",", "")) - contract) if contract else 0}
            for y, p in rows
        ]
        d["extra"]["availability_from"] = "detail"
    m = re.search(r"Points Availability\n(.*?)\nContract Details", t, re.S)
    if m:
        d["availability_text"] = " ".join(m.group(1).split("\n"))
    m = re.search(r"Annual Dues / Point\n\$([\d.]+)", t)
    if m:
        d["dues_per_point"] = float(m.group(1))
    m = re.search(r"Deed Expires\n(20\d\d)", t)
    if m:
        d["expiration"] = int(m.group(1))
    m = re.search(r"Annual Dues at Closing\n([^\n]+)\n\$([\d,.]+)", t)
    if m:
        d["dues_terms"] = f"{m.group(1).strip()} (${m.group(2)})"
    m = re.search(r"Closing Costs\n[^\n]*\n\$([\d,.]+)", t)
    if m:
        d["extra"]["closing_cost"] = money(m.group(1))
    m = re.search(r"Listing #\s*\w+\n(Available|Sale Pending|Reduced|Sold|[A-Z][a-z]+(?: [A-Z][a-z]+)?)\n", t)
    if m:
        d["status_raw"] = m.group(1)
        d["availability"] = classify_status(m.group(1) if m.group(1).lower() not in ("available", "reduced") else None)
    if d.get("resort") == "aulani" and re.search(r"subsidi[sz]ed", t, re.I):
        d["subsidized_dues"] = True
    d["detail_fetched"] = True
    return d
