"""Runs the agent loop against a fake API: checks request shape and that tools execute.

No network or API key needed. It does not prove the live API accepts the request.
"""

import json
import sys
from pathlib import Path

import pytest

anthropic = pytest.importorskip("anthropic")
import httpx2  # noqa: E402  (anthropic 1.x+ transport)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from household_finance.agent import FinanceAgent  # noqa: E402
from household_finance.data import load_config, load_transactions  # noqa: E402


def _msg(content, stop):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10}}


def test_agent_loop():
    sample = ROOT / "sample_data"
    import subprocess
    subprocess.run([sys.executable, str(sample / "make_sample.py")], check=True, capture_output=True)
    txns, config = load_transactions(sample / "transactions.csv"), load_config(sample / "household.json")
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append((dict(request.headers), body))
        if len(requests) == 1:
            return httpx2.Response(200, json=_msg([
                {"type": "tool_use", "id": "tu_1", "name": "find_forgotten_subscriptions", "input": {}},
                {"type": "tool_use", "id": "tu_2", "name": "search_transactions", "input": {"query": "hulu"}},
            ], "tool_use"))
        return httpx2.Response(200, json=_msg([{"type": "text", "text": "## Summary\n- Cancel Calm."}], "end_turn"))

    client = anthropic.Anthropic(api_key="test", http_client=anthropic.DefaultHttpxClient(
        transport=httpx2.MockTransport(handler)))
    calls = []
    agent = FinanceAgent(txns, config, client=client, on_tool_call=lambda n, i: calls.append(n))
    answer = agent.ask("Which subscriptions should I cancel?")

    assert answer.startswith("## Summary")
    assert calls == ["find_forgotten_subscriptions", "search_transactions"]
    headers, first = requests[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["thinking"] == {"type": "adaptive"}
    assert first["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in headers.get("anthropic-beta", "")
    assert {t["name"] for t in first["tools"]} >= {"track_debt_payments", "find_unusual_spending"}
    # Second request carries both tool results in one user message.
    results = requests[1][1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["tu_1", "tu_2"]
    assert "HULU" in json.dumps(results)
    # History is kept for follow-up questions.
    assert [m["role"] for m in agent.history] == ["user", "assistant", "user", "assistant"]
