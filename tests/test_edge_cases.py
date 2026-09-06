"""
A sweep of edge cases identified by an explicit audit pass (not found via
a live failure this time - these are the "what could still break" cases
reasoned through directly from the code, then verified rather than
assumed). Two real gaps were found and fixed as part of writing this
file, both in agent/core.py's _run_tool_loop:

1. A malformed tool call from the model (invalid JSON in `arguments`, or
   a required key like `company_slug` missing entirely) used to raise an
   uncaught JSONDecodeError/KeyError, failing the whole request even
   though only one of possibly several tool calls that turn was bad, and
   even though real data may already have been gathered earlier in the
   same loop. Now caught and reported back to the model as an ordinary
   tool error (same shape as the existing "unknown tool" case), so the
   model can retry and the rest of the request can still succeed.
2. That same malformed-arguments case must still count as "attempted the
   lookup" for the tool-forcing safeguard (bugs #8/#14) - a model that
   tried get_company_signals and got the arguments wrong should not then
   also get scolded by the nudge for "never trying at all". Verified
   directly below.

The rest of this file is defensive coverage for things that were never
actually observed to fail, but are worth pinning down given how easy they
would be to get wrong: empty/duplicate slug lists, sector case
sensitivity, and query-length validation. All of it runs offline, no
OPENAI_API_KEY needed - either it's pure code (validation, dict-based
deduplication) or it uses a fake OpenAI client the same way
test_tool_forcing_budget.py does.
"""

import json

import pytest
from pydantic import ValidationError

from agent.core import _run_tool_loop
from agent.mcp_client import MCPToolClient
from api.main import QueryRequest


# --- reuse the same fake-client scaffolding as test_tool_forcing_budget.py ---


class _FakeFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str):
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


class _ScriptedClient:
    """Returns one canned response per call, in order, then stops
    (raises IndexError if asked for more than were scripted - a test bug,
    not a real code path, so failing loudly is correct)."""

    def __init__(self, tool_calls_per_turn):
        # tool_calls_per_turn: list of "one turn's worth of tool calls",
        # each itself a list of (name, raw_arguments_json_string).
        self._turns = iter(tool_calls_per_turn)

        class _Completions:
            def create(inner_self, **kwargs):
                turn = next(self._turns)
                if turn is None:
                    return _FakeResponse(_FakeMessage(tool_calls=None))
                calls = [
                    _FakeToolCall(f"call_{i}", name, args) for i, (name, args) in enumerate(turn)
                ]
                return _FakeResponse(_FakeMessage(tool_calls=calls))

        class _Chat:
            def __init__(inner_self):
                inner_self.completions = _Completions()

        self.chat = _Chat()


@pytest.mark.asyncio
async def test_malformed_json_arguments_does_not_crash_the_loop():
    """Invalid JSON in a tool call's arguments string must not raise -
    it should come back as a tool error message the model can react to."""
    fake_client = _ScriptedClient(
        [
            [("get_company_signals", "{not valid json")],
            None,  # model gives up after seeing the error - loop should end cleanly
        ]
    )
    messages = [{"role": "system", "content": "test"}, {"role": "user", "content": "test"}]
    async with MCPToolClient() as mcp_client:
        sector_company_slugs = {c["slug"] for c in await mcp_client.list_companies("logistics")}
        # Must not raise.
        result_messages, unresolved, out_of_scope = await _run_tool_loop(
            fake_client, mcp_client, messages, "logistics", sector_company_slugs
        )
    tool_error_messages = [
        m for m in result_messages if m.get("role") == "tool" and "invalid arguments" in m.get("content", "")
    ]
    assert tool_error_messages, "expected the malformed call to come back as a tool error, not a crash"


@pytest.mark.asyncio
async def test_missing_required_argument_does_not_crash_the_loop():
    """Valid JSON, but missing the required `company_slug` key entirely -
    a KeyError inside _dispatch must also be caught, not just bad JSON."""
    fake_client = _ScriptedClient(
        [
            [("get_company_signals", json.dumps({}))],  # no company_slug key at all
            None,
        ]
    )
    messages = [{"role": "system", "content": "test"}, {"role": "user", "content": "test"}]
    async with MCPToolClient() as mcp_client:
        sector_company_slugs = {c["slug"] for c in await mcp_client.list_companies("logistics")}
        result_messages, unresolved, out_of_scope = await _run_tool_loop(
            fake_client, mcp_client, messages, "logistics", sector_company_slugs
        )
    tool_error_messages = [
        m for m in result_messages if m.get("role") == "tool" and "invalid arguments" in m.get("content", "")
    ]
    assert tool_error_messages


