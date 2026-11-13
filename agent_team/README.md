# AI App-Development Team

A team of Claude agents that takes an app idea and returns a planned, built, tested app with a launch kit.

```
                         ┌──────────────────────────┐
           app idea ───▶ │   Planner (lead / PM)    │ ──▶ PLAN.md, docs/FINAL_REPORT.md
                         └────────────┬─────────────┘
          delegates one task at a time│ and reads back each report
   ┌──────────────┬──────────────┬────┴─────────┬──────────────┬──────────────┐
   ▼              ▼              ▼              ▼              ▼
Market        Software        Debugger       QA Engineer    Marketing
Researcher    Engineer                       & Evaluator    Strategist
web search    writes code     reproduces,    writes/runs    launch kit,
+ fetch       runs checks     root-causes,   tests, PASS/   web search
                              fixes          FAIL verdict
docs/research/  anywhere      anywhere       tests/, docs/qa/  docs/marketing/
```

## How it works

1. **Discover**: the Planner asks the Market Researcher to study users, competitors, pricing and gaps (with sources).
2. **Plan**: the Planner writes `PLAN.md` with the MVP scope, tech stack, file layout, and milestones with testable acceptance criteria.
3. **Build and evaluate, one milestone at a time**: Software Engineer implements, then QA Engineer tests it and gives a verdict. On `VERDICT: FAIL`, the Debugger fixes it and QA re-tests, for up to `--max-fix-cycles` rounds.
4. **Launch**: the Marketing Strategist writes positioning, landing-page copy, an app-store listing, a launch plan and social posts, based only on features that were actually built.
5. **Report**: the Planner writes `docs/FINAL_REPORT.md` and prints a summary.

Each specialist is its own Claude agent with its own system prompt and tool set, running in a fresh conversation. The Planner calls them as tools (`ask_coder`, `ask_tester`, …) and sees only their short reports, so its context stays small. The QA agent cannot edit application code, which keeps its evaluation independent. If you re-run with the same `--workspace`, the Planner reads `PLAN.md` and picks up where it left off.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r agent_team/requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # or: ant auth login
```

## Usage

```bash
# Run the whole team (you approve each shell command it wants to run)
python -m agent_team "A habit tracker web app for college students"

# Choose the folder, and let allowlisted commands run without asking
python -m agent_team "Offline-first recipe planner PWA" --workspace projects/recipes --commands auto

# Run one specialist on its own
python -m agent_team --agent market_research "Research the market for privacy-first astrology apps"
python -m agent_team --agent tester "Evaluate the app in this workspace" --workspace projects/recipes
```

| Option | Default | Meaning |
|---|---|---|
| `--workspace DIR` | `workspace/<idea-slug>` | Project folder the team works in |
| `--agent ROLE` | (whole team) | Run one of: `market_research`, `coder`, `debugger`, `tester`, `marketer` |
| `--model` | `claude-opus-5-5` | Planner model |
| `--worker-model` | same as `--model` | Specialist model; e.g. `claude-sonnet-5-5` costs less |
| `--effort` | `high` | Planner effort: `low` `medium` `high` `xhigh` `max` |
| `--commands` | `ask` | `ask` approves each command, `auto` runs allowlisted commands without asking, `deny` never runs any |
| `--max-fix-cycles` | `3` | Rounds of debugging and re-testing per milestone |
| `--quiet` | off | Print only when an agent starts or finishes |

Output in the workspace: the app source, `README.md`, `tests/`, `PLAN.md`, `docs/research/`, `docs/qa/`, `docs/marketing/`, `docs/FINAL_REPORT.md`, and a full event log at `.team/log.jsonl`. A token-usage table for each agent is printed at the end.

## Use from Python

```python
from pathlib import Path
from agent_team import AppTeam, TeamConfig

team = AppTeam(TeamConfig(workspace=Path("projects/notes"), command_policy="auto"))
result = team.build("A markdown notes app with tags and full-text search")
print(result.text)
```

To add a specialist (for example a UX designer), add a `Role` in `roles.py` and include it in `ROLES`. The Planner gets an `ask_<key>` tool for it automatically.

## Safety

The agents' file paths and commands are treated as untrusted:

- Files can only be read or written inside the workspace. Symlinks and `..` cannot escape it, and each role can write only to its own folders.
- Commands run without a shell: one allowlisted program at a time (python, pip, pytest, node, npm, npx, tsc and a few read-only utilities). Pipes, `&&`, redirects, `$` and absolute or `..` paths are refused. Each command has a timeout, and API keys, tokens and passwords are removed from its environment.
- By default you approve every command. Answer `a` to allow the rest of the run.

These rules reduce risk but are not a sandbox: an allowed program like `python` or `npm install` can still do anything your user account can. For ideas or dependencies you don't trust, run the team in a container or VM, especially with `--commands auto`.

## API details

- Built on the official `anthropic` Python SDK's tool runner (`client.beta.messages.tool_runner`), with streaming, adaptive thinking, an effort level for each role, automatic prompt caching, and server-side web search and fetch for research and marketing.
- Refusal fallback is on (`fallbacks: "default"`): if the model declines a request on policy grounds, the API retries it on a suitable fallback model within the same call. To turn it off, remove `betas`/`fallbacks` in `runner.py`.
- The SDK tool runner uses message threads by default. These make each request smaller, but they are not eligible for zero data retention. If you need ZDR, pass `thread=False` to `tool_runner` in `runner.py`.
- All agents use `claude-opus-5-5` by default. Each full build makes many model calls, so for a first try use a small idea or `--worker-model claude-sonnet-5-5`.

## Tests

```bash
pip install pytest
python -m pytest tests -q
```

The tests run offline with a fake client. They cover the sandbox rules and the planner-to-specialist wiring.
