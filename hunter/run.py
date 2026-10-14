"""Full pipeline: scrape -> analyze -> alert. Used by the GitHub Action."""
from __future__ import annotations

import json
import logging
import os

import yaml

from . import analyze, scrape, alert

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    first_run = not os.path.exists(os.path.join(ROOT, "data", "listings.json"))
    res = scrape.run()
    analyze.run()
    cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml")))
    index = json.load(open(os.path.join(ROOT, "docs", "data", "index.json")))
    sent = alert.send(index, res["events"], cfg, first_run)
    health = res["health"]
    health["alerts_sent"] = sent
    health["events"] = {k: sum(1 for e in res["events"] if e["event"] == k)
                        for k in ("new", "price_change", "status_change", "gone")}
    health["scored"] = index["count"]
    json.dump(health, open(os.path.join(ROOT, "data", "health.json"), "w"), indent=1)
    json.dump(health, open(os.path.join(ROOT, "docs", "data", "health.json"), "w"), indent=1)
    logging.info("done: %s", health["events"])


if __name__ == "__main__":
    main()
