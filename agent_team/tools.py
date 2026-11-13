"""Tool definitions handed to each agent. Each agent gets only the tools its role needs."""

from typing import Any, Optional

from anthropic import beta_tool

from .workspace import Workspace

# Anthropic-hosted web tools (run server-side, no code needed on our side).
WEB_TOOLS: list[dict[str, Any]] = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 10},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 10},
]


def file_tools(ws: Workspace, actor: str, write_prefixes: Optional[tuple[str, ...]], can_write: bool = True) -> list:
    where = "anywhere in the workspace" if write_prefixes is None else "only under: " + ", ".join(write_prefixes)

    @beta_tool
    def list_files(path: str = ".") -> str:
        """List the files in the project workspace, recursively, with sizes. Skips dependency and cache folders.

        Call this to see what exists before reading or changing files.

        Args:
            path: Folder relative to the project root. Defaults to the root.
        """
        return ws.list_files(path)

    @beta_tool
    def read_file(path: str, start_line: int = 1, max_lines: int = 2000) -> str:
        """Read a UTF-8 text file from the workspace. Long files are returned in chunks of lines.

        Args:
            path: File path relative to the project root.
            start_line: First line to return (1-based).
            max_lines: Maximum number of lines to return.
        """
        return ws.read_file(path, start_line, max_lines)

    tools = [list_files, read_file]
    if not can_write:
        return tools

    def write_file(path: str, content: str) -> str:
        """Create a file or replace its entire contents.

        Args:
            path: File path relative to the project root. Parent folders are created as needed.
            content: The complete new file contents.
        """
        return ws.write_file(path, content, write_prefixes)

    def edit_file(path: str, old_text: str, new_text: str) -> str:
        """Replace one exact, unique snippet of text in an existing file.

        Args:
            path: File path relative to the project root.
            old_text: Text to replace, copied exactly from the file (including whitespace). Must occur exactly once.
            new_text: Replacement text.
        """
        return ws.edit_file(path, old_text, new_text, write_prefixes)

    tools.append(
        beta_tool(
            write_file,
            description=f"Create a file or replace its entire contents. Use for new files or full rewrites. "
            f"You may write {where}.",
        )
    )
    tools.append(
        beta_tool(
            edit_file,
            description=f"Replace one exact, unique snippet in an existing file. Prefer this over write_file for "
            f"small changes. You may edit {where}.",
        )
    )
    return tools


def command_tool(ws: Workspace, actor: str):
    @beta_tool
    def run_command(command: str) -> str:
        """Run one command in the project root and return its exit code, stdout and stderr.

        Use it to install dependencies, run tests, builds, linters, or quick scripts. Rules: one plain command at a
        time (no pipes, redirects, `&&`, `;` or `$`); relative paths only; it must finish on its own (never start
        servers, watchers or interactive programs). Allowed programs: python, python3, pip, pip3, pytest, node, npm,
        npx, tsc, ls, cat, head, tail, wc, grep, mkdir, echo. A person may be asked to approve each command.

        Args:
            command: The command line, e.g. "python -m pytest -q" or "npm test".
        """
        return ws.run_command(command, actor=actor)

    return run_command
