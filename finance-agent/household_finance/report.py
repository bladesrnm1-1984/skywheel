"""Plain Markdown report built only from the analysis functions (no AI, no network)."""

from __future__ import annotations

from . import analysis
from .data import HouseholdConfig, Transaction


def _money(x: float | None) -> str:
    return "-" if x is None else f"${x:,.2f}"


def offline_report(txns: list[Transaction], config: HouseholdConfig) -> str:
    ov = analysis.overview(txns)
    subs = analysis.find_subscriptions(txns, config)
    debts = analysis.track_debts(txns, config)
    cmp_ = analysis.compare_recurring(txns)
    unusual = analysis.find_unusual(txns, config)
    out: list[str] = ["# Household finance report (offline)", ""]
    out.append(f"Data: {ov['transactions']} transactions, {ov['date_range'][0]} to {ov['date_range'][1]}, "
               f"accounts: {', '.join(ov['accounts']) or '-'}.")
    out.append("")

    out += ["## Subscriptions", "",
            f"{subs['active_count']} active subscriptions costing about {_money(subs['active_monthly_cost'])}"
            f"/month ({_money(subs['active_annual_cost'])}/year).", "",
            "| Merchant | Latest | Cadence | Score | Why review |", "|---|---|---|---|---|"]
    for s in subs["subscriptions"]:
        if s["active"]:
            out.append(f"| {s['merchant']} | {_money(s['latest_amount'])} | {s['cadence']} | "
                       f"{s['forgotten_score']} | {' '.join(s['reasons']) or '-'} |")
    if subs["possible_annual_plans"]:
        out.append("")
    for a in subs["possible_annual_plans"]:
        out.append(f"- Possible annual plan: {a['merchant']} {_money(-a['amount'])} on {a['date']}, "
                   f"may renew around {a['renews_around']}")
    if subs["stopped"]:
        out += ["", f"Stopped: {', '.join(subs['stopped'])}"]
    out.append("")

    out += ["## Debt payments", ""]
    for d in debts["debts"]:
        out.append(f"- **{d['name']}**: paid {_money(d['total_paid'])} "
                   f"(avg {_money(d['average_monthly_payment'])}/month); balance {_money(d['balance'])}"
                   + (f", APR {d['apr']}%" if d["apr"] is not None else "")
                   + (f"; payoff ~{d['payoff_months_at_current_pace']} months"
                      if d.get("payoff_months_at_current_pace") else "")
                   + (". " + " ".join(d["notes"]) if d["notes"] else ""))
    for o in debts["unconfigured_debt_payments"]:
        out.append(f"- {o['merchant']} (not configured): {o['payments']} payments, {_money(o['total_paid'])}")
    if debts["avalanche_order"]:
        out.append(f"- Highest-APR-first payoff order: {' > '.join(debts['avalanche_order'])}")
    out.append(f"- Interest and fees charged: {_money(debts['interest_and_fees_total'])}")
    out.append("")

    out += ["## Recurring expenses: month over month", ""]
    if "error" in cmp_:
        out.append(cmp_["error"])
    else:
        a, b = cmp_["month_a"], cmp_["month_b"]
        t = cmp_["total_recurring"]
        out += [f"Recurring total {a}: {_money(t[a])} -> {b}: {_money(t[b])} ({_money(t['change'])}).", "",
                f"| Merchant | {a} | {b} | Change | Status |", "|---|---|---|---|---|"]
        for r in cmp_["items"]:
            if r["status"] != "same":
                out.append(f"| {r['merchant']} | {_money(r[a])} | {_money(r[b])} | {_money(r['change'])} | "
                           f"{r['status']} |")
    out.append("")

    out += ["## Unusual spending", ""]
    for o in unusual["outlier_transactions"]:
        out.append(f"- {o['date']} {o['merchant']} {_money(-o['amount'])} "
                   f"(usual {_money(o['typical_amount'])} for this {o['compared_to']})")
    for s in unusual["category_spikes"]:
        out.append(f"- {s['category']} in {s['month']}: {_money(s['spent'])} vs avg {_money(s['prior_average'])}")
    for d in unusual["possible_duplicate_charges"]:
        out.append(f"- Possible duplicate: {d['merchant']} {_money(d['amount'])} on {' and '.join(d['dates'])}")
    for n in unusual["new_merchants_large_charges"]:
        out.append(f"- New merchant, large charge: {n['date']} {n['merchant']} {_money(-n['amount'])}")
    for f in unusual["fees_and_interest"]:
        out.append(f"- Fee/interest: {f['date']} {f['description']} {_money(-f['amount'])}")
    return "\n".join(out) + "\n"
