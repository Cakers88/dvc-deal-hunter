"""Score every available legacy contract against our Aulani trips.

For each contract x trip x nights x scenario we work out:
  - points needed (official 2027 Aulani chart, chosen view)
  - points usable for that stay given the contract's use year:
      current-bucket points + bankable prior-year points (if we close before the banking
      deadline) [+ borrowable next-year points]
  - when we could book (11-mo home window for Aulani contracts, 7-mo otherwise) vs when
    the membership will exist after closing
  - full cost of the buy -> use -> resell plan, with every cash movement dated
  - net cost vs simply renting the same points
Then a 0-100 score.

Output: docs/data/scored.json (dashboard) + data/scored_summary.json
"""
from __future__ import annotations

import calendar
import json
import os
import re
from datetime import date, datetime, timedelta

import yaml

from .resorts import display, is_legacy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(ROOT, "reference")


# ---------------------------------------------------------------- helpers
def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def uy_start(d: date, uy_month: int) -> date:
    return date(d.year if d.month >= uy_month else d.year - 1, uy_month, 1)


def parse_date(s) -> date | None:
    if not s:
        return None
    if isinstance(s, date):
        return s
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(str(s)[:10] if fmt == "%Y-%m-%d" else str(s), fmt).date()
        except ValueError:
            pass
    return None


def load_ref():
    chart = json.load(open(os.path.join(REF, "aulani_points_2027.json")))
    resorts = json.load(open(os.path.join(REF, "resorts.json")))["resorts"]
    fees = json.load(open(os.path.join(REF, "fees.json")))
    periods = []
    for p in chart["travel_periods"]:
        for a, b in p["date_ranges"]:
            periods.append((date.fromisoformat(a), date.fromisoformat(b), p["name"], p["nightly"]))
    return periods, resorts, fees


def period_for(d: date, periods):
    for a, b, name, nightly in periods:
        if a <= d <= b:
            return name, nightly
    raise ValueError(f"no travel period for {d}")


def points_needed(check_in: date, nights: int, rooms: list[str], view: str, periods) -> tuple[int, list[str]]:
    total, names = 0, set()
    for i in range(nights):
        name, nightly = period_for(check_in + timedelta(days=i), periods)
        names.add(name)
        for r in rooms:
            v = nightly[r].get(view) or nightly[r].get("S")
            total += v
    return total, sorted(names)


# ---------------------------------------------------------------- contract facts
def buckets(d: dict) -> dict[date, dict]:
    out = {}
    for b in d.get("points_available") or []:
        try:
            y, m = map(int, b["uy"].split("-"))
            out[date(y, m, 1)] = {"points": int(b.get("points") or 0), "banked": int(b.get("banked") or 0)}
        except Exception:
            continue
    return out


def bucket_points(bk: dict, start: date, contract_pts: int) -> tuple[int, int, bool]:
    """(points, banked, assumed) for a use-year starting at `start`."""
    if start in bk:
        p = bk[start]["points"]
        # anything above the annual allotment was banked in and can't be re-banked
        return p, max(bk[start]["banked"], p - contract_pts if contract_pts else 0), False
    if bk and start > max(bk):
        return contract_pts, 0, True  # future year not listed: full allotment
    if bk and start < min(bk):
        return 0, 0, False
    return contract_pts, 0, True


def dues_rate(d: dict, rref: dict) -> float:
    if d.get("dues_per_point"):
        return float(d["dues_per_point"])
    if d.get("subsidized_dues") and d.get("resort") == "aulani":
        return 8.2369
    return float(rref.get("dues_per_point_2026") or 10)


def who_pays(terms: str | None, year: int) -> str:
    """'seller' | 'buyer' | 'unknown' for a dues year, from the listing's free text."""
    t = (terms or "").lower()
    if not t:
        return "unknown"
    y = str(year)
    if re.search(rf"seller pays[^.]*{y}", t) or re.search(rf"no (annual )?dues (until|for) {year + 1}", t) \
            or re.search(rf"no {y} dues|{y}[^.]{{0,30}}no dues|with no dues", t):
        if not re.search(rf"buyer pays[^.]*{y}", t):
            return "seller"
    if re.search(rf"buyer (pays|reimburses)[^.]*{y}", t):
        return "buyer"
    return "unknown"


