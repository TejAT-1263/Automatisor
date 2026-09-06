"""
Regression test for bug #16 in agent/core.py's docstring: a live run
(Mutual Fund Analyst, the exact query bug #14 was found and fixed on)
showed bug #14's proactive nudge firing correctly - the system message
DID get injected with one iteration left - and the model STILL produced
a turn with zero tool calls anyway, ignoring the explicit instruction to
call get_company_signals, and drafted from no data regardless
(grounded=False, confidence 0%, same failure as before the #14 fix).

This is the sharpest possible demonstration that a system-message nudge
is a request, not a constraint: the model can read "you must call one of
those tools now" and simply decline. #14's fix (forcing the nudge to
fire before the budget runs out) was necessary but not sufficient - the
model still has to WANT to comply.

The real fix: the API call immediately following any nudge now passes
tool_choice="required" instead of "auto", which is an actual constraint
enforced by the API itself (the model must call *some* tool that turn),
not a suggestion. This test verifies that constraint is actually applied
- a fake client that ignores instructions the same way the live model
did would still be forced to make a tool call here, because "auto" isn't
what it's offered on that turn.
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
    def __init__(self, tool_calls=None):
        self.tool_calls = tool_calls
        self.role = "assistant"

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


class _ObstinateClient:
    """Reproduces the exact live failure: keeps calling list_companies
    every turn (never get_company_signals), and - the crucial part -
    even records every tool_choice it was actually called with, so the
    test can prove the API constraint was really applied on the nudge
    turn, not just that a nudge message exists somewhere in the
    transcript. If tool_choice is ever "required" on a turn, this fake
    client (unlike the real live model that ignored a plain instruction)
    obeys the *constraint* and calls a tool, exactly as the real OpenAI
    API would enforce - the point of this test is that the constraint
    exists and is passed correctly, not that a fake client can be made to
    disobey a real API-level constraint, which isn't possible."""

    def __init__(self):
        self.call_count = 0
        self.tool_choices_seen = []

        class _Completions:
            def create(inner_self, **kwargs):
                self.call_count += 1
                self.tool_choices_seen.append(kwargs.get("tool_choice"))
                if kwargs.get("tool_choice") == "required":
                    # A real "required" constraint forces some tool call -
                    # simulate the model finally complying under it.
                    tool_call = _FakeToolCall(
                        f"call_{self.call_count}",
                        "get_company_signals",
                        json.dumps({"company_slug": "gxo"}),
                    )
                    return _FakeResponse(_FakeMessage(tool_calls=[tool_call]))
                # Under "auto", reproduce the live failure: always call a
                # non-essential tool instead, exactly like the real model
                # did on this exact query.
                tool_call = _FakeToolCall(
                    f"call_{self.call_count}", "list_companies", json.dumps({"sector": "logistics"})
                )
                return _FakeResponse(_FakeMessage(tool_calls=[tool_call]))

        class _Chat:
            def __init__(inner_self):
                inner_self.completions = _Completions()

        self.chat = _Chat()


@pytest.mark.asyncio
async def test_nudge_actually_constrains_the_next_turn_not_just_asks():
    fake_client = _ObstinateClient()
    messages = [{"role": "system", "content": "test"}, {"role": "user", "content": "test query"}]

    async with MCPToolClient() as mcp_client:
        sector_company_slugs = {c["slug"] for c in await mcp_client.list_companies("logistics")}
        result_messages, unresolved, out_of_scope = await _run_tool_loop(
            fake_client, mcp_client, messages, "logistics", sector_company_slugs
        )

    # The whole point: at some turn, tool_choice must actually have been
    # "required", not just "auto" every time with a nudge message hoping
    # for the best.
    assert "required" in fake_client.tool_choices_seen, (
        f"nudge fired but the next turn was never actually constrained - "
        f"tool_choices seen: {fake_client.tool_choices_seen}"
    )

    # And because of that constraint, get_company_signals must have
    # actually been called by the end of the loop - the exact thing that
    # failed live even after a plain-text nudge.
    tool_call_summaries = [
        m["tool_calls"][0]["function"]["name"]
        for m in result_messages
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert "get_company_signals" in tool_call_summaries, (
        "the forced turn still never called the required tool - the "
        "constraint didn't actually change the outcome"
    )


@pytest.mark.asyncio
async def test_tool_choice_is_auto_by_default_and_only_required_right_after_a_nudge():
    """Guards against over-correcting: tool_choice must stay "auto" on
    every ordinary turn, only flipping to "required" for exactly one
    turn immediately after a nudge, not permanently and not speculatively
    before any nudge has happened."""

    class _CompliantClient:
        """Calls get_company_signals immediately - a well-behaved model
        that should never see anything but tool_choice="auto"."""

        def __init__(self):
            self.tool_choices_seen = []
            call_count = 0

            class _Completions:
                def create(inner_self, **kwargs):
                    nonlocal call_count
                    call_count += 1
                    self.tool_choices_seen.append(kwargs.get("tool_choice"))
                    if call_count == 1:
                        tool_call = _FakeToolCall(
                            "call_1", "get_company_signals", json.dumps({"company_slug": "gxo"})
                        )
                        return _FakeResponse(_FakeMessage(tool_calls=[tool_call]))
                    return _FakeResponse(_FakeMessage(tool_calls=None))

            class _Chat:
                def __init__(inner_self):
                    inner_self.completions = _Completions()

            self.chat = _Chat()

    fake_client = _CompliantClient()
    messages = [{"role": "system", "content": "test"}, {"role": "user", "content": "test query"}]

    async with MCPToolClient() as mcp_client:
        sector_company_slugs = {c["slug"] for c in await mcp_client.list_companies("logistics")}
        await _run_tool_loop(fake_client, mcp_client, messages, "logistics", sector_company_slugs)

    assert all(tc == "auto" for tc in fake_client.tool_choices_seen), (
        f"a model that never needed nudging should never see tool_choice='required': "
        f"{fake_client.tool_choices_seen}"
    )
