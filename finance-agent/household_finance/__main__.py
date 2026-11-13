"""Command line: python -m household_finance {report,chat,analyze} TRANSACTIONS.csv [--config household.json]"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import analysis
from .data import load_config, load_transactions
from .report import offline_report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="household_finance", description="Household finance agent")
    p.add_argument("command", choices=["report", "chat", "analyze", "json"],
                   help="report: AI-written report | chat: ask questions | analyze: offline report, no AI | "
                        "json: raw analysis output")
    p.add_argument("transactions", help="CSV file, or a folder of CSV files (one per account)")
    p.add_argument("--config", help="household.json with debts and known subscriptions")
    p.add_argument("--expenses-positive", action="store_true",
                   help="Export shows purchases as positive numbers (many credit cards)")
    p.add_argument("--out", help="Write the report to this file as well as printing it")
    p.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--quiet", action="store_true", help="Don't print tool calls")
    args = p.parse_args(argv)

    txns = load_transactions(args.transactions, expenses_positive=args.expenses_positive)
    config = load_config(args.config)
    if not txns:
        print("No transactions found.", file=sys.stderr)
        return 1

    if args.command == "analyze":
        text = offline_report(txns, config)
    elif args.command == "json":
        text = json.dumps({
            "overview": analysis.overview(txns),
            "subscriptions": analysis.find_subscriptions(txns, config),
            "debts": analysis.track_debts(txns, config),
            "recurring_comparison": analysis.compare_recurring(txns),
            "unusual": analysis.find_unusual(txns, config),
        }, indent=2, default=str)
    else:
        from .agent import FinanceAgent  # imported here so offline modes don't need the SDK

        def show(name: str, inputs: dict) -> None:
            if not args.quiet:
                print(f"  -> {name}({', '.join(f'{k}={v!r}' for k, v in inputs.items())})", file=sys.stderr)

        agent = FinanceAgent(txns, config, effort=args.effort, on_tool_call=show)
        if args.command == "report":
            print("Reviewing transactions...", file=sys.stderr)
            text = agent.report()
        else:
            print(f"Loaded {len(txns)} transactions. Ask a question (blank line or Ctrl-D to quit). Try:\n"
                  "  'Which subscriptions am I probably not using?'\n"
                  "  'How are my credit card payments going?'\n"
                  "  'Why was last month so expensive?'", file=sys.stderr)
            while True:
                try:
                    q = input("\nyou> ").strip()
                except EOFError:
                    break
                if not q:
                    break
                print("\n" + agent.ask(q))
            return 0

    print(text)
    if args.out:
        Path(args.out).write_text(text)
        print(f"\nSaved to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
