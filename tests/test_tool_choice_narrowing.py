"""
Regression test for bug #17 in agent/core.py's docstring: bug #16's fix
(tool_choice="required" on the turn right after a nudge) was live-verified
to NOT fix the Mutual Fund Analyst / logistics failure, even though 64/64
offline tests passed, including test_tool_choice_forcing.py's own
regression test for #16.

The reason that test didn't catch it: its fake client special-cases
tool_choice == "required" by hardcoding a call to get_company_signals -
this assumes "required" means "call the RIGHT tool", when the real OpenAI
API only enforces "call SOME tool". A model that prefers list_companies is
free to keep calling list_companies under "required" too.

This test uses a fake client that reproduces that gap honestly: given the
full tool list, it always picks list_companies regardless of tool_choice
(exactly the live failure). The only thing that can make it call
get_company_signals is if list_companies is not among the tools it is
offered at all. This directly checks the bug #17 fix - narrowing `tools`
to LOOKUP_TOOLS on the forced turn, not just setting tool_choice.
"""

import json

import pytest

from agent.core import _run_tool_loop
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


class _ListCompaniesAddictClient:
    """The honest reproduction of the live failure: this model ALWAYS
    prefers list_companies, no matter what tool_choice says - "required"
    is satisfied by calling list_companies again, because list_companies
    is a perfectly valid tool call and "required" never named a specific
    tool. The only way to get this client to call get_company_signals is
    to not offer it list_companies at all on that turn."""

    def __init__(self):
        self.call_count = 0
        self.tools_seen = []
        self.tool_choices_seen = []

        class _Completions:
            def create(inner_self, **kwargs):
                self.call_count += 1
                offered_names = {t["function"]["name"] for t in kwargs.get("tools", [])}
                self.tools_seen.append(offered_names)
                self.tool_choices_seen.append(kwargs.get("tool_choice"))
                if "list_companies" in offered_names:
                    # Reproduces the live failure: prefers list_companies
                    # regardless of tool_choice being "auto" or "required".
                    tool_call = _FakeToolCall(
                        f"call_{self.call_count}", "list_companies", json.dumps({"sector": "logistics"})
                    )
                    return _FakeResponse(_FakeMessage(tool_calls=[tool_call]))
                # list_companies isn't on the menu this turn - the only
                # tools left are the real lookups, so it has to pick one.
                tool_call = _FakeToolCall(
                    f"call_{self.call_count}", "get_company_signals", json.dumps({"company_slug": "gxo"})
                )
                return _FakeResponse(_FakeMessage(tool_calls=[tool_call]))

        class _Chat:
            def __init__(inner_self):
                inner_self.completions = _Completions()

        self.chat = _Chat()


@pytest.mark.asyncio
async def test_narrowing_tools_is_what_actually_forces_the_real_lookup():
    """This is the exact bug #16-survives-anyway case: a model that treats
    tool_choice="required" as satisfied by calling list_companies again.
    Without narrowing the tools array, this client would loop
    MAX_TOOL_ITERATIONS times calling list_companies and never call
    get_company_signals, reproducing the live failure exactly. With the
    bug #17 fix (tools=LOOKUP_TOOLS on the forced turn), it has no choice
    but to call a real lookup tool once list_companies is off the menu."""
    fake_client = _ListCompaniesAddictClient()
    messages = [{"role": "system", "content": "test"}, {"role": "user", "content": "buyout targets"}]

    async with MCPToolClient() as mcp_client:
        sector_company_slugs = {c["slug"] for c in await mcp_client.list_companies("logistics")}
        result_messages, unresolved, out_of_scope = await _run_tool_loop(
            fake_client, mcp_client, messages, "logistics", sector_company_slugs
        )

    # Prove the forced turn actually narrowed the tool list, not just the
    # tool_choice field - this is the crux of bug #17's fix.
    narrowed_turns = [names for names in fake_client.tools_seen if names == {"get_company_signals", "compare_companies"}]
    assert narrowed_turns, (
        f"expected at least one turn offering ONLY the lookup tools - tools_seen: {fake_client.tools_seen}"
    )

    tool_call_summaries = [
        m["tool_calls"][0]["function"]["name"]
        for m in result_messages
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert "get_company_signals" in tool_call_summaries, (
        "even with tools narrowed on the forced turn, the real lookup was never called - "
        f"trace: {tool_call_summaries}"
    )


@pytest.mark.asyncio
async def test_ordinary_turns_still_offer_the_full_tool_list():
    """Guards against over-correcting: only the one forced turn right
    after a nudge should ever see a narrowed tools array. A model that
    complies immediately should see the full OPENAI_TOOLS list on every
    turn it takes."""

    class _CompliantClient:
        def __init__(self):
            self.tools_seen = []
            call_count = 0

            class _Completions:
                def create(inner_self, **kwargs):
                    nonlocal call_count
                    call_count += 1
                    self.tools_seen.append({t["function"]["name"] for t in kwargs.get("tools", [])})
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
    messages = [{"role": "system", "content": "test"}, {"role": "user", "content": "test"}]

    async with MCPToolClient() as mcp_client:
        sector_company_slugs = {c["slug"] for c in await mcp_client.list_companies("logistics")}
        await _run_tool_loop(fake_client, mcp_client, messages, "logistics", sector_company_slugs)

    full_tool_names = {"list_companies", "get_company_signals", "compare_companies", "search_sector_context"}
    assert all(names == full_tool_names for names in fake_client.tools_seen), (
        f"a model that never needed nudging should always see the full tool list: {fake_client.tools_seen}"
    )
