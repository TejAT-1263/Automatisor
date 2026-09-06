"""
OpenAI function-calling tool specs, hand-mirrored 1:1 from the MCP tools
in mcp_server/server.py.

Why two definitions of the same 4 tools exist: OpenAI's function-calling
format and the MCP tool schema are different wire formats for the same
underlying capability. Function-calling is how the model *expresses
intent* to call a tool; execution of that intent always goes through
agent/mcp_client.py -> the real MCP client/server protocol, never a direct
call into mcp_server/queries.py. See DECISIONS.md #3. If this list drifts
from the MCP tool signatures, tests/test_stress.py's
`test_tool_specs_match_mcp_tools` will fail on the next test run.
"""

from __future__ import annotations

OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_companies",
            "description": "List every company tracked in a given sector.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sector": {"type": "string", "description": "Sector slug, e.g. 'tech', 'retail', 'logistics'."}
                },
                "required": ["sector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_company_signals",
            "description": (
                "Get everything the database knows about one company: metrics "
                "(revenue, margin, headcount, etc.) and qualitative signals "
                "(hiring, layoffs, automation investment, expansion, etc.), each "
                "with its source. Returns found=false if the company is not tracked "
                "- treat that as 'no data,' never as license to guess."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "company_slug": {"type": "string", "description": "Company slug from list_companies, e.g. 'gxo'."}
                },
                "required": ["company_slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_companies",
            "description": "Compare one or more metrics across 2+ companies side by side.",
            "parameters": {
                "type": "object",
                "properties": {
                    "company_slugs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Company slugs to compare.",
                    },
                    "metric_name": {
                        "type": ["string", "null"],
                        "description": "Restrict to one metric, e.g. 'operating_margin_pct'. Omit for all metrics.",
                    },
                },
                "required": ["company_slugs"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_sector_context",
            "description": (
                "Get every qualitative signal recorded for companies in a sector - "
                "use this for sector-level questions instead of calling "
                "get_company_signals once per company."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sector": {"type": "string", "description": "Sector slug, e.g. 'logistics'."},
                    "signal_type": {
                        "type": ["string", "null"],
                        "description": "Restrict to one signal type, e.g. 'layoff' or 'automation_investment'. Omit for all.",
                    },
                },
                "required": ["sector"],
            },
        },
    },
]

TOOL_NAMES = {tool["function"]["name"] for tool in OPENAI_TOOLS}
