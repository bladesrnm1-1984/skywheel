"""The planner agent and its specialist team."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import anthropic
from anthropic import beta_tool

from .roles import ROLES, Role, planner_system_prompt
from .runner import AgentResult, TeamLog, run_agent
from .tools import WEB_TOOLS, command_tool, file_tools
from .workspace import Approver, Workspace

DEFAULT_MODEL = "claude-opus-5-5"
PLANNER_WRITE_PREFIXES = ("PLAN.md", "docs/")


@dataclass
class TeamConfig:
    workspace: Path
    planner_model: str = DEFAULT_MODEL
    worker_model: str = DEFAULT_MODEL
    planner_effort: str = "high"
    effort_overrides: dict[str, str] = field(default_factory=dict)  # role key -> effort
    command_policy: str = "ask"  # ask | auto | deny
    command_timeout: int = 300
    max_fix_cycles: int = 3
    planner_max_turns: int = 80
    specialist_max_turns: int = 40
    verbose: bool = True


class AppTeam:
    def __init__(
        self,
        config: TeamConfig,
        client: Optional[anthropic.Anthropic] = None,
        approver: Optional[Approver] = None,
    ) -> None:
        self.config = config
        self.client = client or anthropic.Anthropic()
        self.workspace = Workspace(
            config.workspace,
            command_policy=config.command_policy,
            command_timeout=config.command_timeout,
            approver=approver,
        )
        self.log = TeamLog(self.workspace.root, verbose=config.verbose)
        self.brief = ""

    # ------------------------------------------------------------ specialists

    def specialist_tools(self, role: Role) -> list:
        tools = file_tools(self.workspace, role.title, role.write_prefixes)
        if role.can_run_commands:
            tools.append(command_tool(self.workspace, role.title))
        if role.uses_web:
            tools.extend(WEB_TOOLS)
        return tools

    def run_specialist(self, role_key: str, task: str, context: str = "") -> AgentResult:
        role = ROLES[role_key]
        prompt = f"Project brief from the user:\n{self.brief or '(see PLAN.md)'}\n\nYour task:\n{task}"
        if context.strip():
            prompt += f"\n\nContext from the planner:\n{context}"
        self.log.depth += 1
        try:
            return run_agent(
                self.client,
                self.log,
                actor=role.title,
                system=role.system_prompt,
                task=prompt,
                tools=self.specialist_tools(role),
                model=self.config.worker_model,
                effort=self.config.effort_overrides.get(role.key, role.effort),
                max_iterations=self.config.specialist_max_turns,
                workspace=self.workspace,
            )
        finally:
            self.log.depth -= 1

    def delegate_tool(self, role: Role):
        def delegate(task: str, context: str = "") -> str:
            """Delegate a task to a specialist and get back their report.

            Args:
                task: A self-contained description of what to do, including acceptance criteria where relevant.
                context: Optional extra details the specialist needs, such as a previous teammate's report.
            """
            result = self.run_specialist(role.key, task, context)
            report = f"## Report from {role.title}\n\n{result.text or '(no report text)'}"
            if result.files_changed:
                report += "\n\nFiles written: " + ", ".join(result.files_changed)
            if result.problem:
                report += f"\n\nNote: {result.problem}"
            return report

        return beta_tool(delegate, name=f"ask_{role.key}", description=f"{role.title}. {role.summary}")

    # ---------------------------------------------------------------- planner

    def planner_tools(self) -> list:
        tools = file_tools(self.workspace, "Planner", PLANNER_WRITE_PREFIXES)
        tools.extend(self.delegate_tool(role) for role in ROLES.values())
        return tools

    def build(self, idea: str) -> AgentResult:
        """Plan, build, test, and market an app for the given idea."""
        self.brief = idea.strip()
        task = (
            f"App idea from the user:\n{self.brief}\n\n"
            f"Project workspace contents:\n{self.workspace.list_files()}\n\n"
            "Lead the team through the workflow and finish with the final report."
        )
        return run_agent(
            self.client,
            self.log,
            actor="Planner",
            system=planner_system_prompt(self.config.max_fix_cycles),
            task=task,
            tools=self.planner_tools(),
            model=self.config.planner_model,
            effort=self.config.planner_effort,
            max_iterations=self.config.planner_max_turns,
            workspace=self.workspace,
        )