@pytest.mark.asyncio
async def test_malformed_lookup_attempt_still_counts_as_attempted_no_false_nudge():
    """A model that TRIES get_company_signals but gets the arguments
    wrong made a real attempt - the tool-forcing nudge (bugs #8/#14)
    exists to catch a model that never tries at all, not one that tried
    and made a mistake. This must not also get nudged."""
    fake_client = _ScriptedClient(
        [
            [("get_company_signals", "{not valid json")],
            None,
        ]
    )
    messages = [{"role": "system", "content": "test"}, {"role": "user", "content": "test"}]
    async with MCPToolClient() as mcp_client:
        sector_company_slugs = {c["slug"] for c in await mcp_client.list_companies("logistics")}
        result_messages, _, _ = await _run_tool_loop(
            fake_client, mcp_client, messages, "logistics", sector_company_slugs
        )
    nudge_texts = [
        m
        for m in result_messages
        if m.get("role") == "system" and "get_company_signals" in m.get("content", "") and "have not called" in m.get("content", "")
    ]
    assert not nudge_texts, "a malformed-but-real attempt should not also trigger the 'never tried' nudge"


@pytest.mark.asyncio
async def test_blocked_out_of_sector_attempt_still_counts_as_attempted_no_false_nudge():
    """A model that correctly gets blocked looking up an out-of-sector
    company (bug #4) still made a real attempt at the required tool - it
    must not ALSO get hit with the 'you never tried' nudge on top of the
    sector-scope block, which would be a confusing, contradictory pair of
    system messages for no reason."""
    fake_client = _ScriptedClient(
        [
            [("get_company_signals", json.dumps({"company_slug": "microsoft"}))],  # real company, wrong sector
            None,
        ]
    )
    messages = [{"role": "system", "content": "test"}, {"role": "user", "content": "test"}]
    async with MCPToolClient() as mcp_client:
        sector_company_slugs = {c["slug"] for c in await mcp_client.list_companies("logistics")}
        result_messages, unresolved, out_of_scope = await _run_tool_loop(
            fake_client, mcp_client, messages, "logistics", sector_company_slugs
        )
    assert "microsoft" in out_of_scope
    nudge_texts = [
        m
        for m in result_messages
        if m.get("role") == "system" and "have not called" in m.get("content", "")
    ]
    assert not nudge_texts


# --- MCP-layer defensive coverage: empty / duplicate slug lists ---


@pytest.mark.asyncio
async def test_compare_companies_empty_slug_list_is_graceful():
    async with MCPToolClient() as client:
        result = await client.compare_companies([], metric_name="revenue")
    assert result == {"companies": {}, "unknown_slugs": []}


@pytest.mark.asyncio
async def test_compare_companies_duplicate_slugs_do_not_duplicate_or_crash():
    async with MCPToolClient() as client:
        result = await client.compare_companies(["gxo", "gxo"], metric_name="operating_margin_pct")
    # dict-keyed by slug, so a duplicate naturally collapses to one entry -
    # this pins that down as intentional behavior, not an accident.
    assert list(result["companies"].keys()) == ["gxo"]


@pytest.mark.asyncio
async def test_compare_companies_unrecognized_metric_name_returns_empty_rows_not_an_error():
    """metric_name is a closed vocabulary at the schema level (DECISIONS.md
    #2's CHECK constraint), but the query layer takes it as a plain string
    parameter - a value outside that vocabulary must fail honestly (zero
    matching rows), not raise a SQL error."""
    async with MCPToolClient() as client:
        result = await client.compare_companies(["gxo"], metric_name="not_a_real_metric_name")
    assert result["companies"]["gxo"] == []


# --- API-layer validation edge cases (no live call needed - fails before run_agent) ---


def test_sector_is_case_sensitive_through_pydantic_and_api_validation():
    """'Logistics' (capitalized) is not the same slug as 'logistics' -
    confirmed here at the pydantic level; api/main.py's _validate_sector
    does an exact `in` check against the DB's lowercase slugs, so this
    must 400, not silently normalize the case and proceed."""
    # QueryRequest itself has no case restriction (sector is just a
    # length-bounded string) - the rejection happens one layer up, in
    # _validate_sector's exact-match check against real DB slugs. This
    # test documents that boundary: pydantic alone will happily accept
    # "Logistics".
    request = QueryRequest(query="test query", persona="equity_analyst", sector="Logistics")
    assert request.sector == "Logistics"  # pydantic doesn't normalize case
    # The actual case-sensitivity enforcement is tests/test_api.py's
    # test_query_rejects_unknown_sector's job at the HTTP layer; this
    # test exists to make explicit that pydantic validation alone is NOT
    # where that protection lives, so nobody "fixes" case sensitivity by
    # loosening the pydantic Field and accidentally removes the real check.


def test_query_at_max_length_boundary_is_accepted():
    QueryRequest(query="a" * 2000, persona="equity_analyst", sector="logistics")


def test_query_over_max_length_boundary_is_rejected():
    with pytest.raises(ValidationError):
        QueryRequest(query="a" * 2001, persona="equity_analyst", sector="logistics")
