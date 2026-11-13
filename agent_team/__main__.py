"""Command-line entry point.

Examples:
    python -m agent_team "A habit tracker web app for students"
    python -m agent_team "..." --workspace projects/habits --commands auto
    python -m agent_team --agent market_research "Research the market for AI astrology apps"
"""

import argparse
import re
import sys
from pathlib import Path

import anthropic

from .roles import ROLES
from .team import DEFAULT_MODEL, AppTeam, TeamConfig

EFFORTS = ("low", "medium", "high", "xhigh", "max")


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40].rstrip("-") or "app"


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="agent_team", description="An AI app-development team: a planner agent plus "
                                "market research, coding, debugging, testing and marketing specialists.")
    p.add_argument("idea", help="The app idea (or, with --agent, the task for that one specialist).")
    p.add_argument("--workspace", type=Path, help="Project folder to build in (default: workspace/<idea-slug>).")
    p.add_argument("--agent", choices=sorted(ROLES), help="Run a single specialist instead of the whole team.")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Planner model (default: {DEFAULT_MODEL}).")
    p.add_argument("--worker-model", help="Model for the specialists (default: same as --model).")
    p.add_argument("--effort", choices=EFFORTS, default="high", help="Planner effort level (default: high).")
    p.add_argument("--commands", choices=("ask", "auto", "deny"), default="ask",
                   help="ask: approve each command (default); auto: run allowlisted commands without asking; "
                        "deny: never run commands.")
    p.add_argument("--command-timeout", type=int, default=300, help="Seconds before a command is stopped.")
    p.add_argument("--max-fix-cycles", type=int, default=3, help="Debug/re-test rounds per milestone (default: 3).")
    p.add_argument("--quiet", action="store_true", help="Only print agent start/finish lines.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    workspace = args.workspace or Path("workspace") / slugify(args.idea)
    config = TeamConfig(
        workspace=workspace,
        planner_model=args.model,
        worker_model=args.worker_model or args.model,
        planner_effort=args.effort,
        command_policy=args.commands,
        command_timeout=args.command_timeout,
        max_fix_cycles=args.max_fix_cycles,
        verbose=not args.quiet,
    )
    team = AppTeam(config)
    print(f"Workspace: {team.workspace.root}")

    try:
        if args.agent:
            team.brief = "(single-specialist run; see the task and PLAN.md if present)"
            result = team.run_specialist(args.agent, args.idea)
        else:
            result = team.build(args.idea)
    except anthropic.AuthenticationError:
        print("Authentication failed. Set ANTHROPIC_API_KEY or run `ant auth login`.", file=sys.stderr)
        return 2
    except anthropic.APIConnectionError as exc:
        print(f"Could not reach the Anthropic API: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped. Re-run with the same --workspace to continue from PLAN.md.", file=sys.stderr)
        return 130
    finally:
        print("\nToken usage\n" + team.log.usage_table())

    print("\n" + "=" * 72 + "\n" + (result.text or "(no final report)"))
    if result.problem:
        print(f"\nNote: {result.problem}")
    print(f"\nFiles are in {team.workspace.root}; the full event log is in .team/log.jsonl there.")
    return 0 if result.problem is None else 1


if __name__ == "__main__":
    sys.exit(main())
