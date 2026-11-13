"""Loading and normalizing bank/credit-card transaction exports."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

# Column names we accept for each field (lower-cased, matched exactly).
_COLUMN_ALIASES = {
    "date": ["date", "transaction date", "posted date", "posting date", "trans date"],
    "description": ["description", "merchant", "payee", "name", "memo", "details"],
    "amount": ["amount", "transaction amount", "value"],
    "debit": ["debit", "withdrawal", "withdrawals", "money out"],
    "credit": ["credit", "deposit", "deposits", "money in"],
    "category": ["category", "type"],
    "account": ["account", "account name", "card"],
}

_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y"]

# Prefixes card processors put in front of merchant names.
_PREFIX_RE = re.compile(
    r"^(pos |debit card purchase |debit purchase |purchase |recurring |ach debit |ach |checkcard |"
    r"sq \*|sq\*|tst\* ?|pp\*|paypal \*|paypal\*|sp \* ?|sp\*|py \*|in \*|dd \*|apl\*|apple\.com/bill ?)+",
    re.I,
)
_NOISE_RE = re.compile(r"(#\s?\d+|\b\d{3,}\b|\b[a-z]*\d+[a-z\d]*\b|x{2,}\d*|\*+|\.com\b|www\.|https?://)", re.I)
# Trailing US state code (leaves out codes that are also common words, e.g. "co", "in", "me").
_STATE_TAIL_RE = re.compile(r"\s+(?:al|ak|az|ar|ca|ct|fl|ga|ia|ks|ky|md|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy|dc)$", re.I)

# Keyword -> category, used when the export has no category column (or to refine it).
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "Streaming & Media": ["netflix", "hulu", "disney", "spotify", "apple music", "youtube premium", "youtube tv",
                          "hbo", "max.com", "peacock", "paramount", "sling", "audible", "siriusxm", "pandora",
                          "crunchyroll", "fubo", "tidal", "kindle unlimited"],
    "Software & Cloud": ["adobe", "dropbox", "icloud", "google storage", "google one", "microsoft 365", "office 365",
                         "openai", "chatgpt", "claude.ai", "anthropic", "canva", "grammarly", "1password", "lastpass",
                         "nordvpn", "expressvpn", "github", "notion", "zoom", "calm app", "headspace",
                         "duolingo", "noom"],
    "Fitness": ["planet fitness", "peloton", "la fitness", "equinox", "anytime fitness", "orangetheory", "strava",
                "classpass", "ymca", "gym"],
    "Gaming": ["xbox", "playstation", "nintendo", "steam", "ea play"],
    "News & Reading": ["nytimes", "new york times", "wsj", "washington post", "medium", "substack", "economist",
                       "patreon"],
    "Memberships": ["amazon prime", "prime membership", "costco", "sams club", "walmart+", "dashpass", "uber one",
                    "instacart+", "grubhub+", "aaa "],
    "Debt Payments": ["loan", "mortgage", "student", "navient", "nelnet", "sallie mae", "mohela", "credit card payment",
                      "card payment", "payment thank you", "sofi", "lendingclub", "upstart",
                      "affirm", "klarna", "afterpay", "car payment", "toyota financial", "honda financial",
                      "ford credit", "ally auto", "discover payment", "amex payment", "capital one payment",
                      "chase card", "citi card"],
    "Utilities": ["electric", "power", "energy", "water", "gas co", "comcast", "xfinity", "verizon", "at&t", "t-mobile",
                  "spectrum", "internet", "pg&e", "con ed", "duke energy", "waste"],
    "Insurance": ["insurance", "geico", "state farm", "progressive", "allstate", "lemonade", "aetna", "cigna"],
    "Housing": ["rent", "hoa", "property mgmt", "apartments"],
    "Groceries": ["grocery", "safeway", "kroger", "trader joe", "whole foods", "aldi", "publix", "heb", "wegmans",
                  "sprouts", "food lion"],
    "Dining": ["restaurant", "cafe", "coffee", "starbucks", "dunkin", "mcdonald", "chipotle", "pizza", "doordash",
               "uber eats", "grubhub", "taco", "burger", "sushi", "bar & grill"],
    "Transport": ["shell", "chevron", "exxon", "bp ", "uber", "lyft", "parking", "toll", "transit", "gas station", "auto repair",
                  "jiffy lube", "tire", "car wash", "dmv"],
    "Shopping": ["amazon", "target", "walmart", "best buy", "etsy", "ebay", "home depot", "lowes", "ikea"],
    "Fees & Interest": ["overdraft", "late fee", "interest charge", "finance charge", "annual fee", "service fee",
                        "foreign transaction", "atm fee", "nsf"],
    "Income": ["payroll", "direct dep", "salary", "paycheck", "deposit from"],
    "Transfers": ["transfer", "zelle", "venmo", "cash app"],
}

SUBSCRIPTION_CATEGORIES = {"Streaming & Media", "Software & Cloud", "Fitness", "Gaming", "News & Reading",
                           "Memberships"}


@dataclass
class Transaction:
    date: date
    description: str
    amount: float  # negative = money out, positive = money in
    category: str = "Uncategorized"
    account: str = ""
    merchant: str = ""

    @property
    def is_outflow(self) -> bool:
        return self.amount < 0

    @property
    def spend(self) -> float:
        """Positive dollar amount of money out (0 for inflows)."""
        return -self.amount if self.amount < 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "description": self.description,
            "merchant": self.merchant,
            "amount": round(self.amount, 2),
            "category": self.category,
            "account": self.account,
        }


@dataclass
class Debt:
    name: str
    match: list[str]
    balance: float | None = None
    apr: float | None = None  # percent, e.g. 22.9
    minimum_payment: float | None = None
    due_day: int | None = None
    kind: str = ""


@dataclass
class HouseholdConfig:
    debts: list[Debt] = field(default_factory=list)
    known_subscriptions: list[str] = field(default_factory=list)
    large_purchase_threshold: float = 250.0


def normalize_merchant(description: str) -> str:
    """Collapse 'SQ *BLUE BOTTLE #1234 OAKLAND CA' -> 'BLUE BOTTLE'."""
    text = description.strip().lower()
    text = _PREFIX_RE.sub("", text)
    text = _NOISE_RE.sub(" ", text)
    text = re.sub(r"[^a-z&+' ]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _STATE_TAIL_RE.sub("", text).strip()
    words = text.split()
    return " ".join(words[:3]).upper() if words else description.strip().upper()[:30]


def categorize(description: str, existing: str = "") -> str:
    text = " " + description.lower() + " "
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(k in text for k in keywords):
            return category
    return existing or "Uncategorized"


def _parse_date(value: str) -> date:
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date format: {value!r}")


def _parse_money(value: str) -> float:
    value = (value or "").strip()
    if not value:
        return 0.0
    negative = value.startswith("(") and value.endswith(")") or value.startswith("-")
    cleaned = re.sub(r"[^\d.]", "", value)
    if not cleaned:
        return 0.0
    number = float(cleaned)
    return -number if negative else number


def _find_column(headers: list[str], key: str) -> str | None:
    lowered = {h.strip().lower(): h for h in headers}
    for alias in _COLUMN_ALIASES[key]:
        if alias in lowered:
            return lowered[alias]
    return None


def load_transactions(path: str | Path, expenses_positive: bool = False) -> list[Transaction]:
    """Load a CSV export. Supports a signed `amount` column or separate debit/credit columns.

    By default a negative amount means money out. Pass `expenses_positive=True` for exports
    (common with credit cards) where purchases are positive numbers.
    """
    path = Path(path)
    paths = sorted(path.glob("*.csv")) if path.is_dir() else [path]
    transactions: list[Transaction] = []
    for p in paths:
        with p.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            headers = reader.fieldnames or []
            cols = {k: _find_column(headers, k) for k in _COLUMN_ALIASES}
            if not cols["date"] or not cols["description"]:
                raise ValueError(f"{p}: need at least a date and a description column (found {headers})")
            if not cols["amount"] and not (cols["debit"] or cols["credit"]):
                raise ValueError(f"{p}: need an amount column, or debit/credit columns (found {headers})")
            for row in reader:
                if not (row.get(cols["date"]) or "").strip():
                    continue
                if cols["amount"]:
                    amount = _parse_money(row[cols["amount"]])
                    if expenses_positive:
                        amount = -amount
                else:
                    debit = abs(_parse_money(row.get(cols["debit"]) or "")) if cols["debit"] else 0.0
                    credit = abs(_parse_money(row.get(cols["credit"]) or "")) if cols["credit"] else 0.0
                    amount = credit - debit
                description = row[cols["description"]].strip()
                given_category = (row.get(cols["category"]) or "").strip() if cols["category"] else ""
                transactions.append(Transaction(
                    date=_parse_date(row[cols["date"]]),
                    description=description,
                    amount=amount,
                    category=given_category or categorize(description),
                    account=((row.get(cols["account"]) or "").strip() if cols["account"] else p.stem),
                    merchant=normalize_merchant(description),
                ))
    transactions.sort(key=lambda t: t.date)
    return transactions


def load_config(path: str | Path | None) -> HouseholdConfig:
    if not path:
        return HouseholdConfig()
    raw = json.loads(Path(path).read_text())
    debts = [
        Debt(
            name=d["name"],
            match=[m.lower() for m in d.get("match", [d["name"]])],
            balance=d.get("balance"),
            apr=d.get("apr"),
            minimum_payment=d.get("minimum_payment"),
            due_day=d.get("due_day"),
            kind=d.get("kind", ""),
        )
        for d in raw.get("debts", [])
    ]
    return HouseholdConfig(
        debts=debts,
        known_subscriptions=[s.lower() for s in raw.get("known_subscriptions", [])],
        large_purchase_threshold=float(raw.get("large_purchase_threshold", 250.0)),
    )
