"""Generate fictional sample data (6 months) with known issues planted for testing.

Planted: Hulu price increase, forgotten Calm app + gym, Peacock trial converting, Spotify on two cards,
annual Amazon Prime renewal coming up, credit card paid at minimum, a missed student loan month,
a duplicate charge, an electricity spike, a big new-merchant purchase, a late fee.
"""

import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path

random.seed(7)
HERE = Path(__file__).parent
START, END = date(2026, 5, 1), date(2026, 10, 31)
rows: list[tuple[str, str, float, str]] = []


def add(d: date, desc: str, amt: float, account: str = "Checking"):
    if START <= d <= END:
        rows.append((d.isoformat(), desc, round(amt, 2), account))


def monthly(day: int, desc: str, amt, account="Checking", start=START, end=END):
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        d = date(y, m, min(day, 28))
        if start <= d <= end:
            add(d, desc, -(amt(d) if callable(amt) else amt), account)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


# Income
d = date(2026, 5, 1)
while d <= END:
    add(d, "ACME CORP PAYROLL DIRECT DEP", 3150.00)
    d += timedelta(days=14)

# Housing, utilities, insurance
monthly(1, "OAK RIDGE APARTMENTS RENT", 1850.00)
monthly(12, "CITY ELECTRIC CO", lambda d: 212.40 if d.month == 10 else random.uniform(88, 112))
monthly(15, "XFINITY INTERNET", 79.99)
monthly(20, "T-MOBILE AUTOPAY", 140.00)
monthly(5, "GEICO INSURANCE", 128.50)

# Subscriptions
monthly(3, "NETFLIX.COM", 15.49, "Visa")
monthly(8, "HULU 877-8244858 CA", lambda d: 17.99 if d.month >= 8 else 14.99, "Visa")
monthly(10, "SPOTIFY USA", 11.99, "Visa")
monthly(10, "SPOTIFY P1A2B3C", 11.99, "Mastercard", start=date(2026, 7, 1))
monthly(14, "DISNEY PLUS", 13.99, "Visa")
monthly(22, "PEACOCK TV", 7.99, "Visa", start=date(2026, 9, 1))
monthly(18, "APPLE.COM/BILL CALM APP", 6.99, "Visa")
monthly(2, "PLANET FITNESS #1123", 24.99)
monthly(25, "GOOGLE ONE STORAGE", 2.99, "Visa")
monthly(9, "ADOBE CREATIVE CLOUD", 59.99, "Visa", end=date(2026, 7, 31))  # cancelled
add(date(2025, 11, 20), "AMAZON PRIME MEMBERSHIP", -139.00, "Visa")  # outside window: skipped by add
add(date(2026, 5, 2), "AMAZON PRIME MEMBERSHIP", -139.00, "Visa")

# Debt payments
monthly(16, "CAPITAL ONE PAYMENT THANK YOU", 85.00)
monthly(21, "TOYOTA FINANCIAL AUTO PAY", 389.00)
y_m = [(2026, m) for m in range(5, 11) if m != 8]  # missed August
for y, m in y_m:
    add(date(y, m, 7), "NAVIENT STUDENT LOAN PMT", -220.00)
add(date(2026, 9, 19), "CAPITAL ONE LATE FEE", -39.00, "Checking")

# Everyday spending
d = START
while d <= END:
    if d.weekday() in (5, 6) or random.random() < 0.2:
        add(d, random.choice(["SAFEWAY #1234", "TRADER JOE'S #552", "COSTCO WHSE #0123"]),
            -random.uniform(35, 140), "Visa")
    if random.random() < 0.35:
        add(d, random.choice(["SQ *BLUE BOTTLE COFFEE", "STARBUCKS STORE 1123", "CHIPOTLE 2231",
                              "DOORDASH*THAI PALACE", "TST* PIZZA PLACE"]), -random.uniform(6, 48), "Visa")
    if random.random() < 0.12:
        add(d, random.choice(["SHELL OIL 5744", "CHEVRON 0091"]), -random.uniform(38, 62), "Visa")
    if random.random() < 0.15:
        add(d, random.choice(["AMAZON MKTPLACE PMTS", "TARGET 00012"]), -random.uniform(12, 90), "Visa")
    d += timedelta(days=1)

# Unusual
add(date(2026, 10, 4), "TARGET 00012", -612.37, "Visa")
add(date(2026, 10, 11), "BEST BUY 00342", -1149.99, "Visa")  # new merchant, big
add(date(2026, 10, 17), "SQ *CITY AUTO REPAIR", -480.00, "Visa")
add(date(2026, 10, 18), "SQ *CITY AUTO REPAIR", -480.00, "Visa")  # duplicate

rows.sort()
with (HERE / "transactions.csv").open("w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["Date", "Description", "Amount", "Account"])
    w.writerows(rows)

(HERE / "household.json").write_text(json.dumps({
    "known_subscriptions": ["netflix", "hulu", "spotify", "disney", "amazon prime", "google one"],
    "large_purchase_threshold": 250,
    "debts": [
        {"name": "Capital One Visa", "kind": "credit card", "match": ["capital one payment"],
         "balance": 3200.00, "apr": 27.49, "minimum_payment": 85.00},
        {"name": "Toyota auto loan", "kind": "auto loan", "match": ["toyota financial"],
         "balance": 14250.00, "apr": 6.4, "minimum_payment": 389.00},
        {"name": "Navient student loan", "kind": "student loan", "match": ["navient"],
         "balance": 18900.00, "apr": 5.05, "minimum_payment": 220.00},
    ],
}, indent=2) + "\n")
print(f"Wrote {len(rows)} transactions")
