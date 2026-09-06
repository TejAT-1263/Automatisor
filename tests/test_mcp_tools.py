"""
Integration tests for the MCP server itself, calling it exactly the way
the agent does - through fastmcp.Client, never mcp_server.queries
directly - so a passing suite here is evidence the MCP boundary actually
works, not just that the SQL underneath it does.
"""

import pytest
from fastmcp import Client

from agent.tool_specs import TOOL_NAMES
from mcp_server.server import mcp


@pytest.mark.asyncio
async def test_all_declared_tools_are_registered():
    async with Client(mcp) as client:
        registered = {tool.name for tool in await client.list_tools()}
    # Every tool the LLM can call must actually exist on the server.
    # list_sectors is server-only (used to populate the UI dropdown, see
    # DECISIONS.md #8) and deliberately not offered to the LLM, so it's
    # excluded from this check rather than added to TOOL_NAMES.
    assert TOOL_NAMES.issubset(registered)


@pytest.mark.asyncio
async def test_list_companies_returns_real_rows_for_each_sector():
    async with Client(mcp) as client:
        for sector in ("tech", "retail", "logistics"):
            result = await client.call_tool("list_companies", {"sector": sector})
            assert len(result.data) >= 5, f"sector {sector} has too few companies"


@pytest.mark.asyncio
async def test_get_company_signals_found_case_is_grounded():
    async with Client(mcp) as client:
        result = await client.call_tool("get_company_signals", {"company_slug": "gxo"})
        data = result.data
        assert data["found"] is True
        assert data["metrics"], "gxo must have at least one metric row"
        for metric in data["metrics"]:
            assert metric["source_url"].startswith("http")


@pytest.mark.asyncio
async def test_get_company_signals_unknown_company_is_honest():
    """This is the machine-checkable half of the take-home's out-of-scope
    test: the DB layer must say found=false, not silently return
    something plausible-looking."""
    async with Client(mcp) as client:
        result = await client.call_tool(
            "get_company_signals", {"company_slug": "definitely-not-a-real-company"}
        )
        assert result.data == {
            "found": False,
            "company_slug": "definitely-not-a-real-company",
        }


@pytest.mark.asyncio
async def test_compare_companies_reports_unknown_slugs_separately():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "compare_companies",
            {"company_slugs": ["fedex", "not-real"], "metric_name": "operating_margin_pct"},
        )
        assert "fedex" in result.data["companies"]
        assert "not-real" in result.data["unknown_slugs"]
        assert "not-real" not in result.data["companies"]


@pytest.mark.asyncio
async def test_search_sector_context_filters_by_signal_type():
    async with Client(mcp) as client:
        all_signals = await client.call_tool("search_sector_context", {"sector": "logistics"})
        layoffs_only = await client.call_tool(
            "search_sector_context", {"sector": "logistics", "signal_type": "layoff"}
        )
        assert len(layoffs_only.data["signals"]) <= len(all_signals.data["signals"])
        assert all(s["signal_type"] == "layoff" for s in layoffs_only.data["signals"])
