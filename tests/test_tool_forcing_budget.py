"""
Regression test for bug #14 in agent/core.py's docstring: a live run of
the Mutual Fund Analyst against the "buyout targets" query (the exact
query bug #8 was originally found on) never called get_company_signals
at all. It didn't give up either - it just kept calling
list_companies/list_sectors turn after turn until MAX_TOOL_ITERATIONS ran
out, then drafted with grounded=False, confidence 0%.

Bug #8's original tool-forcing fix only checked for the nudge on a turn
where the model produced NO tool calls (see _run_tool_loop's "if not
message.tool_calls" branch). A model that keeps calling *some* tool every
turn, just never the required one, never hits that branch, so the nudge
never fired. This test builds a fake OpenAI client that reproduces that
exact behavior deterministically (always calls list_companies, never
get_company_signals/compare_companies) and asserts the fix forces the
nudge into the transcript before the budget runs out - no live model
call needed, since this is really a pure control-flow bug, not a prompt
wording one.
"""

import json

import pytest

from agent.core import MAX_TOOL_ITERATIONS, _run_tool_loop
from agent.mcp_client import MCPToolClient


class _FakeFunction:
    def __init__(self, name: str, arguments: str = "{}"):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str = "{}"):
        self.id = call_id
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    """Duck-types just enough of openai's ChatCompletionMessage for
    _run_tool_loop's own code to work: .tool_calls and .model_dump()."""

    def __init__(self, tool_calls=None):
        self.tool_calls = tool_calls
        self.role = "assistant"
        self.content = None

    def model_dump(self, exclude_none=True):
        d = {"role": self.role}
        if self.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in self.tool_calls
            ]
        return d


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeResponse:
    def __init__(self, message):
        self.choices = [_FakeChoice(message)]


class _NeverLooksUpACompanyClient:
    """Simulates the exact live failure: every single turn calls
    list_companies again (a real, allowed tool - never a no-op), so the
    "if not message.tool_calls" branch in _run_tool_loop is never true,
    no matter how many turns run."""

    def __init__(self):
        self.call_count = 0

        class _Completions:
            def create(inner_self, **kwargs):
                self.call_count += 1
                tool_call = _FakeToolCall(
                    f"call_{self.call_count}", "list_companies", json.dumps({"sector": "logistics"})
                )
                return _FakeResponse(_FakeMessage(tool_calls=[tool_call]))

        class _Chat:
            def __init__(inner_self):
                inner_self.completions = _Completions()

        self.chat = _Chat()


@pytest.mark.asyncio
async def test_nudge_fires_even_when_model_never_stops_calling_tools():
    fake_client = _NeverLooksUpACompanyClient()
    messages = [{"role": "system", "content": "test"}, {"role": "user", "content": "test query"}]

    async with MCPToolClient() as mcp_client:
        sector_company_slugs = {c["slug"] for c in await mcp_client.list_companies("logistics")}
        result_messages, unresolved, out_of_scope = await _run_tool_loop(
            fake_client, mcp_client, messages, "logistics", sector_company_slugs
        )

    nudge_texts = [
        m["content"]
        for m in result_messages
        if m.get("role") == "system" and "get_company_signals" in m.get("content", "")
    ]
    assert nudge_texts, (
        "Bug #14 regression: the model called a tool every single turn "
        "(never get_company_signals/compare_companies) and the loop still "
        "exhausted its budget with no nudge ever injected into the transcript."
    )
    # Exactly one nudge - the safeguard still isn't a hard loop-forever
    # requirement, it fires once and stops, same contract as bug #8's fix.
    assert len(nudge_texts) == 1
    # The fake client always calls list_companies, so this never resolves
    # to a real lookup - unresolved/out_of_scope both stay empty. The
    # point of this test is that the nudge fired at all, not that it
    # changed the fake model's (fixed, scripted) behavior.
    assert unresolved == []
    assert out_of_scope == []
    # The fake client happily keeps answering with tool calls forever, so
    # the loop should run the full budget, not stop early.
    assert fake_client.call_count == MAX_TOOL_ITERATIONS


@pytest.mark.asyncio
async def test_nudge_still_fires_on_natural_stop_bug_8_case():
    """Bug #8's original case must still work: a model that gives up
    outright (no tool calls at all, ever) still gets nudged once."""

    class _GivesUpImmediatelyClient:
        def __init__(self):
            class _Completions:
                def create(inner_self, **kwargs):
                    return _FakeResponse(_FakeMessage(tool_calls=None))

            class _Chat:
                def __init__(inner_self):
                    inner_self.completions = _Completions()

            self.chat = _Chat()

    fake_client = _GivesUpImmediatelyClient()
    messages = [{"role": "system", "content": "test"}, {"role": "user", "content": "test query"}]

    async with MCPToolClient() as mcp_client:
        sector_company_slugs = {c["slug"] for c in await mcp_client.list_companies("logistics")}
        result_messages, _, _ = await _run_tool_loop(
            fake_client, mcp_client, messages, "logistics", sector_company_slugs
        )

    nudge_texts = [
        m["content"]
        for m in result_messages
        if m.get("role") == "system" and "get_company_signals" in m.get("content", "")
    ]
    assert len(nudge_texts) == 1
