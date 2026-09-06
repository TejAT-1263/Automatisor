"""
Read-only SQLite access for the MCP server.

This module is the ONLY code in the repo allowed to open db/agent.db.
The agent (agent/core.py) never imports this module directly — it only
reaches this data through mcp_server/server.py's MCP tools, over the MCP
client/server boundary (agent/mcp_client.py). See DECISIONS.md #3 for why
that separation is enforced rather than merely conventional.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _db_path() -> Path:
    configured = os.environ.get("DATABASE_PATH", "db/agent.db")
    path = Path(configured)
    return path if path.is_absolute() else REPO_ROOT / path


def _connect() -> sqlite3.Connection:
    path = _db_path()
    if not path.exists():
        raise FileNotFoundError(
            f"Database not found at {path}. Run `python db/build_db.py` first."
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def list_sectors() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT slug, name, description FROM sectors ORDER BY name").fetchall()
        return [dict(row) for row in rows]


def list_companies(sector_slug: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT c.slug, c.name, c.ticker, c.is_public, c.hq_location, c.description
               FROM companies c
               JOIN sectors s ON s.id = c.sector_id
               WHERE s.slug = ?
               ORDER BY c.name""",
            (sector_slug,),
        ).fetchall()
        return [dict(row) for row in rows]


def _company_id(conn: sqlite3.Connection, company_slug: str) -> int | None:
    row = conn.execute("SELECT id FROM companies WHERE slug = ?", (company_slug,)).fetchone()
    return row["id"] if row else None


def get_company_signals(company_slug: str) -> dict | None:
    """The core grounding lookup: everything this DB knows about one
    company — its metrics and its qualitative signals, each with the
    source that backs it. Returns None if the company is not in the DB
    (the caller uses that to trigger out-of-scope handling — see
    agent/guardrails.py)."""
    with _connect() as conn:
        company_row = conn.execute(
            """SELECT c.slug, c.name, c.ticker, c.is_public, c.hq_location,
                      c.description, s.name AS sector_name, s.slug AS sector_slug
               FROM companies c JOIN sectors s ON s.id = c.sector_id
               WHERE c.slug = ?""",
            (company_slug,),
        ).fetchone()
        if company_row is None:
            return None

        company_id = conn.execute(
            "SELECT id FROM companies WHERE slug = ?", (company_slug,)
        ).fetchone()["id"]

        metrics = conn.execute(
            """SELECT m.metric_name, m.value_numeric, m.unit, m.period, m.as_of_date,
                      m.notes, src.url AS source_url, src.publisher AS source_publisher,
                      src.retrieved_date AS source_retrieved_date
               FROM company_metrics m JOIN sources src ON src.id = m.source_id
               WHERE m.company_id = ?
               ORDER BY m.metric_name, m.as_of_date DESC""",
            (company_id,),
        ).fetchall()

        signals = conn.execute(
            """SELECT g.signal_type, g.description, g.signal_date,
                      src.url AS source_url, src.publisher AS source_publisher,
                      src.retrieved_date AS source_retrieved_date
               FROM company_signals g JOIN sources src ON src.id = g.source_id
               WHERE g.company_id = ?
               ORDER BY g.signal_date DESC""",
            (company_id,),
        ).fetchall()

        return {
            "company": dict(company_row),
            "metrics": [dict(row) for row in metrics],
            "signals": [dict(row) for row in signals],
        }


def compare_companies(company_slugs: list[str], metric_name: str | None = None) -> dict:
    """Side-by-side metric comparison. If metric_name is given, restricts
    to that one metric across companies; otherwise returns every metric
    for every requested company. Unknown slugs are reported separately
    rather than silently dropped, so the agent can be honest about which
    companies it could and couldn't find."""
    with _connect() as conn:
        found: dict[str, list[dict]] = {}
        missing: list[str] = []
        for slug in company_slugs:
            company_id = _company_id(conn, slug)
            if company_id is None:
                missing.append(slug)
                continue
            if metric_name:
                rows = conn.execute(
                    """SELECT m.metric_name, m.value_numeric, m.unit, m.period, m.as_of_date,
                              m.notes, src.url AS source_url, src.publisher AS source_publisher,
                              src.retrieved_date AS source_retrieved_date
                       FROM company_metrics m JOIN sources src ON src.id = m.source_id
                       WHERE m.company_id = ? AND m.metric_name = ?
                       ORDER BY m.as_of_date DESC""",
                    (company_id, metric_name),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT m.metric_name, m.value_numeric, m.unit, m.period, m.as_of_date,
                              m.notes, src.url AS source_url, src.publisher AS source_publisher,
                              src.retrieved_date AS source_retrieved_date
                       FROM company_metrics m JOIN sources src ON src.id = m.source_id
                       WHERE m.company_id = ?
                       ORDER BY m.metric_name, m.as_of_date DESC""",
                    (company_id,),
                ).fetchall()
            found[slug] = [dict(row) for row in rows]
        return {"companies": found, "unknown_slugs": missing}


def search_sector_context(sector_slug: str, signal_type: str | None = None) -> dict:
    """Aggregated signals across every company in a sector — the tool a
    persona uses to answer sector-level questions ("is this a good sector
    to deploy capital into right now") without the agent having to call
    get_company_signals once per company itself."""
    with _connect() as conn:
        sector_row = conn.execute(
            "SELECT slug, name, description FROM sectors WHERE slug = ?", (sector_slug,)
        ).fetchone()
        if sector_row is None:
            return {"sector": None, "signals": []}

        query = """
            SELECT c.slug AS company_slug, c.name AS company_name,
                   g.signal_type, g.description, g.signal_date,
                   src.url AS source_url, src.publisher AS source_publisher
            FROM company_signals g
            JOIN companies c ON c.id = g.company_id
            JOIN sources src ON src.id = g.source_id
            WHERE c.sector_id = (SELECT id FROM sectors WHERE slug = ?)
        """
        params: list = [sector_slug]
        if signal_type:
            query += " AND g.signal_type = ?"
            params.append(signal_type)
        query += " ORDER BY g.signal_date DESC"

        rows = conn.execute(query, params).fetchall()
        return {"sector": dict(sector_row), "signals": [dict(row) for row in rows]}
