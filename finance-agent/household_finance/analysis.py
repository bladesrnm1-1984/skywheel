"""Deterministic analyses the agent calls as tools.

Every function takes the loaded transactions and returns plain JSON-serialisable dicts,
so results can be tested without the model and handed to Claude verbatim.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta

from .data import SUBSCRIPTION_CATEGORIES, HouseholdConfig, Transaction

CADENCES = [
    # name, nominal days, tolerance (days), periods per year
    ("weekly", 7, 2, 52),
    ("biweekly", 14, 3, 26),
    ("monthly", 30.4, 4, 12),
    ("quarterly", 91, 10, 4),
    ("semiannual", 182, 15, 2),
    ("annual", 365, 20, 1),
]

# Categories where having several services at once is worth questioning.
OVERLAP_CATEGORIES = {"Streaming & Media", "Fitness", "Gaming", "News & Reading"}

NON_SUBSCRIPTION_CATEGORIES = {"Debt Payments", "Utilities", "Insurance", "Housing", "Income", "Transfers",
                               "Groceries", "Transport"}


def _month(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _r(x: float) -> float:
    return round(x, 2)


def _months_between(start: date, end: date) -> list[str]:
    months, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def _complete_months(txns: list[Transaction]) -> list[str]:
    """Months fully covered by the data (drops a partial first/last month)."""
    if not txns:
        return []
    start, end = txns[0].date, txns[-1].date
    months = _months_between(start, end)
    if start.day > 3 and len(months) > 1:
        months = months[1:]
    next_day = end + timedelta(days=1)
    if next_day.month == end.month and len(months) > 1:  # data ends before month end
        months = months[:-1]
    return months


def filter_txns(txns: list[Transaction], start: str | None = None, end: str | None = None) -> list[Transaction]:
    s = date.fromisoformat(start) if start else date.min
    e = date.fromisoformat(end) if end else date.max
    return [t for t in txns if s <= t.date <= e]


# --------------------------------------------------------------------------- overview

def overview(txns: list[Transaction]) -> dict:
    if not txns:
        return {"transactions": 0}
    by_month: dict[str, dict[str, float]] = defaultdict(lambda: {"income": 0.0, "spending": 0.0})
    by_category: dict[str, float] = defaultdict(float)
    for t in txns:
        m = by_month[_month(t.date)]
        if t.category == "Transfers":
            continue
        if t.amount > 0:
            m["income"] += t.amount
        else:
            m["spending"] += t.spend
            by_category[t.category] += t.spend
    months = sorted(by_month)
    return {
        "transactions": len(txns),
        "date_range": [txns[0].date.isoformat(), txns[-1].date.isoformat()],
        "complete_months": _complete_months(txns),
        "accounts": sorted({t.account for t in txns if t.account}),
        "monthly": [
            {"month": m, "income": _r(by_month[m]["income"]), "spending": _r(by_month[m]["spending"]),
             "net": _r(by_month[m]["income"] - by_month[m]["spending"])}
            for m in months
        ],
        "spending_by_category": {k: _r(v) for k, v in sorted(by_category.items(), key=lambda kv: -kv[1])},
        "note": "Transfers between accounts are excluded from income and spending.",
    }


def monthly_by_category(txns: list[Transaction], months: int = 6) -> dict:
    table: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in txns:
        if t.is_outflow and t.category != "Transfers":
            table[t.category][_month(t.date)] += t.spend
    all_months = sorted({_month(t.date) for t in txns})[-months:]
    rows = []
    for cat, per_month in table.items():
        values = [per_month.get(m, 0.0) for m in all_months]
        rows.append({"category": cat, **{m: _r(v) for m, v in zip(all_months, values)},
                     "average": _r(sum(values) / len(values)) if values else 0})
    rows.sort(key=lambda r: -r["average"])
    return {"months": all_months, "rows": rows}


def search(txns: list[Transaction], query: str = "", start: str | None = None, end: str | None = None,
           category: str = "", min_amount: float = 0.0, limit: int = 50) -> dict:
    q = query.lower()
    hits = [
        t for t in filter_txns(txns, start, end)
        if (not q or q in t.description.lower() or q in t.merchant.lower())
        and (not category or category.lower() == t.category.lower())
        and abs(t.amount) >= min_amount
    ]
    return {
        "matches": len(hits),
        "total_out": _r(sum(t.spend for t in hits)),
        "total_in": _r(sum(t.amount for t in hits if t.amount > 0)),
        "transactions": [t.to_dict() for t in hits[-limit:]],
        "truncated": len(hits) > limit,
    }


# --------------------------------------------------------------------------- recurring

def _classify_cadence(intervals: list[int]) -> tuple[str, float, int] | None:
    if not intervals:
        return None
    med = statistics.median(intervals)
    for name, nominal, tol, per_year in CADENCES:
        if abs(med - nominal) <= tol:
            regular = sum(1 for i in intervals if abs(i - nominal) <= tol * 1.5) / len(intervals)
            if regular >= 0.6:
                return name, nominal, per_year
    return None


def find_recurring(txns: list[Transaction], min_occurrences: int = 3) -> list[dict]:
    """Detect charges that repeat on a regular cadence from the same merchant."""
    if not txns:
        return []
    data_end = txns[-1].date
    groups: dict[str, list[Transaction]] = defaultdict(list)
    for t in txns:
        if t.is_outflow and t.category not in ("Transfers",):
            groups[t.merchant].append(t)

    results = []
    for merchant, items in groups.items():
        items.sort(key=lambda t: t.date)
        # Collapse same-day charges (split payments) into one event for cadence detection.
        events: list[tuple[date, float]] = []
        for t in items:
            if events and events[-1][0] == t.date:
                events[-1] = (t.date, events[-1][1] + t.spend)
            else:
                events.append((t.date, t.spend))
        annual_candidate = len(events) == 2 and 340 <= (events[1][0] - events[0][0]).days <= 390
        if len(events) < min_occurrences and not annual_candidate:
            continue
        intervals = [(b[0] - a[0]).days for a, b in zip(events, events[1:])]
        cadence = _classify_cadence(intervals)
        if not cadence:
            continue
        name, nominal, per_year = cadence
        amounts = [a for _, a in events]
        mean_amt = statistics.mean(amounts)
        cv = statistics.pstdev(amounts) / mean_amt if mean_amt else 0
        last_date = events[-1][0]
        next_expected = last_date + timedelta(days=round(nominal))
        active = (data_end - last_date).days <= nominal * 1.5 + 5
        first_amt, last_amt = amounts[0], amounts[-1]
        category = items[-1].category
        results.append({
            "merchant": merchant,
            "example_description": items[-1].description,
            "category": category,
            "cadence": name,
            "occurrences": len(events),
            "first_charge": events[0][0].isoformat(),
            "last_charge": last_date.isoformat(),
            "next_expected": next_expected.isoformat() if active else None,
            "active": active,
            "latest_amount": _r(last_amt),
            "first_amount": _r(first_amt),
            "average_amount": _r(mean_amt),
            "fixed_amount": cv < 0.05,
            "amount_variation_pct": _r(cv * 100),
            "annualized_cost": _r(mean_amt * per_year) if active else 0.0,
            "price_change_pct": _r((last_amt - first_amt) / first_amt * 100) if first_amt else 0.0,
            "total_paid_in_data": _r(sum(amounts)),
            "accounts": sorted({t.account for t in items if t.account}),
        })
    results.sort(key=lambda r: -r["annualized_cost"])
    return results


# --------------------------------------------------------------------------- subscriptions

def find_subscriptions(txns: list[Transaction], config: HouseholdConfig | None = None) -> dict:
    """Recurring fixed-ish charges that look like subscriptions, scored for 'possibly forgotten'."""
    config = config or HouseholdConfig()
    if not txns:
        return {"subscriptions": []}
    data_end = txns[-1].date
    recurring = find_recurring(txns, min_occurrences=2)
    subs = [r for r in recurring
            if r["category"] not in NON_SUBSCRIPTION_CATEGORIES
            and (r["category"] in SUBSCRIPTION_CATEGORIES or r["amount_variation_pct"] < 10)
            and r["average_amount"] < 500]

    by_category: dict[str, list[dict]] = defaultdict(list)
    for s in subs:
        if s["active"]:
            by_category[s["category"]].append(s)

    flagged = []
    for s in subs:
        reasons, score = [], 0
        known = any(k in s["merchant"].lower() or k in s["example_description"].lower()
                    for k in config.known_subscriptions)
        if config.known_subscriptions and not known and s["active"]:
            reasons.append("Not on your list of known subscriptions.")
            score += 3
        if s["price_change_pct"] >= 5:
            reasons.append(f"Price went up {s['price_change_pct']:.0f}% "
                           f"(${s['first_amount']:.2f} -> ${s['latest_amount']:.2f}) - easy to miss.")
            score += 2
        first = date.fromisoformat(s["first_charge"])
        if s["active"] and (data_end - first).days <= 75:
            reasons.append("Started recently - possibly a free trial that converted to paid.")
            score += 2
        if s["active"] and s["latest_amount"] < 20 and s["occurrences"] >= 6:
            reasons.append("Small charge that has quietly renewed for a long time.")
            score += 1
        peers = [p for p in by_category.get(s["category"], []) if p["merchant"] != s["merchant"]]
        if s["active"] and peers and s["category"] in OVERLAP_CATEGORIES:
            reasons.append(f"Overlaps with {len(peers)} other active {s['category']} subscription(s): "
                           + ", ".join(p["merchant"] for p in peers[:4]) + ".")
            score += 1
        if s["active"] and len(s["accounts"]) > 1:
            reasons.append("Billed to more than one account/card - possible duplicate signup.")
            score += 2
        brand = s["merchant"].split()[0]
        twins = [p["merchant"] for p in subs if p is not s and p["active"] and p["merchant"].split()[0] == brand]
        if s["active"] and twins and len(brand) >= 4:
            reasons.append(f"Looks like the same service billed twice (also: {', '.join(twins)}).")
            score += 3
        if s["cadence"] == "annual" and s["active"] and s["next_expected"]:
            days = (date.fromisoformat(s["next_expected"]) - data_end).days
            if 0 <= days <= 45:
                reasons.append(f"Annual renewal due in about {days} days (~${s['latest_amount']:.2f}).")
                score += 2
        flagged.append({**s, "forgotten_score": score, "reasons": reasons})

    flagged.sort(key=lambda s: (-s["active"], -s["forgotten_score"], -s["annualized_cost"]))
    active = [s for s in flagged if s["active"]]

    # One-off charges at subscription-type merchants: often annual plans seen only once in the data.
    recurring_names = {r["merchant"] for r in recurring}
    counts: dict[str, int] = defaultdict(int)
    for t in txns:
        if t.is_outflow:
            counts[t.merchant] += 1
    possible_annual = [
        {**t.to_dict(), "renews_around": (t.date + timedelta(days=365)).isoformat()}
        for t in txns
        if t.is_outflow and t.category in SUBSCRIPTION_CATEGORIES and t.spend >= 20
        and t.merchant not in recurring_names and counts[t.merchant] == 1
    ]
    return {
        "active_count": len(active),
        "active_monthly_cost": _r(sum(s["annualized_cost"] for s in active) / 12),
        "active_annual_cost": _r(sum(s["annualized_cost"] for s in active)),
        "subscriptions": flagged,
        "stopped": [s["merchant"] for s in flagged if not s["active"]],
        "possible_annual_plans": possible_annual,
        "scoring": "forgotten_score: higher = more likely forgotten or worth reviewing.",
    }


# --------------------------------------------------------------------------- debt

def _payoff_months(balance: float, apr: float, payment: float) -> tuple[int | None, float | None]:
    """Months to pay off and total interest, simulating monthly compounding. (None, None) if never."""
    r = apr / 100 / 12
    if payment <= 0 or payment <= balance * r:
        return None, None
    months, interest_total = 0, 0.0
    while balance > 0.005 and months < 1200:
        interest = balance * r
        interest_total += interest
        balance = balance + interest - payment
        months += 1
    return months, _r(interest_total)


def track_debts(txns: list[Transaction], config: HouseholdConfig | None = None) -> dict:
    config = config or HouseholdConfig()
    months = _complete_months(txns)
    results, matched_ids = [], set()

    for debt in config.debts:
        pays = [t for t in txns if t.is_outflow and any(m in t.description.lower() for m in debt.match)]
        matched_ids.update(id(t) for t in pays)
        per_month: dict[str, float] = defaultdict(float)
        for t in pays:
            per_month[_month(t.date)] += t.spend
        missed = [m for m in months if per_month.get(m, 0) == 0]
        paid_months = [per_month[m] for m in months if per_month.get(m, 0) > 0]
        avg = statistics.mean(paid_months) if paid_months else 0.0
        entry = {
            "name": debt.name,
            "kind": debt.kind,
            "payments_found": len(pays),
            "total_paid": _r(sum(t.spend for t in pays)),
            "average_monthly_payment": _r(avg),
            "monthly_payments": {m: _r(per_month.get(m, 0.0)) for m in months},
            "months_with_no_payment": missed,
            "last_payment": pays[-1].to_dict() if pays else None,
            "balance": debt.balance,
            "apr": debt.apr,
            "minimum_payment": debt.minimum_payment,
        }
        notes = []
        if debt.minimum_payment and paid_months:
            under = [m for m in months if 0 < per_month.get(m, 0) < debt.minimum_payment - 0.01]
            if under:
                notes.append(f"Paid less than the ${debt.minimum_payment:.2f} minimum in: {', '.join(under)}.")
            high_interest = (debt.apr or 0) >= 12 or "card" in debt.kind.lower()
            if high_interest and avg <= debt.minimum_payment * 1.05:
                notes.append("Paying only about the minimum on a high-interest debt.")
        if missed:
            notes.append(f"No payment found in {len(missed)} month(s): {', '.join(missed)}.")
        if debt.balance and debt.apr is not None:
            interest = debt.balance * debt.apr / 100 / 12
            entry["estimated_monthly_interest"] = _r(interest)
            n, total_interest = _payoff_months(debt.balance, debt.apr, avg)
            entry["payoff_months_at_current_pace"] = n
            entry["total_interest_at_current_pace"] = total_interest
            if n is None and avg > 0:
                notes.append("At the current payment the balance never goes down (payment <= monthly interest).")
            extra = avg + 100
            n2, ti2 = _payoff_months(debt.balance, debt.apr, extra)
            if n2 is not None:
                entry["if_paid_100_more_per_month"] = {
                    "payoff_months": n2,
                    "total_interest": ti2,
                    "interest_saved": _r(total_interest - ti2) if total_interest is not None else None,
                }
        entry["notes"] = notes
        results.append(entry)

    # Debt-like payments that don't match a configured debt.
    unconfigured: dict[str, list[Transaction]] = defaultdict(list)
    for t in txns:
        if t.is_outflow and t.category == "Debt Payments" and id(t) not in matched_ids:
            unconfigured[t.merchant].append(t)
    other = [
        {"merchant": m, "payments": len(ts), "total_paid": _r(sum(t.spend for t in ts)),
         "last_payment": ts[-1].date.isoformat(), "last_amount": _r(ts[-1].spend)}
        for m, ts in sorted(unconfigured.items(), key=lambda kv: -sum(t.spend for t in kv[1]))
    ]
    fees = [t.to_dict() for t in txns if t.is_outflow and t.category == "Fees & Interest"]

    total_balance = sum(d.balance or 0 for d in config.debts)
    avalanche = sorted([d for d in config.debts if d.balance], key=lambda d: -(d.apr or 0))
    return {
        "debts": results,
        "total_known_balance": _r(total_balance),
        "total_paid_toward_debt": _r(sum(r["total_paid"] for r in results) + sum(o["total_paid"] for o in other)),
        "avalanche_order": [d.name for d in avalanche],
        "unconfigured_debt_payments": other,
        "interest_and_fees_charged": fees,
        "interest_and_fees_total": _r(sum(-f["amount"] for f in fees)),
        "hint": None if config.debts else
        "No debts configured - add balances/APRs in a config file for payoff projections.",
    }


# --------------------------------------------------------------------------- recurring comparison

def compare_recurring(txns: list[Transaction], month_a: str | None = None, month_b: str | None = None) -> dict:
    """Compare recurring bills between two months (defaults: last two complete months)."""
    months = _complete_months(txns)
    if not month_b:
        if len(months) < 2:
            return {"error": "Need at least two complete months of data."}
        month_a, month_b = months[-2], months[-1]
    elif not month_a:
        idx = months.index(month_b) if month_b in months else len(months)
        month_a = months[idx - 1] if idx > 0 else None
    if not month_a:
        return {"error": f"No complete month before {month_b} to compare against."}
    recurring = [r for r in find_recurring(txns) if r["cadence"] in ("weekly", "biweekly", "monthly")]
    names = {r["merchant"]: r for r in recurring}
    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in txns:
        if t.is_outflow and t.merchant in names:
            totals[t.merchant][_month(t.date)] += t.spend

    rows, a_total, b_total = [], 0.0, 0.0
    for merchant, per_month in totals.items():
        a, b = per_month.get(month_a, 0.0), per_month.get(month_b, 0.0)
        history = [per_month.get(m, 0.0) for m in months if m < month_b]
        hist_nonzero = [h for h in history if h > 0]
        typical = statistics.median(hist_nonzero) if hist_nonzero else 0.0
        a_total += a
        b_total += b
        if a == 0 and b == 0:
            continue
        status = "new" if a == 0 else "missing" if b == 0 else "changed" if abs(b - a) >= 1 else "same"
        rows.append({
            "merchant": merchant, "category": names[merchant]["category"],
            month_a: _r(a), month_b: _r(b), "change": _r(b - a),
            "change_pct": _r((b - a) / a * 100) if a else None,
            "typical_month": _r(typical),
            "vs_typical_pct": _r((b - typical) / typical * 100) if typical else None,
            "status": status,
        })
    rows.sort(key=lambda r: -abs(r["change"]))
    by_cat: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        by_cat[r["category"]][0] += r[month_a]
        by_cat[r["category"]][1] += r[month_b]
    return {
        "month_a": month_a, "month_b": month_b,
        "total_recurring": {month_a: _r(a_total), month_b: _r(b_total), "change": _r(b_total - a_total)},
        "by_category": {c: {month_a: _r(v[0]), month_b: _r(v[1]), "change": _r(v[1] - v[0])}
                        for c, v in sorted(by_cat.items(), key=lambda kv: -abs(kv[1][1] - kv[1][0]))},
        "items": rows,
    }


# --------------------------------------------------------------------------- unusual spending

def _robust_z(value: float, sample: list[float]) -> float:
    med = statistics.median(sample)
    mad = statistics.median([abs(x - med) for x in sample]) or 0.0
    scale = 1.4826 * mad if mad else (statistics.pstdev(sample) or max(med * 0.25, 1.0))
    return (value - med) / scale


def find_unusual(txns: list[Transaction], config: HouseholdConfig | None = None, sensitivity: float = 3.0,
                 recent_days: int = 90) -> dict:
    """Flag outlier transactions, category spikes, duplicates, new big merchants and fees."""
    config = config or HouseholdConfig()
    if not txns:
        return {}
    data_end = txns[-1].date
    recent_start = data_end - timedelta(days=recent_days)
    outflows = [t for t in txns if t.is_outflow and t.category not in ("Transfers",)]

    # 1) Transactions far above that merchant's (or category's) usual amount.
    by_merchant: dict[str, list[Transaction]] = defaultdict(list)
    by_category: dict[str, list[float]] = defaultdict(list)
    for t in outflows:
        by_merchant[t.merchant].append(t)
        by_category[t.category].append(t.spend)
    outliers = []
    for t in outflows:
        if t.date < recent_start:
            continue
        peers = [p.spend for p in by_merchant[t.merchant] if p is not t]
        basis = "merchant"
        if len(peers) < 3:
            peers = [x for x in by_category[t.category] if x != t.spend]
            basis = "category"
        if len(peers) < 5 or t.spend < 25:
            continue
        z = _robust_z(t.spend, peers)
        if z >= sensitivity and t.spend >= 2 * statistics.median(peers):
            outliers.append({**t.to_dict(), "typical_amount": _r(statistics.median(peers)),
                             "compared_to": basis, "score": _r(z)})
    outliers.sort(key=lambda o: -o["score"])

    # 2) Category spikes: latest complete month vs prior months.
    months = _complete_months(txns)
    spikes = []
    if len(months) >= 3:
        latest, prior = months[-1], months[:-1]
        cat_month: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for t in outflows:
            cat_month[t.category][_month(t.date)] += t.spend
        for cat, pm in cat_month.items():
            hist = [pm.get(m, 0.0) for m in prior]
            cur = pm.get(latest, 0.0)
            avg = statistics.mean(hist)
            sd = statistics.pstdev(hist)
            if cur - avg >= 75 and (cur >= avg * 1.4) and (sd == 0 or (cur - avg) / sd >= 1.5):
                spikes.append({"category": cat, "month": latest, "spent": _r(cur), "prior_average": _r(avg),
                               "increase": _r(cur - avg), "increase_pct": _r((cur - avg) / avg * 100) if avg else None})
        spikes.sort(key=lambda s: -s["increase"])

    # 3) Possible duplicate charges (same merchant + amount within 3 days).
    duplicates = []
    for merchant, items in by_merchant.items():
        items = sorted(items, key=lambda t: t.date)
        for a, b in zip(items, items[1:]):
            if abs(a.spend - b.spend) < 0.01 and 0 <= (b.date - a.date).days <= 3 and a.spend >= 5:
                if a.category in ("Groceries", "Dining", "Transport") and a.spend < 40:
                    continue  # repeated coffee/gas is normal
                duplicates.append({"merchant": merchant, "amount": _r(a.spend),
                                   "dates": [a.date.isoformat(), b.date.isoformat()],
                                   "descriptions": [a.description, b.description]})

    # 4) First-time merchants with large charges.
    first_seen: dict[str, Transaction] = {}
    for t in outflows:
        first_seen.setdefault(t.merchant, t)
    new_large = [
        {**t.to_dict()} for m, t in first_seen.items()
        if t.date >= recent_start and t.spend >= config.large_purchase_threshold
        and t.date > txns[0].date + timedelta(days=30)
    ]
    new_large.sort(key=lambda t: t["amount"])

    # 5) Fees and interest.
    fees = [t.to_dict() for t in outflows if t.category == "Fees & Interest" and t.date >= recent_start]

    return {
        "window": [recent_start.isoformat(), data_end.isoformat()],
        "outlier_transactions": outliers[:25],
        "category_spikes": spikes,
        "possible_duplicate_charges": duplicates,
        "new_merchants_large_charges": new_large,
        "fees_and_interest": fees,
    }