def rofr_risk(d: dict, rref: dict) -> tuple[str, str]:
    r = rref.get("rofr_recent") or {}
    ppt = d.get("price_per_point") or 0
    mx = r.get("max_buyback_price_13mo")
    recent = (r.get("most_recent_buyback") or {}).get("highest_price")
    rate = r.get("ytd_2026_buyback_rate_pct") or 0
    if mx is None:
        level, why = "low", "no recent buybacks at this resort"
    elif recent and ppt <= recent:
        level, why = "high", f"${ppt:.0f}/pt is at/below Disney's latest buyback (${recent})"
    elif ppt <= mx:
        level, why = "medium", f"${ppt:.0f}/pt is within Disney's 13-mo buyback range (≤${mx})"
    elif ppt <= mx + 5:
        level, why = "medium", f"${ppt:.0f}/pt is just above Disney's highest recent buyback (${mx})"
    else:
        level, why = "low", f"above Disney's highest recent buyback (${mx})"
    if rate >= 10 and level != "high":
        level = {"low": "medium", "medium": "high"}[level]
        why += f"; Disney buys back {rate:.0f}% here"
    return level, why


# ---------------------------------------------------------------- the plan
def plan(d: dict, trip: dict, nights: int, scenario: dict, cfg: dict, periods, rref: dict, fees: dict,
         today: date) -> dict:
    A = cfg["assumptions"]
    view = cfg.get("view", "S")
    ci = parse_date(trip["check_in"])
    co = ci + timedelta(days=nights)
    need, pnames = points_needed(ci, nights, scenario["rooms"], view, periods)
    pts = int(d.get("points") or 0)
    uy = int(d["use_year"])
    bk = buckets(d)
    aulani_home = d.get("resort") == "aulani"

    # closing timeline
    close_days = A["close_days_aulani"] if aulani_home else A["close_days_other"]
    closing = today + timedelta(days=close_days)
    cca = parse_date((d.get("extra") or {}).get("can_close_after"))
    if cca and cca > closing:
        closing = cca
    ready = closing + timedelta(days=A["membership_setup_days"])

    # use-year math
    s0 = uy_start(ci, uy)
    s_prev, s_next = add_months(s0, -12), add_months(s0, 12)
    cur, cur_banked, cur_assumed = bucket_points(bk, s0, pts)
    prev, prev_banked, _ = bucket_points(bk, s_prev, pts)
    nxt, _, nxt_assumed = bucket_points(bk, s_next, pts)
    bank_deadline = add_months(s_prev, 8) - timedelta(days=1)
    # prior-year points can be banked into the trip's use year only if we own the
    # contract before that year's banking deadline (8 months into the use year)
    bankable = max(0, prev - prev_banked) if ready <= bank_deadline else 0
    usable_nb = cur + bankable
    usable_b = usable_nb + nxt
    if usable_nb >= need:
        fit, borrow = "yes", 0
    elif usable_b >= need:
        fit, borrow = "borrow", need - usable_nb
    else:
        fit, borrow = "no", 0
    bank_used = max(0, min(bankable, need - cur)) if fit != "no" else 0
    leftover = max(0, usable_nb - need) if fit == "yes" else 0
    # banked leftovers of current year beyond... (prior-year points not banked expire)
    lost_prior = max(0, bankable - bank_used) if fit != "no" else 0

    # booking
    window = add_months(ci, -11 if aulani_home else -7)
    book_on = max(window, ready)
    late = (ready - window).days
    tp = pnames[0] if pnames else ""
    peak = any("Period 4" in n or "Period 5" in n for n in pnames)
    seven = add_months(ci, -7)
    bank_margin = (bank_deadline - ready).days if bank_used else None
    if late <= 0:
        brisk = "low"
    elif aulani_home and book_on <= seven:
        brisk = "low" if not peak else "medium"     # still ahead of the 7-month crowd
    else:
        brisk = "medium" if not peak else "high"

    if bank_used and bank_margin is not None and bank_margin < 21:
        brisk = "high" if bank_margin < 0 else ("medium" if brisk == "low" else brisk)

    # costs
    price = float(d.get("price") or 0)
    ex = d.get("extra") or {}
    closing_cost = float(ex.get("closing_cost") or ex.get("closing_costs") or A["closing_cost"])
    caf = float(ex.get("caf") or A["caf_fee"])
    rate26 = dues_rate(d, rref)
    g = (rref.get("dues_cagr_5yr_2021_2026_pct") or A["dues_growth_pct"]) / 100

    def annual_dues(y):
        return pts * rate26 * (1 + g) ** (y - 2026)

    # sale options
    trip_uy_end = s_next
    harpta = A["harpta_pct"] if aulani_home else 0
    if (rref.get("resale_avg_price_per_point") or {}).get("trailing_6mo_avg_2026-04_to_2026-09"):
        market = float(rref["resale_avg_price_per_point"]["trailing_6mo_avg_2026-04_to_2026-09"])
    else:
        market = float(d.get("price_per_point") or 0)
    # Conservative exit: resell at what we paid, or the market average if that's lower.
    # (A below-market ask is often below market for a reason; don't bank on a gain.)
    ppt_paid = float(d.get("price_per_point") or market)
    exit_ppt = min(ppt_paid, market + (15 if d.get("subsidized_dues") and aulani_home else 0))
    comm = A["seller_commission_pct"]
    estoppel = float(((fees.get("seller_costs") or {}).get("disney_estoppel_fee_usd")) or 150)

    def dues_between(a: date, b: date):
        """buyer-borne dues for ownership from a to b (calendar-year billing, prorated)."""
        total, rows = 0.0, []
        for y in range(a.year, b.year + 1):
            ys, ye = date(y, 1, 1), date(y, 12, 31)
            s, e = max(a, ys), min(b, ye)
            if e < s:
                continue
            frac = ((e - s).days + 1) / 365
            payer = who_pays(d.get("dues_terms"), y)
            amt = annual_dues(y) * frac
            if payer == "seller" and y <= closing.year + 1:
                amt = 0.0
            total += amt
            rows.append((y, round(amt), payer))
        return total, rows

    options = []
    # A: sell soon after the trip (current UY partly used -> stripped discount)
    list_a = co + timedelta(days=7)
    disc_a = 0.0
    remaining_cur = max(0, cur + bank_used - need) if fit == "yes" else 0
    if remaining_cur < 0.5 * pts:
        disc_a += A["stripped_discount_per_point"]
    if borrow:
        disc_a += A["stripped_discount_per_point"]
    if list_a >= trip_uy_end and not borrow:
        disc_a = 0
    options.append(("sell after trip", list_a, disc_a))
    # B: wait for a fully loaded use year, sell loaded
    list_b = add_months(s0, 24 if borrow else 12)
    options.append(("sell when reloaded", list_b, 0.0))

    best = None
    for name, list_on, disc in options:
        sale_close = list_on + timedelta(days=A["sale_marketing_days"] + A["sale_close_days"])
        dues_total, dues_rows = dues_between(closing, sale_close)
        gross = max(0.0, (exit_ppt - disc) * pts)
        net_sale = gross * (1 - comm) - estoppel
        buy_total = price + closing_cost + caf
        net_cost = buy_total + dues_total - net_sale
        o = {"exit": name, "list_on": list_on, "sale_close": sale_close, "discount_ppt": disc,
             "exit_ppt": exit_ppt - disc, "gross_sale": gross, "net_sale": net_sale,
             "dues_total": dues_total, "dues_rows": dues_rows, "buy_total": buy_total,
             "net_cost": net_cost}
        if best is None or net_cost < best["net_cost"]:
            best = o

    rent_cost = need * A["rent_rate_per_point"]
    leftover_value = leftover * A["owner_rent_out_rate"]
    savings = rent_cost - best["net_cost"]
    # break-even: sale $/pt at which owning == renting
    denom = pts * (1 - comm)
    breakeven = (best["buy_total"] + best["dues_total"] - rent_cost + estoppel) / denom + best["discount_ppt"] if denom else None
    tat = need * 0.82 if True else 0  # 2027 Aulani occupancy tax est. ($/pt), same whether owned or rented

    # cash timeline
    dep = price * A["deposit_pct"]
    cash = [
        {"date": today, "what": "Offer accepted: deposit", "amount": -dep},
        {"date": today + timedelta(days=A["rofr_days"]), "what": "Disney ROFR decision (deposit refunded if Disney buys)", "amount": 0},
    ]
    at_close = price - dep + closing_cost + caf
    cy_rows = [r for r in best["dues_rows"] if r[0] == closing.year]
    if cy_rows and cy_rows[0][1] and cy_rows[0][2] != "seller":
        at_close += cy_rows[0][1]
    pay_next_at_close = closing >= date(closing.year, 11, 20)
    cash.append({"date": closing, "what": "Closing: balance, closing costs, Disney fee" +
                 (", prorated current-year dues" if cy_rows and cy_rows[0][1] else ""), "amount": -at_close})
    for y, amt, payer in best["dues_rows"]:
        if y == closing.year or amt == 0:
            continue
        when = closing if (pay_next_at_close and y == closing.year + 1) else date(y, 1, 15)
        lbl = f"{y} annual dues" + (" (Disney requires at closing)" if when == closing else "")
        if y == best["sale_close"].year:
            lbl += " — buyer reimburses unused months at sale"
        cash.append({"date": when, "what": lbl, "amount": -annual_dues(y) if y != best["sale_close"].year else -annual_dues(y)})
    # refund of the unused part of the sale-year dues
    sy = [r for r in best["dues_rows"] if r[0] == best["sale_close"].year]
    if sy and sy[0][0] != closing.year:
        cash.append({"date": best["sale_close"], "what": "Dues reimbursement from buyer (unused months)",
                     "amount": annual_dues(sy[0][0]) - sy[0][1]})
    cash.append({"date": book_on, "what": f"Book {scenario['name'].lower()} ({'home 11-mo' if aulani_home else '7-mo'} window)", "amount": 0})
    cash.append({"date": ci, "what": f"Check in ({need} pts; est. Hawaii occupancy tax ${tat:,.0f} paid at checkout)", "amount": -tat})
    cash.append({"date": best["list_on"], "what": f"List for sale ({best['exit']})", "amount": 0})
    withheld = best["gross_sale"] * harpta
    cash.append({"date": best["sale_close"], "what": "Sale closes: proceeds after commission" + (" and HARPTA withholding" if harpta else ""),
                 "amount": best["net_sale"] - withheld})
    if harpta:
        cash.append({"date": date(best["sale_close"].year + 1, 4, 15), "what": "HARPTA refund via Hawaii tax return (est.)",
                     "amount": withheld})
    cash.sort(key=lambda c: c["date"])
    peak_out, run = 0.0, 0.0
    for c in cash:
        run += c["amount"]
        peak_out = min(peak_out, run)

    return {
        "trip": trip["id"], "trip_name": trip["name"], "nights": nights, "scenario": scenario["id"],
        "scenario_name": scenario["name"], "view": view, "periods": pnames,
        "need": need, "uy_start": s0.isoformat(), "cur": cur, "cur_assumed": cur_assumed,
        "bankable": bankable, "bank_deadline": bank_deadline.isoformat(), "bank_used": bank_used,
        "bank_margin_days": bank_margin,
        "next_uy": nxt, "next_assumed": nxt_assumed, "usable_no_borrow": usable_nb, "usable_with_borrow": usable_b,
        "fit": fit, "borrow": borrow, "leftover": leftover, "lost_prior": lost_prior,
        "closing_est": closing.isoformat(), "member_ready": ready.isoformat(),
        "window_opens": window.isoformat(), "book_on": book_on.isoformat(), "days_late": max(0, late),
        "booking_risk": brisk,
        "price": price, "closing_cost": closing_cost, "caf": caf, "dues_rate_2026": rate26,
        "exit": best["exit"], "list_on": best["list_on"].isoformat(), "sale_close": best["sale_close"].isoformat(),
        "exit_ppt": round(best["exit_ppt"], 1), "market_ppt": market, "gross_sale": round(best["gross_sale"]),
        "net_sale": round(best["net_sale"]), "dues_total": round(best["dues_total"]),
        "dues_rows": best["dues_rows"], "buy_total": round(best["buy_total"]),
        "net_cost": round(best["net_cost"]), "rent_cost": round(rent_cost), "savings": round(savings),
        "savings_pct": round(100 * savings / rent_cost, 1) if rent_cost else 0,
        "breakeven_exit_ppt": round(breakeven, 1) if breakeven else None,
        "leftover_value": round(leftover_value), "harpta_withheld": round(withheld),
        "peak_cash_out": round(-peak_out), "tat_est": round(tat),
        "cash": [{**c, "date": c["date"].isoformat(), "amount": round(c["amount"])} for c in cash],
    }


