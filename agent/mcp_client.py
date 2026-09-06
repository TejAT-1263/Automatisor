"""
The agent's ONLY path to company data. This module is the MCP client side
of the boundary described in DECISIONS.md #3: everything here goes through
fastmcp.Client against the real mcp_server.server.mcp instance, over the
actual MCP protocol (call_tool / list_tools), never by importing
mcp_server.queries directly.

Transport: by default this connects in-memory (passing the FastMCP object
straight to fastmcp.Client), which still round-trips through the real MCP
client/server protocol classes — it just skips a subprocess boundary for
local dev. Set MCP_SERVER_URL to point at a standalone
`python -m mcp_server.server --http` process instead; that's a one-line
change (see `_transport()` below) with no change to calling code.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from fastmcp import Client

from mcp_server.server import mcp as mcp_server_instance


@dataclass
class ToolCallRecord:
    tool: str
    arguments: dict
    duration_ms: float
    found: bool | None  # None when not applicable to this tool


class MCPToolClient:
    """One instance per agent request. Tracks every tool call made during
    that request so the response can report a real trace (see
    'Log every MCP tool call' in the take-home brief) and so guardrails.py
    can compute confidence from what was actually retrieved."""

    def __init__(self) -> None:
        self.call_log: list[ToolCallRecord] = []
        self._client: Client | None = None

    def _transport(self):
        url = os.environ.get("MCP_SERVER_URL")
        return url if url else mcp_server_instance

    async def __aenter__(self) -> "MCPToolClient":
        self._client = Client(self._transport())
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc_info)

    async def _call(self, tool: str, arguments: dict) -> dict:
        assert self._client is not None, "use `async with MCPToolClient() as client:`"
        start = time.monotonic()
        result = await self._client.call_tool(tool, arguments)
        duration_ms = (time.monotonic() - start) * 1000
        data = result.data
        found = data.get("found") if isinstance(data, dict) and "found" in data else None
        self.call_log.append(
            ToolCallRecord(tool=tool, arguments=arguments, duration_ms=duration_ms, found=found)
        )
        return data

    async def list_sectors(self) -> list[dict]:
        return await self._call("list_sectors", {})

    async def list_companies(self, sector: str) -> list[dict]:
        return await self._call("list_companies", {"sector": sector})

    async def get_company_signals(self, company_slug: str) -> dict:
        return await self._call("get_company_signals", {"company_slug": company_slug})

    async def compare_companies(self, company_slugs: list[str], metric_name: str | None = None) -> dict:
        return await self._call(
            "compare_companies", {"company_slugs": company_slugs, "metric_name": metric_name}
        )

    async def search_sector_context(self, sector: str, signal_type: str | None = None) -> dict:
        return await self._call(
            "search_sector_context", {"sector": sector, "signal_type": signal_type}
        )

    def call_log_summary(self) -> list[str]:
        """Human-readable trace, e.g. 'get_company_signals(company_slug=gxo)'."""
        summaries = []
        for record in self.call_log:
            args = ", ".join(f"{k}={v}" for k, v in record.arguments.items() if v is not None)
            summaries.append(f"{record.tool}({args})")
        return summaries
