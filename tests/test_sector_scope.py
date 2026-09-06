"""
Unit tests for the sector-scope enforcement added to agent/core.py's
_dispatch (bug #4 in that module's docstring) — no LLM or network access
required, since _dispatch's sector check runs before any model call and
only needs the real local MCPToolClient/DB (built by conftest.py).

Why this exists: manual UI testing on 2026-09-04 found the agent would
freely retrieve and confidently compare Microsoft (a tech-sector company)
while the conversation was scoped to "logistics" — with 88% confidence
and no mention it was out of scope — identically across all 3 personas.
The cause was that get_company_signals/compare_companies never took or
checked a sector argument at all; "you are scoped to the 'logistics'
sector" was a system-prompt sentence and nothing else. These tests pin
down the fix directly at the dispatch layer, rather than only through a
live LLM run that can't distinguish "the model chose not to" from "the
code physically blocked it."
"""

import os

import pytest

from agent.core import _dispatch, run_agent
from agent.guardrails import build_sector_scope_notice
from agent.mcp_client import MCPToolClient
from agent.schemas import Persona


@pytest.mark.asyncio
async def test_get_company_signals_blocks_out_of_sector_slug():
    """microsoft is a real, real-data company - just not in "logistics".
    The block must happen without ever returning real Microsoft data."""
    async with MCPToolClient() as client:
        sector_company_slugs = {c["slug"] for c in await client.list_companies("logistics")}
        result = await _dispatch(
            client, "get_company_signals", {"company_slug": "microsoft"}, "logistics", sector_company_slugs
        )
    assert result == {"found": False, "company_slug": "microsoft", "out_of_sector": True}


@pytest.mark.asyncio
async def test_get_company_signals_allows_in_sector_slug():
    """gxo IS in "logistics" - the fix must not over-block real in-scope
    lookups, only out-of-scope ones."""
    async with MCPToolClient() as client:
        sector_company_slugs = {c["slug"] for c in await client.list_companies("logistics")}
        result = await _dispatch(
            client, "get_company_signals", {"company_slug": "gxo"}, "logistics", sector_company_slugs
        )
    assert result["found"] is True
    assert result["company"]["slug"] == "gxo"


@pytest.mark.asyncio
async def test_compare_companies_splits_in_and_out_of_sector():
    """The exact scenario from the live bug: comparing gxo (in-sector) to
    microsoft (out-of-sector) while scoped to logistics. gxo's real data
    must still come back; microsoft must be reported separately as
    out-of-sector, NOT silently merged into unknown_slugs (which means
    "doesn't exist at all" - a different, less accurate claim)."""
    async with MCPToolClient() as client:
        sector_company_slugs = {c["slug"] for c in await client.list_companies("logistics")}
        result = await _dispatch(
            client,
            "compare_companies",
            {"company_slugs": ["gxo", "microsoft"], "metric_name": None},
            "logistics",
            sector_company_slugs,
        )
    assert "gxo" in result["companies"]
    assert result["companies"]["gxo"]  # real rows came back
    assert "microsoft" not in result["companies"]
    assert result["out_of_sector_slugs"] == ["microsoft"]
    assert "microsoft" not in result["unknown_slugs"]  # microsoft exists - it's not "unknown"


@pytest.mark.asyncio
async def test_compare_companies_all_out_of_sector_returns_empty_companies():
    async with MCPToolClient() as client:
        sector_company_slugs = {c["slug"] for c in await client.list_companies("logistics")}
        result = await _dispatch(
            client,
            "compare_companies",
            {"company_slugs": ["microsoft", "salesforce"], "metric_name": None},
            "logistics",
            sector_company_slugs,
        )
    assert result["companies"] == {}
    assert set(result["out_of_sector_slugs"]) == {"microsoft", "salesforce"}


@pytest.mark.asyncio
async def test_list_companies_ignores_model_supplied_sector():
    """The model could pass any sector string as an argument to
    list_companies - it must always be overridden to the conversation's
    real declared sector, never trusted."""
    async with MCPToolClient() as client:
        result = await _dispatch(client, "list_companies", {"sector": "tech"}, "logistics", set())
    slugs = {c["slug"] for c in result["companies"]}
    assert slugs == {"fedex", "ups", "xpo", "old-dominion", "gxo"}  # logistics, not tech


@pytest.mark.asyncio
async def test_search_sector_context_ignores_model_supplied_sector():
    async with MCPToolClient() as client:
        result = await _dispatch(client, "search_sector_context", {"sector": "retail"}, "tech", set())
    assert result["sector"]["slug"] == "tech"  # tech, not the model-supplied "retail"


def test_sector_scope_notice_none_when_nothing_blocked():
    assert build_sector_scope_notice([], "logistics") is None


def test_sector_scope_notice_names_slugs_and_sector_distinctly_from_grounding_notice():
    notice = build_sector_scope_notice(["microsoft"], "logistics")
    assert notice is not None
    assert "microsoft" in notice
    assert "logistics" in notice
    # This must NOT claim "no data" - that's build_grounding_notice's
    # wording for a genuinely unknown company, a different, less accurate
    # claim for a company that exists just in another sector.
    assert "NOT in the database" not in notice
    assert "out of scope" in notice.lower()


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="requires a live OPENAI_API_KEY")
@pytest.mark.live
@pytest.mark.asyncio
async def test_live_cross_sector_comparison_is_now_blocked():
    """Regression test for the exact live finding this whole file exists
    to fix: manual UI testing on 2026-09-04 asked all 3 personas to
    compare GXO (logistics) to Microsoft (tech) while scoped to
    "logistics", and got a confident 88%-confidence answer with real
    Microsoft data and zero mention it was out of scope, identically
    across all 3 runs. This is the same query, run for real against a
    live API key post-fix, to confirm the block actually holds end to
    end - not just at the _dispatch unit-test level above."""
    response = await run_agent(
        Persona.EQUITY_ANALYST,
        "logistics",
        "Comparing the operating margins of GXO Logistics and Microsoft reveals a significant "
        "difference in their financial performance - which one is stronger?",
    )
    # companies_referenced is computed purely from resolved tool results
    # (agent/core.py's run_agent) - if the block worked, no real Microsoft
    # data was ever resolved, so it cannot appear here regardless of
    # whether the model names "Microsoft" in prose while explaining it's
    # out of scope (which is correct, desired behavior, not a failure).
    assert "microsoft" not in response.companies_referenced, (
        f"microsoft (out-of-sector) was referenced despite the sector-scope block: "
        f"{response.companies_referenced}"
    )
    # The specific real figures from the pre-fix live leak must not
    # appear - that's the actual harm (fabricated-looking real data used
    # out of scope), not the mere mention of the company's name.
    for leaked_figure in ("45.6%", "128.5", "281.7"):
        assert leaked_figure not in response.answer, (
            f"answer contains Microsoft's real out-of-sector figure '{leaked_figure}' "
            f"despite the sector-scope block: {response.answer}"
        )