def score(d: dict, plans: list[dict], rofr: str, cfg: dict) -> tuple[float, dict]:
    trip_w = {t["id"]: (1.0 if i == 0 else 0.85) for i, t in enumerate(cfg["trips"])}
    best_cov, best_val, best_time, best_plan = 0, 0, 0, None
    for p in plans:
        w = trip_w.get(p["trip"], 0.8)
        if p["fit"] == "no":
            continue
        cov = (35 if p["scenario"] == "both" else 20) * (1 if p["fit"] == "yes" else 0.8) * (1 if p["nights"] == 6 else 0.95) * w
        val = max(0, min(35, (p["savings_pct"] + 40) / 100 * 35))  # -40% -> 0, +60% -> 35
        tim = {"low": 10, "medium": 5, "high": 0}[p["booking_risk"]]
        total = cov + val + tim
        if best_plan is None or total > best_cov + best_val + best_time:
            best_cov, best_val, best_time, best_plan = cov, val, tim, p
    risk = {"low": 10, "medium": 5, "high": 0}[rofr]
    conf = 0
    conf += 3 if d.get("detail_fetched") else 0
    conf += 2 if not (d.get("extra") or {}).get("availability_incomplete") and d.get("points_available") else 0
    conf += 1 if d.get("dues_terms") else 0
    stale = 0 if (d.get("stale") or (d.get("extra") or {}).get("aggregator_only")) else 4
    s = best_cov + best_val + best_time + risk + conf + stale
    return round(s, 1), {"coverage": round(best_cov, 1), "value": round(best_val, 1), "timing": best_time,
                         "rofr": risk, "data": conf, "freshness": stale,
                         "best": best_plan and f"{best_plan['trip']}/{best_plan['nights']}n/{best_plan['scenario']}"}


