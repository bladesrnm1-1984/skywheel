"""Runs one agent (planner or specialist) to completion with the Anthropic SDK's tool runner."""

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic

MAX_TOKENS = 64_000  # streamed, so a long file write does not hit HTTP timeouts
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class AgentResult:
    actor: str
    text: str
    stop_reason: str | None
    turns: int
    files_changed: list[str] = field(default_factory=list)

    @property
    def problem(self) -> str | None:
        """A human-readable note when the agent did not finish normally."""
        if self.stop_reason == "refusal":
            return "The model declined this task (refusal)."
        if self.stop_reason == "max_tokens":
            return "The last reply was cut off at the output limit."
        if self.stop_reason == "tool_use":
            return "Stopped at the turn limit before finishing."
        return None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    requests: int = 0

    def add(self, usage: Any) -> None:
        self.requests += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0


class TeamLog:
    """Prints progress to the terminal and appends every event to .team/log.jsonl in the workspace."""

    COLORS = {"Planner": "35", "Market Researcher": "36", "Software Engineer": "32", "Debugger": "31",
              "QA Engineer and Evaluator": "33", "Marketing Strategist": "34"}

    def __init__(self, workspace_root: Path, verbose: bool = True) -> None:
        self.verbose = verbose
        self.path = workspace_root / ".team" / "log.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.usage: dict[str, Usage] = {}
        self.depth = 0
        self._color = sys.stdout.isatty()

    def event(self, actor: str, kind: str, **data: Any) -> None:
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "actor": actor, "kind": kind, **data}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        if not self.verbose and kind not in ("start", "finish"):
            return
        indent = "    " * self.depth
        name = f"\033[{self.COLORS.get(actor, '37')}m{actor}\033[0m" if self._color else actor
        if kind == "start":
            print(f"\n{indent}▶ {name}: {data['task'][:300]}")
        elif kind == "text":
            body = data["text"].strip().replace("\n", "\n" + indent + "  ")
            print(f"{indent}  {name}: {body}")
        elif kind == "tool":
            print(f"{indent}  {name} → {data['name']}({_summarize(data['input'])})")
        elif kind == "note":
            print(f"{indent}  {name} ⚠ {data['message']}")
        elif kind == "finish":
            print(f"{indent}■ {name} finished after {data['turns']} turns ({data['stop_reason']}).")

    def add_usage(self, actor: str, usage: Any) -> None:
        self.usage.setdefault(actor, Usage()).add(usage)

    def usage_table(self) -> str:
        rows = [f"{'Agent':<28}{'Requests':>9}{'Input':>12}{'Cache read':>12}{'Output':>10}"]
        for actor, u in self.usage.items():
            rows.append(f"{actor:<28}{u.requests:>9}{u.input_tokens:>12,}{u.cache_read_tokens:>12,}{u.output_tokens:>10,}")
        return "\n".join(rows)


def _summarize(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    parts = []
    for key, value in tool_input.items():
        text = str(value).replace("\n", " ")
        parts.append(f"{key}={text[:80] + '…' if len(text) > 80 else text}")
    return ", ".join(parts)


def run_agent(
    client: anthropic.Anthropic,
    log: TeamLog,
    *,
    actor: str,
    system: str,
    task: str,
    tools: list,
    model: str,
    effort: str,
    max_iterations: int,
    workspace=None,
) -> AgentResult:
    """Run one agent until it stops calling tools, and return its final report."""
    log.event(actor, "start", task=task, model=model, effort=effort)
    before = set(workspace.changed_files) if workspace is not None else set()

    runner = client.beta.messages.tool_runner(
        model=model,
        max_tokens=MAX_TOKENS,
        stream=True,
        max_iterations=max_iterations,
        system=system,
        tools=tools,
        messages=[{"role": "user", "content": task}],
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        cache_control={"type": "ephemeral"},  # caches the growing conversation between turns
        # If the model declines on policy grounds, the API retries on a suitable fallback model in the same call.
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )

    last = None
    turns = 0
    for stream in runner:
        message = stream.get_final_message()
        last = message
        turns += 1
        log.add_usage(actor, message.usage)
        for block in message.content:
            if block.type == "text" and block.text.strip():
                log.event(actor, "text", text=block.text)
            elif block.type in ("tool_use", "server_tool_use"):
                log.event(actor, "tool", name=block.name, input=block.input)
            elif block.type == "fallback":
                log.event(actor, "note", message=f"{block.from_.model} declined; continued on {block.to.model}.")

    if last is None:
        raise RuntimeError(f"{actor} produced no response")

    text = "\n".join(b.text for b in last.content if b.type == "text").strip()
    changed = sorted(workspace.changed_files - before) if workspace is not None else []
    result = AgentResult(actor=actor, text=text, stop_reason=last.stop_reason, turns=turns, files_changed=changed)
    if result.problem:
        log.event(actor, "note", message=result.problem)
    log.event(actor, "finish", turns=turns, stop_reason=last.stop_reason, files_changed=changed)
    return result
