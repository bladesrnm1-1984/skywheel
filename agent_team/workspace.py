"""A sandboxed project folder that the agents read, write, and run commands in.

Every path and command the agents send is untrusted model output, so this module:

* confines file access to the workspace root (after resolving symlinks and ``..``),
* limits which folders each role may write to,
* runs only allowlisted programs, one at a time, with no shell, a timeout, and an
  environment stripped of API keys and other secrets,
* asks a person before each command unless the run was started with ``--commands auto``.

This is a guard rail, not a security boundary: an allowed program such as
``python`` can still do anything the current user can. For untrusted ideas, run the
team inside a container or VM.
"""

import os
import shlex
import subprocess
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from anthropic.lib.tools import ToolError

# Programs agents may start. Anything else is refused.
ALLOWED_COMMANDS = frozenset(
    {
        "python", "python3", "pip", "pip3", "pytest",
        "node", "npm", "npx", "tsc",
        "ls", "cat", "head", "tail", "wc", "grep", "mkdir", "echo",
    }
)

# Characters that would chain, pipe, redirect, or substitute in a shell.
SHELL_METACHARS = frozenset(";&|`$<>\n\r")

# Folders that list_files skips because they are huge or not project source.
SKIP_DIRS = frozenset({".git", ".team", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"})

# Environment variables whose names contain these words are not passed to commands.
SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "ANTHROPIC")

COMMAND_POLICIES = ("ask", "auto", "deny")

# Approver(actor, command) -> "yes" | "no" | "always"
Approver = Callable[[str, str], str]


class WorkspaceError(ToolError):
    """Raised for a refused or failed tool action; the tool runner returns it to Claude with is_error."""


def console_approver(actor: str, command: str) -> str:
    """Ask on the terminal whether an agent may run a command."""
    try:
        answer = input(f"\n[{actor}] wants to run:  {command}\nAllow? [y]es / [n]o / [a]lways: ").strip().lower()
    except EOFError:
        return "no"
    if answer in ("a", "always"):
        return "always"
    return "yes" if answer in ("y", "yes") else "no"


class Workspace:
    def __init__(
        self,
        root: Path,
        *,
        command_policy: str = "ask",
        command_timeout: int = 300,
        approver: Optional[Approver] = None,
        output_limit: int = 20_000,
    ) -> None:
        if command_policy not in COMMAND_POLICIES:
            raise ValueError(f"command_policy must be one of {COMMAND_POLICIES}")
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.command_policy = command_policy
        self.command_timeout = command_timeout
        self.approver = approver or console_approver
        self.output_limit = output_limit
        self.changed_files: set[str] = set()

    # ------------------------------------------------------------------ paths

    def resolve(self, path: str) -> Path:
        """Turn an agent-supplied relative path into an absolute path inside the workspace."""
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            raise WorkspaceError("A non-empty relative path is required.")
        candidate = (self.root / path.strip()).resolve()
        if not candidate.is_relative_to(self.root):
            raise WorkspaceError(f"'{path}' is outside the project workspace. Use a path relative to the project root.")
        return candidate

    def relative(self, absolute: Path) -> str:
        rel = absolute.relative_to(self.root).as_posix()
        return rel or "."

    def check_writable(self, rel: str, write_prefixes: Optional[tuple[str, ...]]) -> None:
        if rel == "." or rel.startswith(".team/") or rel == ".team":
            raise WorkspaceError(f"'{rel}' is reserved for the team's own logs.")
        if write_prefixes is None:
            return
        for prefix in write_prefixes:
            if prefix.endswith("/") and rel.startswith(prefix):
                return
            if rel == prefix:
                return
        allowed = ", ".join(write_prefixes)
        raise WorkspaceError(f"Your role may only write to: {allowed}. Ask the planner to route '{rel}' to the right teammate.")

    # ------------------------------------------------------------------ files

    def list_files(self, path: str = ".", max_entries: int = 500) -> str:
        base = self.resolve(path)
        if not base.exists():
            raise WorkspaceError(f"'{path}' does not exist.")
        if base.is_file():
            return self.relative(base)
        entries: list[str] = []
        total = 0
        for current, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                total += 1
                if len(entries) < max_entries:
                    full = Path(current) / name
                    entries.append(f"{self.relative(full)}  ({full.stat().st_size} bytes)")
        if not entries:
            return "(no files yet)"
        if total > len(entries):
            entries.append(f"[{total - len(entries)} more files not shown; list a subfolder to see them]")
        return "\n".join(entries)

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 2000) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise WorkspaceError(f"'{path}' is not a file. Use list_files to see what exists.")
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            raise WorkspaceError(f"'{path}' is not a UTF-8 text file.") from None
        start = max(start_line, 1)
        chunk = lines[start - 1 : start - 1 + max_lines]
        end = start + len(chunk) - 1
        header = f"[{self.relative(target)}: lines {start}-{end} of {len(lines)}]"
        if end < len(lines):
            header += f" [more below: call read_file with start_line={end + 1}]"
        return header + "\n" + "\n".join(chunk)

    def write_file(self, path: str, content: str, write_prefixes: Optional[tuple[str, ...]] = None) -> str:
        target = self.resolve(path)
        rel = self.relative(target)
        self.check_writable(rel, write_prefixes)
        if target.is_dir():
            raise WorkspaceError(f"'{rel}' is a directory.")
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(content, encoding="utf-8")
        self.changed_files.add(rel)
        verb = "Updated" if existed else "Created"
        return f"{verb} {rel} ({len(content.splitlines())} lines)."

    def edit_file(
        self, path: str, old_text: str, new_text: str, write_prefixes: Optional[tuple[str, ...]] = None
    ) -> str:
        target = self.resolve(path)
        rel = self.relative(target)
        self.check_writable(rel, write_prefixes)
        if not target.is_file():
            raise WorkspaceError(f"'{rel}' does not exist. Use write_file to create it.")
        text = target.read_text(encoding="utf-8")
        count = text.count(old_text) if old_text else 0
        if count == 0:
            raise WorkspaceError(f"old_text was not found in {rel}. Re-read the file and copy the text exactly.")
        if count > 1:
            raise WorkspaceError(f"old_text appears {count} times in {rel}. Include more surrounding lines so it is unique.")
        target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        self.changed_files.add(rel)
        return f"Edited {rel}."

    # --------------------------------------------------------------- commands

    def validate_command(self, command: str) -> list[str]:
        if not isinstance(command, str) or not command.strip():
            raise WorkspaceError("An empty command was given.")
        bad = sorted({c for c in command if c in SHELL_METACHARS})
        if bad:
            shown = " ".join(repr(c) for c in bad)
            raise WorkspaceError(f"Shell operators ({shown}) are not allowed. Run one plain command at a time.")
        try:
            args = shlex.split(command)
        except ValueError as exc:
            raise WorkspaceError(f"Could not parse the command: {exc}") from None
        program = args[0]
        if program not in ALLOWED_COMMANDS:
            allowed = ", ".join(sorted(ALLOWED_COMMANDS))
            raise WorkspaceError(f"'{program}' is not an allowed program. Allowed: {allowed}.")
        for token in args[1:]:
            values = [token] + ([token.split("=", 1)[1]] if "=" in token else [])
            for value in values:
                if value.startswith(("/", "~")) or ".." in PurePosixPath(value).parts:
                    raise WorkspaceError(f"Argument '{token}' points outside the workspace. Use relative paths.")
        return args

    def run_command(self, command: str, actor: str = "agent") -> str:
        if self.command_policy == "deny":
            raise WorkspaceError("Running commands is turned off for this run (--commands deny). Report what you would have run instead.")
        args = self.validate_command(command)
        if self.command_policy == "ask":
            decision = self.approver(actor, command)
            if decision == "always":
                self.command_policy = "auto"
            elif decision != "yes":
                raise WorkspaceError("The user declined to run this command. Continue without it or suggest an alternative.")

        env = {k: v for k, v in os.environ.items() if not any(m in k.upper() for m in SECRET_MARKERS)}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            proc = subprocess.run(
                args,
                cwd=self.root,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            raise WorkspaceError(f"'{args[0]}' is not installed on this machine.") from None
        except subprocess.TimeoutExpired as exc:
            partial = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            raise WorkspaceError(
                f"Timed out after {self.command_timeout}s. Do not start servers or watchers; run finite commands only.\n"
                + self._clip(partial)
            ) from None
        return (
            f"exit code: {proc.returncode}\n"
            f"--- stdout ---\n{self._clip(proc.stdout)}\n"
            f"--- stderr ---\n{self._clip(proc.stderr)}"
        )

    def _clip(self, text: str) -> str:
        if len(text) <= self.output_limit:
            return text
        omitted = len(text) - self.output_limit
        return f"[first {omitted} characters omitted]\n" + text[-self.output_limit :]