def run(today: date | None = None) -> dict:
    today = today or date.today()
    cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml")))
    periods, resorts, fees = load_ref()
    L = json.load(open(os.path.join(ROOT, "data", "listings.json")))
    out = []
    for d in L["listings"]:
        if not d.get("active") or d.get("availability") != "available":
            continue
        if cfg.get("legacy_only", True) and not is_legacy(d.get("resort")):
            continue
        if not d.get("points") or not d.get("use_year") or not d.get("price"):
            continue
        if d["points"] < cfg.get("min_points", 0):
            continue
        rref = resorts.get(d["resort"], {})
        plans = []
        for t in cfg["trips"]:
            for n in t["nights"]:
                for sc in cfg["scenarios"]:
                    try:
                        plans.append(plan(d, t, n, sc, cfg, periods, rref, fees, today))
                    except Exception as e:  # never let one bad listing kill the run
                        plans.append({"trip": t["id"], "nights": n, "scenario": sc["id"], "fit": "no", "error": str(e)})
        rofr, rofr_why = rofr_risk(d, rref)
        sc, parts = score(d, plans, rofr, cfg)
        good = [p for p in plans if p.get("fit") != "no" and "error" not in p]
        if good and good[0].get("buy_total", 0) > cfg.get("max_total_cost", 1e9):
            sc = round(sc * 0.5, 1)
            parts["over_budget"] = True
        out.append({
            "uid": d["uid"], "source": d["source"], "url": d.get("url"), "listing_id": d["listing_id"],
            "resort": d["resort"], "resort_name": display(d["resort"]), "points": d["points"],
            "price": d["price"], "ppt": d.get("price_per_point"), "use_year": d["use_year"],
            "status": d.get("status_raw"), "first_seen": d.get("first_seen"), "date_listed": d.get("date_listed"),
            "points_available": d.get("points_available"), "availability_text": d.get("availability_text"),
            "dues_terms": d.get("dues_terms"), "dues_per_point": d.get("dues_per_point"),
            "subsidized": bool(d.get("subsidized_dues")), "expiration": d.get("expiration"),
            "detail_fetched": d.get("detail_fetched"), "broker": (d.get("extra") or {}).get("broker"),
            "aggregator_only": bool((d.get("extra") or {}).get("aggregator_only")),
            "history": d.get("history", [])[-6:],
            "rofr": rofr, "rofr_why": rofr_why, "score": sc, "score_parts": parts,
            "plans": [p for p in plans if "error" not in p],
        })
    out.sort(key=lambda x: -x["score"])
    for i, x in enumerate(out, 1):
        x["rank"] = i
    write_outputs(out, cfg, today)
    return {"listings": out}


