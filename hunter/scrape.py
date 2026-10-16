"""Run every broker adapter, de-duplicate, track history, fetch details for new/changed contracts.

Outputs (all committed to the repo so git keeps the full history):
  data/listings.json   current state of every contract we know about
  data/events.jsonl    append-only log: new / price change / status change / gone
  data/health.json     per-source status for this run
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import pkgutil
import re
import time
from datetime import datetime, timezone

from . import adapters as adapters_pkg
from .http import PoliteSession
from .resorts import is_legacy

log = logging.getLogger("scrape")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

# Sources scraped directly; the aggregator's copies of these are dropped when matched.
AGGREGATOR = "dvcmarket"
AGG_BROKER_MAP = {  # dvcmarket broker_key -> our adapter name
    "dvcresalemarket": "dvcresalemarket", "dvcstore": "dvcstore", "dvcshop": "dvcshop",
    "dvcresaleexperts": "dvcresaleexperts", "dvcsales": "dvcsales",
    "vacationclublife": "vacationclublife", "buyandselldvc": "buyandselldvc",
    "dvcbyresale": "dvcbyresale",
}
DETAIL_MAX_PER_RUN = 60
DETAIL_CAP_PER_SOURCE = {"vacationclublife": 4}  # its robots.txt asks for a 10s crawl delay
DETAIL_REFRESH_DAYS = 3


def _reparse_banked(d: dict) -> None:
    """Re-derive banked counts from the stored availability text so parser fixes apply to
    carried-over listings without waiting for the next detail refresh. Only the banked split
    is touched, and only when the re-parse yields the same use-year buckets and points."""
    from .model import parse_availability_lines
    pa, text = d.get("points_available") or [], d.get("availability_text")
    if not pa or not text:
        return
    new = parse_availability_lines(text)
    if [(b["uy"], b["points"]) for b in new] == [(b["uy"], b["points"]) for b in pa]:
        for b, n in zip(pa, new):
            if n["banked"]:  # never wipe a banked count another source (e.g. seller comments) set
                b["banked"] = n["banked"]
SKIP_DETAIL = {"dvcshop"}  # index already has everything
TRACKED_FIELDS = ("price", "price_per_point", "availability", "status_raw")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_adapters():
    mods = []
    for m in pkgutil.iter_modules(adapters_pkg.__path__):
        mod = importlib.import_module(f"{adapters_pkg.__name__}.{m.name}")
        if hasattr(mod, "fetch_listings") and getattr(mod, "ENABLED", True):
            mods.append(mod)
    # aggregator last so direct sources win
    return sorted(mods, key=lambda m: (m.NAME == AGGREGATOR, m.NAME))


def uid(d: dict) -> str:
    return f"{d['source']}:{d['listing_id']}"


def fingerprint(d: dict):
    if not (d.get("resort") and d.get("points") and d.get("use_year") and d.get("price")):
        return None
    return (d["resort"], d["points"], d["use_year"], round(d["price"] / 50))


def _distinct_same_source(a: dict, b: dict) -> bool:
    """Two rows from the same broker with unrelated ids are separate contracts
    (brokers often list a seller's identical contracts side by side), not mirrors.
    Ids where one is a prefix of the other (e.g. X-1 / X-2 page variants) still dedupe."""
    if a.get("source") != b.get("source"):
        return False
    x, y = str(a.get("listing_id", "")).upper(), str(b.get("listing_id", "")).upper()
    base = lambda s: re.sub(r"-\d{1,2}$", "", s)
    return base(x) != base(y)


def dedupe(by_source: dict[str, list[dict]]) -> list[dict]:
    direct = {k: v for k, v in by_source.items() if k != AGGREGATOR}
    ids = {}
    urls = {}
    for src, rows in direct.items():
        for d in rows:
            ids[(src, str(d["listing_id"]).upper())] = d
            if d.get("url"):
                urls[d["url"].rstrip("/").lower()] = d
    out = [d for rows in direct.values() for d in rows]
    for d in by_source.get(AGGREGATOR, []):
        ex = d.get("extra") or {}
        bk = AGG_BROKER_MAP.get(ex.get("broker_key") or "")
        match = None
        if bk:
            for cand in (ex.get("broker_listing_id"), ex.get("broker_ref")):
                if cand and (bk, str(cand).upper()) in ids:
                    match = ids[(bk, str(cand).upper())]
                    break
            if not match and ex.get("broker_url"):
                match = urls.get(ex["broker_url"].rstrip("/").lower())
        if match:
            match.setdefault("extra", {})["also_on"] = AGGREGATOR
            continue
        d["extra"]["via_aggregator"] = True
        if bk:
            d["extra"]["aggregator_only"] = True  # broker scraped directly but not found there (likely gone/pending)
        out.append(d)
    # cross-source duplicates (mirrors): same resort/points/use year/price
    seen = {}
    final = []
    for d in out:
        fp = fingerprint(d)
        if fp and d["availability"] == "available" and fp in seen and not _distinct_same_source(seen[fp], d):
            keep = seen[fp]
            keep.setdefault("extra", {}).setdefault("duplicates", []).append(uid(d))
            continue
        if fp and d["availability"] == "available":
            seen[fp] = d
        final.append(d)
    return final


def wanted_for_detail(d: dict) -> bool:
    return is_legacy(d.get("resort")) and d.get("availability") == "available" and d["source"] not in SKIP_DETAIL


def run() -> dict:
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, "listings.json")
    prev = {}
    if os.path.exists(path):
        prev = {r["uid"]: r for r in json.load(open(path))["listings"]}

    session = PoliteSession()
    by_source, health = {}, {}
    for mod in load_adapters():
        t = time.time()
        try:
            rows = mod.fetch_listings(session)
            by_source[mod.NAME] = rows
            health[mod.NAME] = {"ok": True, "count": len(rows), "seconds": round(time.time() - t, 1)}
            if not rows:
                health[mod.NAME]["ok"] = False
                health[mod.NAME]["error"] = "returned 0 listings"
                # an empty page is almost always a transient/site problem, not a sell-out:
                # keep last good data instead of marking the whole inventory gone
                by_source[mod.NAME] = [r for r in prev.values() if r["source"] == mod.NAME and r.get("active")]
                for r in by_source[mod.NAME]:
                    r["stale"] = True
        except Exception as e:  # keep last good data for this source
            log.exception("adapter %s failed", mod.NAME)
            health[mod.NAME] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
            by_source[mod.NAME] = [r for r in prev.values() if r["source"] == mod.NAME and r.get("active")]
            for r in by_source[mod.NAME]:
                r["stale"] = True

    current = dedupe(by_source)
    ts = now_iso()
    events = []
    mods = {m.NAME: m for m in load_adapters()}
    detail_budget = DETAIL_MAX_PER_RUN
    per_src: dict[str, int] = {}
    # prioritize details: Aulani first, then cheapest $/pt
    current.sort(key=lambda d: (d.get("resort") != "aulani", d.get("price_per_point") or 999))

    merged = {}
    for d in current:
        k = uid(d)
        d["uid"] = k
        old = prev.get(k)
        d["active"] = True
        d.setdefault("stale", False)
        if old:
            d["first_seen"] = old.get("first_seen", ts)
            d["history"] = old.get("history", [])
            changed = {f: (old.get(f), d.get(f)) for f in TRACKED_FIELDS if old.get(f) != d.get(f)}
            if changed:
                d["history"].append({"t": ts, **{f: v[1] for f, v in changed.items()}})
                kind = "price_change" if "price" in changed else "status_change"
                events.append({"t": ts, "uid": k, "event": kind, "changes": changed})
            # carry over detail fields if listing unchanged
            if old.get("detail_fetched") and not changed and not d.get("detail_fetched"):
                for f in ("points_available", "availability_text", "dues_terms", "dues_per_point",
                          "subsidized_dues", "expiration", "date_listed"):
                    if old.get(f) not in (None, [], ""):
                        if f == "points_available" and d.get("extra", {}).get("availability_from") == "detail":
                            continue
                        d[f] = old[f]
                d["detail_fetched"] = True
                d["detail_at"] = old.get("detail_at")
                for ek, ev in (old.get("extra") or {}).items():
                    d["extra"].setdefault(ek, ev)
                _reparse_banked(d)
                if hasattr(mods.get(d["source"]), "reapply"):
                    mods[d["source"]].reapply(d)
        else:
            d["first_seen"] = ts
            d["history"] = [{"t": ts, **{f: d.get(f) for f in TRACKED_FIELDS}}]
            events.append({"t": ts, "uid": k, "event": "new"})

        need_detail = wanted_for_detail(d) and (
            not d.get("detail_fetched")
            or not d.get("detail_at")
            or (datetime.fromisoformat(ts) - datetime.fromisoformat(d["detail_at"])).days >= DETAIL_REFRESH_DAYS
        )
        mod = mods.get(d["source"])
        src_used = per_src.get(d["source"], 0)
        src_cap = DETAIL_CAP_PER_SOURCE.get(d["source"], DETAIL_MAX_PER_RUN)
        if need_detail and detail_budget > 0 and src_used < src_cap and mod and hasattr(mod, "fetch_detail") and not d.get("stale"):
            per_src[d["source"]] = src_used + 1
            try:
                mod.fetch_detail(session, d)
                d["detail_at"] = ts
                detail_budget -= 1
                if d.get("availability") != "available":
                    events.append({"t": ts, "uid": k, "event": "status_change",
                                   "changes": {"availability": ["available", d["availability"]]}})
            except Exception as e:
                log.warning("detail %s failed: %s", k, e)
        merged[k] = d

    # gone listings
    for k, old in prev.items():
        if k in merged:
            continue
        if old.get("active"):
            old["active"] = False
            old["gone_at"] = ts
            events.append({"t": ts, "uid": k, "event": "gone",
                           "last": {f: old.get(f) for f in ("price", "price_per_point", "availability")}})
        # keep gone listings 120 days for market history
        gone = old.get("gone_at")
        if gone and (datetime.fromisoformat(ts) - datetime.fromisoformat(gone)).days > 120:
            continue
        merged[k] = old

    listings = sorted(merged.values(), key=lambda d: d["uid"])
    json.dump({"updated": ts, "count": len(listings), "listings": listings},
              open(path, "w"), indent=1, default=str)
    with open(os.path.join(DATA, "events.jsonl"), "a") as f:
        for e in events:
            f.write(json.dumps(e, default=str) + "\n")
    health_doc = {"updated": ts, "requests": session.requests_made, "sources": health,
                  "detail_fetches": DETAIL_MAX_PER_RUN - detail_budget}
    json.dump(health_doc, open(os.path.join(DATA, "health.json"), "w"), indent=1)
    log.info("scrape done: %d listings, %d events, %d requests", len(listings), len(events), session.requests_made)
    return {"events": events, "health": health_doc}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    run()
