# Household Finance Agent

An AI agent that reviews your bank and credit-card transactions and tells you:

- **Forgotten subscriptions**: recurring charges you may not remember, price increases, overlapping
  streaming services, free trials that turned into paid plans, the same service billed on two cards,
  and annual plans that will renew.
- **Debt payments**: what you paid toward each loan or card, missed or below-minimum months, interest and
  late fees, payoff time at your current pace, and how much paying $100/month more would save.
- **Recurring expenses month over month**: which bills went up, are new, or stopped.
- **Unusual spending**: outlier purchases, category spikes, possible duplicate charges, big first-time
  merchants, and bank fees.

It has two layers:

1. **Analysis engine** (`household_finance/analysis.py`): plain Python that does the detection and math. It works
   offline and needs no API key.
2. **Claude agent** (`household_finance/agent.py`): Claude (`claude-opus-5-5`) calls the analysis functions
   as tools, checks the underlying transactions, and writes a prioritized report. You can also chat with it.
   Every number in its answers comes from a tool, not from a guess.

## Setup

```bash
cd finance-agent
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # only needed for `report` and `chat`
```

## Try it on the sample data

The sample is six months of made-up transactions with problems planted in it.

```bash
python sample_data/make_sample.py
python -m household_finance analyze sample_data/transactions.csv --config sample_data/household.json  # offline
python -m household_finance report  sample_data/transactions.csv --config sample_data/household.json --out report.md
python -m household_finance chat    sample_data/transactions.csv --config sample_data/household.json
```

## Use your own data

1. Download CSV exports from each bank and card (3 to 12 months works best). Put them in one folder. You can
   pass the folder, and each file's name is used as its account name.
2. The loader detects common column names: `Date`/`Posted Date`, `Description`/`Payee`/`Merchant`, and either
   one signed `Amount` column or separate `Debit`/`Credit` columns. `Category` and `Account` are optional.
   If your card export shows purchases as positive numbers, add `--expenses-positive`.
3. Optional: copy `sample_data/household.json` and fill it in:
   - `debts`: name, text that appears in the payment description (`match`), `balance`, `apr`,
     `minimum_payment`. These enable missed-payment checks and payoff projections.
   - `known_subscriptions`: services you know about. Anything else that recurs gets flagged.
   - `large_purchase_threshold`: the dollar amount above which a first-time merchant is flagged.

```bash
python -m household_finance report ~/Downloads/statements/ --config household.json --out report.md
```

| Command   | What it does                                            | Needs API key |
|-----------|---------------------------------------------------------|---------------|
| `report`  | Claude writes the full report                           | yes           |
| `chat`    | Ask follow-ups ("why was October so expensive?")        | yes           |
| `analyze` | Report with no AI, built straight from the analysis     | no            |
| `json`    | Raw analysis output, for spreadsheets or other tools    | no            |

## Privacy

`analyze` and `json` run entirely on your computer. `report` and `chat` send tool results to the Anthropic
API. These include merchant names, amounts, and dates; they do not include account numbers unless your
descriptions contain them. Don't commit your real statements: `.gitignore` excludes CSVs outside
`sample_data/`.

## Limits

- Detection uses heuristics. A flag means "worth checking", not "definitely wrong".
- A recurring charge needs 3 occurrences to be detected, or 2 charges about a year apart for an annual plan.
  Annual plans seen only once are listed separately as "possible annual plans".
- The agent can read data but cannot cancel anything or move money. It is not a licensed financial adviser.

## Tests

```bash
python -m pytest -q
```

The tests check every planted issue in the sample data, plus the agent loop against a mocked API.
