"""Tests run against the generated sample data, which has known issues planted in it."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from household_finance import analysis  # noqa: E402
from household_finance.data import load_config, load_transactions, normalize_merchant  # noqa: E402

SAMPLE = ROOT / "sample_data"


@pytest.fixture(scope="module")
def data():
    subprocess.run([sys.executable, str(SAMPLE / "make_sample.py")], check=True, capture_output=True)
    return load_transactions(SAMPLE / "transactions.csv"), load_config(SAMPLE / "household.json")


@pytest.mark.parametrize("raw,expected", [
    ("SQ *BLUE BOTTLE COFFEE", "BLUE BOTTLE COFFEE"),
    ("HULU 877-8244858 CA", "HULU"),
    ("PLANET FITNESS #1123", "PLANET FITNESS"),
    ("CITY ELECTRIC CO", "CITY ELECTRIC CO"),
    ("SPOTIFY P1A2B3C", "SPOTIFY"),
])
def test_normalize_merchant(raw, expected):
    assert normalize_merchant(raw) == expected


def test_subscriptions(data):
    txns, config = data
    subs = analysis.find_subscriptions(txns, config)
    by_name = {s["merchant"]: s for s in subs["subscriptions"]}
    assert by_name["HULU"]["price_change_pct"] == pytest.approx(20.01, abs=0.1)
    assert any("trial" in r for r in by_name["PEACOCK TV"]["reasons"])
    assert any("billed twice" in r for r in by_name["SPOTIFY"]["reasons"])
    assert any("known subscriptions" in r for r in by_name["CALM APP"]["reasons"])
    assert "ADOBE CREATIVE CLOUD" in subs["stopped"]
    assert "OAK RIDGE APARTMENTS" not in by_name  # rent is not a subscription
    assert any(a["merchant"].startswith("AMAZON PRIME") for a in subs["possible_annual_plans"])


def test_debts(data):
    txns, config = data
    debts = {d["name"]: d for d in analysis.track_debts(txns, config)["debts"]}
    assert debts["Navient student loan"]["months_with_no_payment"] == ["2026-08"]
    assert debts["Capital One Visa"]["payoff_months_at_current_pace"] > 60
    assert debts["Capital One Visa"]["if_paid_100_more_per_month"]["interest_saved"] > 1000
    assert any("minimum" in n for n in debts["Capital One Visa"]["notes"])
    assert not any("minimum" in n for n in debts["Toyota auto loan"]["notes"])


def test_payoff_math():
    n, interest = analysis._payoff_months(1000, 12, 100)
    assert n == 11 and 50 < interest < 70
    assert analysis._payoff_months(1000, 24, 10) == (None, None)


def test_compare_recurring(data):
    txns, _ = data
    cmp_ = analysis.compare_recurring(txns)
    assert (cmp_["month_a"], cmp_["month_b"]) == ("2026-09", "2026-10")
    electric = next(i for i in cmp_["items"] if i["merchant"] == "CITY ELECTRIC CO")
    assert electric["change"] > 100
    assert analysis.compare_recurring(txns, month_b="2026-05")["error"]


def test_unusual(data):
    txns, config = data
    u = analysis.find_unusual(txns, config)
    assert any(d["merchant"] == "CITY AUTO REPAIR" for d in u["possible_duplicate_charges"])
    assert any(n["merchant"] == "BEST BUY" for n in u["new_merchants_large_charges"])
    assert any(o["merchant"] == "TARGET" and o["amount"] == -612.37 for o in u["outlier_transactions"])
    assert any("LATE FEE" in f["description"] for f in u["fees_and_interest"])
    assert any(s["category"] == "Shopping" for s in u["category_spikes"])
    # Normal coffee/grocery runs are not flagged as duplicates.
    assert not any(d["merchant"] in ("SAFEWAY", "STARBUCKS STORE") for d in u["possible_duplicate_charges"])


def test_debit_credit_columns(tmp_path):
    f = tmp_path / "bank.csv"
    f.write_text("Posted Date,Payee,Debit,Credit\n01/05/2026,NETFLIX.COM,15.49,\n01/06/2026,PAYROLL,,2000.00\n")
    t = load_transactions(f)
    assert [x.amount for x in t] == [-15.49, 2000.0]
    assert t[0].category == "Streaming & Media"


def test_offline_cli(data):
    out = subprocess.run([sys.executable, "-m", "household_finance", "json", str(SAMPLE / "transactions.csv"),
                          "--config", str(SAMPLE / "household.json")], cwd=ROOT, check=True,
                         capture_output=True, text=True).stdout
    assert json.loads(out)["overview"]["transactions"] > 300
