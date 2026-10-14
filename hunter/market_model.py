"""How much less do contracts sell for when their points are already used ("stripped")?

Fitted fresh every run from every live + pending listing we track (pending = an offer was
accepted, so closest thing to a real sale price we can see). Model, $/pt:

    ppt = resort_effect + a*ln(points)
          - b * (share of current-year points missing) * (years until the points reload)
          - c * (share of next-year points missing, i.e. borrowed)
          - d * [pending]

b = what buyers knock off per year they'd wait for points; c = extra knocked off when
next year's points are gone too; d = how far accepted prices sit below asking.
"""
from __future__ import annotations

import math
from datetime import date

import numpy as np

DEFAULTS = {"per_year_missing_current": 4.4, "missing_next": 8.8, "accepted_below_ask": 4.7}
BOUNDS = {"per_year_missing_current": (0, 30), "missing_next": (0, 40), "accepted_below_ask": (0, 20)}
EXCLUDE = {None, "rr", "vdh", "pit", "cfw"}


def _rows(listings: list[dict], today: date):
    out = []
    for d in listings:
        if not d.get("active") or not d.get("points") or not d.get("use_year") or not d.get("price_per_point"):
            continue
        if d.get("availability") not in ("available", "pending") or d.get("resort") in EXCLUDE:
            continue
        if (d.get("extra") or {}).get("availability_incomplete"):
            continue
        uy = int(d["use_year"])
        y = today.year if today.month >= uy else today.year - 1
        bk = {b["uy"]: b["points"] for b in d.get("points_available") or []}
        cur, nxt = bk.get(f"{y}-{uy:02d}"), bk.get(f"{y + 1}-{uy:02d}")
        if cur is None or nxt is None:
            continue
        p = d["points"]
        yrs = max(0.0, (date(y + 1, uy, 1) - today).days / 365)
        out.append((d["resort"], p, float(d["price_per_point"]), min(1, cur / p), min(1, nxt / p), yrs,
                    d["availability"] == "pending"))
    return out


def fit(listings: list[dict], today: date | None = None) -> dict:
    today = today or date.today()
    rows = _rows(listings, today)
    res = sorted({r[0] for r in rows})
    if len(rows) < 150 or len(res) < 3:
        return {**DEFAULTS, "n": len(rows), "fallback": True, "as_of": today.isoformat()}
    X = np.array([[1.0 if r[0] == k else 0.0 for k in res] +
                  [math.log(r[1]), (1 - r[3]) * r[5], 1 - r[4], 1.0 if r[6] else 0.0] for r in rows])
    Y = np.array([r[2] for r in rows])
    b, *_ = np.linalg.lstsq(X, Y, rcond=None)
    k = len(res)
    rng = np.random.default_rng(0)
    boot = []
    for _ in range(200):
        i = rng.integers(0, len(Y), len(Y))
        bb, *_ = np.linalg.lstsq(X[i], Y[i], rcond=None)
        boot.append(bb[k:])
    se = np.std(boot, axis=0)
    est = {"per_year_missing_current": -b[k + 1], "missing_next": -b[k + 2], "accepted_below_ask": -b[k + 3]}
    out = {"n": len(rows), "fallback": False, "as_of": today.isoformat(),
           "size_effect_per_ln_point": round(float(b[k]), 2),
           "se": {"per_year_missing_current": round(float(se[1]), 2), "missing_next": round(float(se[2]), 2),
                  "accepted_below_ask": round(float(se[3]), 2)}}
    for key, v in est.items():
        lo, hi = BOUNDS[key]
        out[key] = round(float(min(hi, max(lo, v))), 2)
    return out


def stripped_discount(model: dict, cur_frac: float, years_to_reload: float, next_frac: float) -> float:
    """$/pt a buyer knocks off for missing points at the moment you sell."""
    return (model["per_year_missing_current"] * max(0.0, 1 - cur_frac) * max(0.0, years_to_reload)
            + model["missing_next"] * max(0.0, 1 - next_frac))
