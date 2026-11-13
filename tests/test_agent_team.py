"""Offline tests for the agent team: sandbox rules and planner -> specialist wiring (no API calls)."""

from types import SimpleNamespace

import pytest

from agent_team import ROLES, AppTeam, TeamConfig, Workspace, WorkspaceError


# ----------------------------------------------------------------- workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "proj", command_policy="auto")


def test_paths_are_confined_to_workspace(ws):
    for bad in ["../outside.txt", "/etc/passwd", "a/../../x", ""]:
        with pytest.raises(WorkspaceError):
            ws.resolve(bad)
    assert ws.resolve("src/app.py") == ws.root / "src" / "app.py"


def test_symlink_escape_is_blocked(ws, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (ws.root / "link.txt").symlink_to(outside)
    with pytest.raises(WorkspaceError):
        ws.read_file("link.txt")


def test_write_prefixes_limit_roles(ws):
    ws.write_file("tests/test_x.py", "x = 1\n", ROLES["tester"].write_prefixes)
    with pytest.raises(WorkspaceError):
        ws.write_file("src/app.py", "hack\n", ROLES["tester"].write_prefixes)
    with pytest.raises(WorkspaceError):
        ws.write_file(".team/log.jsonl", "x", None)
    assert "tests/test_x.py" in ws.changed_files


def test_edit_file_requires_unique_match(ws):
    ws.write_file("a.txt", "one two two\n")
    with pytest.raises(WorkspaceError):
        ws.edit_file("a.txt", "two", "three")
    with pytest.raises(WorkspaceError):
        ws.edit_file("a.txt", "missing", "x")
    ws.edit_file("a.txt", "one", "zero")
    assert ws.read_file("a.txt").endswith("zero two two")


def test_read_file_chunks(ws):
    ws.write_file("big.txt", "\n".join(str(i) for i in range(1, 11)))
    out = ws.read_file("big.txt", start_line=3, max_lines=2)
    assert "lines 3-4 of 10" in out and "start_line=5" in out


@pytest.mark.parametrize(
    "command",
    ["rm -rf .", "python a.py && rm x", "cat x | grep y", "echo $HOME", "cat ../x", "cat /etc/passwd", "ls --dir=/etc"],
)
def test_dangerous_commands_are_refused(ws, command):
    with pytest.raises(WorkspaceError):
        ws.run_command(command)


def test_allowed_command_runs_without_secrets(ws, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    ws.write_file("show_env.py", "import os\nprint('KEY' in str(sorted(os.environ)))\n")
    out = ws.run_command("python3 show_env.py")
    assert "exit code: 0" in out and "False" in out


def test_ask_policy_uses_approver(tmp_path):
    calls = []
    ws = Workspace(tmp_path, command_policy="ask", approver=lambda actor, cmd: calls.append(cmd) or "no")
    with pytest.raises(WorkspaceError, match="declined"):
        ws.run_command("echo hi", actor="Tester")
    assert calls == ["echo hi"]

    ws.approver = lambda actor, cmd: "always"
    assert "hi" in ws.run_command("echo hi")
    assert ws.command_policy == "auto"


def test_deny_policy(tmp_path):
    ws = Workspace(tmp_path, command_policy="deny")
    with pytest.raises(WorkspaceError, match="turned off"):
        ws.run_command("echo hi")


# ------------------------------------------------------------ team wiring


def _message(text, stop_reason="end_turn"):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason, usage=usage)


class FakeClient:
    """Stands in for anthropic.Anthropic: each tool_runner call runs a scripted agent that uses real tools."""

    def __init__(self):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(tool_runner=self.tool_runner))

    def tool_runner(self, **kwargs):
        self.calls.append(kwargs)
        tools = {getattr(t, "name", None) or t["name"]: t for t in kwargs["tools"]}
        if "ask_coder" in tools:  # planner
            tools["write_file"].call({"path": "PLAN.md", "content": "# Plan\n"})
            report = tools["ask_coder"].call({"task": "Build milestone 1"})
            verdict = tools["ask_tester"].call({"task": "Evaluate milestone 1", "context": report})
            text = f"All done.\n{verdict}"
        elif "web_search" in tools:  # researcher / marketer
            text = "Research summary."
        elif "edit_file" in tools and kwargs["output_config"]["effort"] == "high":  # coder / tester / debugger
            system = kwargs["system"]
            if "QA Engineer" in system:
                tools["write_file"].call({"path": "tests/test_app.py", "content": "def test_ok():\n    assert True\n"})
                text = "Scorecard ...\nVERDICT: PASS"
            else:
                tools["write_file"].call({"path": "app.py", "content": "print('hi')\n"})
                text = "Implemented app.py"
        else:
            text = "ok"
        return [SimpleNamespace(get_final_message=lambda t=text: _message(t))]


def test_planner_delegates_to_specialists(tmp_path):
    client = FakeClient()
    team = AppTeam(TeamConfig(workspace=tmp_path / "app", command_policy="deny", verbose=False), client=client)
    result = team.build("A to-do list app")

    assert "VERDICT: PASS" in result.text
    assert "Implemented app.py" in result.text or "Files written" in result.text
    assert (team.workspace.root / "app.py").exists()
    assert (team.workspace.root / "tests" / "test_app.py").exists()
    assert set(result.files_changed) >= {"PLAN.md", "app.py", "tests/test_app.py"}

    planner_call = client.calls[0]
    assert planner_call["model"] == "claude-opus-5-5"
    assert planner_call["stream"] is True
    assert planner_call["fallbacks"] == "default"
    tool_names = {getattr(t, "name", None) for t in planner_call["tools"]}
    assert {f"ask_{k}" for k in ROLES} <= tool_names
    assert "run_command" not in tool_names  # the planner coordinates; it does not execute code
    assert set(team.log.usage) == {"Planner", "Software Engineer", "QA Engineer and Evaluator"}


def test_specialist_tool_sets(tmp_path):
    team = AppTeam(TeamConfig(workspace=tmp_path, verbose=False), client=FakeClient())

    def names(key):
        return {getattr(t, "name", None) or t["name"] for t in team.specialist_tools(ROLES[key])}

    assert {"web_search", "web_fetch"} <= names("market_research")
    assert "run_command" not in names("market_research")
    assert "run_command" in names("coder") and "web_search" not in names("coder")
    assert "run_command" in names("tester")
    assert {"web_search"} <= names("marketer")
