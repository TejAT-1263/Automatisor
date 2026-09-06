"""
Unit tests for agent.core._find_named_out_of_scope_companies and
_company_name_token — bug #9 in agent/core.py's docstring. No LLM or
network access required: this scans the user's query text against the
real local DB (built by conftest.py) via MCPToolClient, entirely before
any model call.

Why this exists: a live run (2026-09-05) asked "Compare GXO's operating
margin to Microsoft's" scoped to logistics. The model never attempted the
(correctly blocked) get_company_signals(microsoft) call at all, so
build_sector_scope_notice's reactive path never fired — it silently
substituted UPS as the comparison target and answered a DIFFERENT
question with no disclosure. These tests pin down the proactive,
deterministic counterpart directly, the same way test_sector_scope.py
pins down the reactive block at the _dispatch layer rather than trusting
only a live LLM run to exercise it.
"""

import pytest

from agent.core import _company_name_token, _find_named_out_of_scope_companies
from agent.mcp_client import MCPToolClient


def test_company_name_token_strips_common_corporate_suffixes():
    assert _company_name_token("Microsoft Corporation") == "Microsoft"
    assert _company_name_token("Salesforce, Inc.") == "Salesforce"
    assert _company_name_token("Adobe Inc.") == "Adobe"
    assert _company_name_token("Palantir Technologies Inc.") == "Palantir"
    assert _company_name_token("Costco Wholesale Corporation") == "Costco"
    assert _company_name_token("The Kroger Co.") == "Kroger"
    assert _company_name_token("Old Dominion Freight Line, Inc.") == "Old Dominion"
    assert _company_name_token("United Parcel Service, Inc.") == "United Parcel Service"


@pytest.mark.asyncio
async def test_detects_out_of_scope_company_named_by_short_name():
    async with MCPToolClient() as client:
        matches = await _find_named_out_of_scope_companies(
            client, "Compare GXO's operating margin to Microsoft's - which one is stronger?", "logistics"
        )
    slugs = {m["slug"] for m in matches}
    assert "microsoft" in slugs


@pytest.mark.asyncio
async def test_detects_out_of_scope_company_named_by_ticker():
    async with MCPToolClient() as client:
        matches = await _find_named_out_of_scope_companies(
            client, "How does MSFT's margin compare to GXO's?", "logistics"
        )
    slugs = {m["slug"] for m in matches}
    assert "microsoft" in slugs


@pytest.mark.asyncio
async def test_no_false_positive_when_query_only_mentions_in_scope_companies():
    async with MCPToolClient() as client:
        matches = await _find_named_out_of_scope_companies(
            client, "Compare GXO's operating margin to UPS's - which one is stronger?", "logistics"
        )
    assert matches == []


@pytest.mark.asyncio
async def test_ambiguous_short_name_does_not_false_positive_on_common_word():
    """Target Corporation's short name is the ordinary word "target" -
    bare mentions like "GXO's target margin" must not trigger a spurious
    out-of-scope disclosure about Target Corp."""
    async with MCPToolClient() as client:
        matches = await _find_named_out_of_scope_companies(
            client, "What's GXO's target operating margin for next year?", "logistics"
        )
    slugs = {m["slug"] for m in matches}
    assert "target" not in slugs


@pytest.mark.asyncio
async def test_ambiguous_short_name_still_detected_via_ticker():
    async with MCPToolClient() as client:
        matches = await _find_named_out_of_scope_companies(
            client, "How does TGT's margin compare to Walmart's?", "logistics"
        )
    slugs = {m["slug"] for m in matches}
    assert "target" in slugs


@pytest.mark.asyncio
async def test_no_matches_when_query_names_no_company_at_all():
    async with MCPToolClient() as client:
        matches = await _find_named_out_of_scope_companies(
            client, "What's the general sentiment in this sector lately?", "logistics"
        )
    assert matches == []
