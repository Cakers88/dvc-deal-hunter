"""Push new / price-dropped high scorers to ntfy.

Env:
  NTFY_URL    full topic URL, e.g. https://ntfy.example.com/dvc-deals   (required to send)
  NTFY_TOKEN  access token if the topic is protected (optional)
  DASHBOARD_URL  link included in notifications (optional)
State: data/alerted.json remembers what we already pushed (uid -> price) so nothing repeats.
"""
from __future__ import annotations

import json
import logging
import os

import requests

log = logging.getLogger("alert")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _best(x: dict, keys: list[str]) -> dict | None:
    plans = [dict(zip(keys, p)) for p in x["plans"]]
    ok = [p for p in plans if p["fit"] != "no"]
    if not ok:
        return None
    pref = {"both": 0, "one": 1}
    return sorted(ok, key=lambda p: (pref.get(p["scenario"], 2), p["fit"] != "yes", -p["savings"]))[0]


def message(x: dict, keys: list[str], dash: str | None) -> tuple[str, str]:
    p = _best(x, keys)
    title = f"{x['score']:.0f} · {x['resort_name']} {x['points']} pts @ ${x['ppt']:.0f}"
    lines = [f"${x['price']:,.0f} · UY {x['use_year']} · {x['source']}"]
    if p:
        who = "both families" if p["scenario"] == "both" else "your family"
        trip = "Sept" if p["trip"].startswith("sep") else "July"
        lines.append(f"Covers {who}, {trip} {p['nights']}n ({p['need']} pts)"
                     + (" with borrowing" if p["fit"] == "borrow" else ""))
        lines.append(f"Net cost after resale ${p['net_cost']:,} vs renting ${p['rent_cost']:,}"
                     f" ({'saves' if p['savings'] >= 0 else 'costs'} ${abs(p['savings']):,})")
        lines.append(f"Book on {p['book_on']} · ROFR risk {x['rofr']}")
    return title, "\n".join(lines)


def send(index: dict, events: list[dict], cfg: dict, first_run: bool) -> int:
    url = os.environ.get("NTFY_URL")
    path = os.path.join(ROOT, "data", "alerted.json")
    alerted = json.load(open(path)) if os.path.exists(path) else {}
    keys = list(index["plan_keys"])
    by_uid = {x["uid"]: x for x in index["listings"]}
    acfg = cfg.get("alerts", {})
    cands = []
    for e in events:
        if e["event"] not in ("new", "price_change"):
            continue
        x = by_uid.get(e["uid"])
        if not x or x["score"] < acfg.get("ntfy_min_score", 75):
            continue
        if alerted.get(x["uid"]) is not None and x["price"] >= alerted[x["uid"]]:
            continue  # already told you at this price or lower
        cands.append(x)
    cands.sort(key=lambda x: -x["score"])
    sent = 0
    dash = os.environ.get("DASHBOARD_URL")
    for x in cands[: acfg.get("max_alerts_per_run", 5)]:
        alerted[x["uid"]] = x["price"]
        if first_run:
            continue  # seed state silently on the very first run
        if not url:
            log.info("NTFY_URL not set; would alert: %s", x["uid"])
            continue
        title, body = message(x, keys, dash)
        headers = {"Title": title.encode("utf-8"), "Tags": "palm_tree",
                   "Priority": "urgent" if x["score"] >= acfg.get("ntfy_priority_score", 85) else "high"}
        if x.get("url"):
            headers["Click"] = x["url"]
            headers["Actions"] = f"view, Listing, {x['url']}" + (f"; view, Dashboard, {dash}" if dash else "")
        if os.environ.get("NTFY_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
        try:
            r = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=15)
            r.raise_for_status()
            sent += 1
        except Exception as ex:
            log.warning("ntfy failed: %s", ex)
            alerted.pop(x["uid"], None)
    json.dump(alerted, open(path, "w"), indent=1, sort_keys=True)
    return sent
