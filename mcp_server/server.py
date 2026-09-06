"""
MCP server — the ONLY sanctioned path from the agent to the database.

Exposes 4 tools, matching the take-home's own example names:
  - list_companies(sector)
  - get_company_signals(company_slug)
  - compare_companies(company_slugs, metric_name=None)
  - search_sector_context(sector, signal_type=None)

Run standalone for manual testing:
    python -m mcp_server.server            # stdio transport
    python -m mcp_server.server --http     # streamable-http on :8300

The agent does not import this file. It talks to it exclusively through
fastmcp.Client (see agent/mcp_client.py), which speaks the real MCP
protocol whether the transport underneath is in-memory, stdio, or HTTP.
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv
from fastmcp import FastMCP

from mcp_server import queries

load_dotenv()

mcp = FastMCP(
    name="persona-agent-db",
    instructions=(
        "Read-only access to a database of public-company metrics and "
        "operational signals across 3 sectors (tech, retail, logistics). "
        "Use list_companies to see what's in a sector before calling "
        "get_company_signals on a specific company slug."
    ),
)


@mcp.tool
def list_sectors() -> list[dict]:
    """List every sector available in the database, with its slug (use
    this slug in other tool calls) and a short description."""
    return queries.list_sectors()


@mcp.tool
def list_companies(sector: str) -> list[dict]:
    """List every company tracked in the given sector.

    Args:
        sector: sector slug, e.g. "tech", "retail", or "logistics".
    """
    return queries.list_companies(sector)


@mcp.tool
def get_company_signals(company_slug: str) -> dict:
    """Get everything the database knows about one company: its metrics
    (revenue, margin, headcount, etc., each with the period it covers and
    the source it came from) and its qualitative signals (hiring,
    layoffs, automation investment, expansion, etc.).

    Args:
        company_slug: the company's slug, e.g. "fedex" or "gxo". Get valid
            slugs from list_companies first.

    Returns a dict with company/metrics/signals, or
    {"found": false, "company_slug": ...} if the slug is not in the
    database — callers MUST treat that as "no data," never as license to
    guess.
    """
    result = queries.get_company_signals(company_slug)
    if result is None:
        return {"found": False, "company_slug": company_slug}
    return {"found": True, **result}


@mcp.tool
def compare_companies(company_slugs: list[str], metric_name: str | None = None) -> dict:
    """Compare metrics across 2 or more companies, side by side.

    Args:
        company_slugs: list of company slugs to compare.
        metric_name: restrict to one metric (e.g. "operating_margin_pct").
            Omit to get every metric for every company.

    Any slug not found in the database is returned separately under
    "unknown_slugs" rather than silently dropped.
    """
    return queries.compare_companies(company_slugs, metric_name)


@mcp.tool
def search_sector_context(sector: str, signal_type: str | None = None) -> dict:
    """Get every qualitative signal (hiring, layoffs, automation
    investment, expansion, etc.) recorded for companies in a sector — the
    tool to use for sector-level questions rather than calling
    get_company_signals once per company.

    Args:
        sector: sector slug, e.g. "logistics".
        signal_type: optionally restrict to one type, e.g. "layoff" or
            "automation_investment".
    """
    return queries.search_sector_context(sector, signal_type)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the persona-agent MCP server standalone.")
    parser.add_argument(
        "--http", action="store_true", help="Serve over streamable-http instead of stdio"
    )
    args = parser.parse_args()

    if args.http:
        host = os.environ.get("MCP_SERVER_HOST", "127.0.0.1")
        port = int(os.environ.get("MCP_SERVER_PORT", "8300"))
        mcp.run(transport="http", host=host, port=port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