SUMMARY_KEYS = ("trip", "nights", "scenario", "need", "fit", "borrow", "usable_no_borrow", "usable_with_borrow",
                "book_on", "booking_risk", "net_cost", "rent_cost", "savings", "savings_pct", "exit",
                "breakeven_exit_ppt", "peak_cash_out", "leftover")


def write_outputs(out: list[dict], cfg: dict, today: date) -> None:
    """index.json: compact rows for the table. l/<id>.json: full plans + cash timelines."""
    base = os.path.join(ROOT, "docs", "data")
    ldir = os.path.join(base, "l")
    os.makedirs(ldir, exist_ok=True)
    rows, keep = [], set()
    for x in out:
        fid = re.sub(r"[^A-Za-z0-9_-]", "_", x["uid"])
        keep.add(fid + ".json")
        row = {k: v for k, v in x.items() if k not in ("plans", "history", "availability_text", "dues_terms")}
        row["fid"] = fid
        prices = [h.get("price") for h in x.get("history") or [] if h.get("price")]
        row["prev_price"] = max(prices) if prices and max(prices) > (x["price"] or 0) else None
        row["plans"] = [[p.get(k) for k in SUMMARY_KEYS] for p in x["plans"]]
        rows.append(row)
        detail = {k: x[k] for k in ("uid", "history", "availability_text", "dues_terms", "plans")}
        with open(os.path.join(ldir, fid + ".json"), "w") as f:
            json.dump(detail, f, default=str, separators=(",", ":"))
    for fn in os.listdir(ldir):
        if fn not in keep:
            os.remove(os.path.join(ldir, fn))
    with open(os.path.join(base, "index.json"), "w") as f:
        json.dump({"today": today.isoformat(), "config": cfg, "plan_keys": SUMMARY_KEYS,
                   "count": len(rows), "listings": rows},
                  f, default=str, separators=(",", ":"))


if __name__ == "__main__":
    doc = run()
    for x in doc["listings"][:15]:
        print(x["rank"], x["score"], x["uid"], x["resort"], x["points"], x["ppt"], x["score_parts"]["best"], x["rofr"])
