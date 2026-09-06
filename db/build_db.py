"""
Rebuild the persona-agent SQLite database from db/schema.sql and the
sourced seed files in data/seed/*.json.

Usage:
    python db/build_db.py [--db PATH]

This script is idempotent: it drops and recreates the target file every
run, so it is safe to re-run after editing a seed file. It never invents
data — every row it inserts is either a structural identifier (sector,
company slug/name) taken verbatim from the seed files, or a fact that
carries an explicit `source` object in the seed file. If a seed record is
missing a required source, this script raises rather than inserting a
fact with no provenance (see DECISIONS.md #2 and #6).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "db" / "schema.sql"
SEED_DIR = REPO_ROOT / "data" / "seed"
DEFAULT_DB_PATH = REPO_ROOT / "db" / "agent.db"


def load_seed_files() -> list[dict]:
    seed_files = sorted(SEED_DIR.glob("*.json"))
    if not seed_files:
        raise FileNotFoundError(f"No seed files found in {SEED_DIR}")
    return [json.loads(path.read_text()) for path in seed_files]


def get_or_create_source(conn: sqlite3.Connection, source: dict) -> int:
    """Sources are de-duplicated by URL: the same article/filing is often
    cited by several metrics or signals for the same company."""
    row = conn.execute("SELECT id FROM sources WHERE url = ?", (source["url"],)).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        """INSERT INTO sources (url, publisher, source_type, published_date, retrieved_date)
           VALUES (?, ?, ?, ?, ?)""",
        (
            source["url"],
            source["publisher"],
            source["source_type"],
            source.get("published_date"),
            source["retrieved_date"],
        ),
    )
    return cur.lastrowid


def build(db_path: Path) -> None:
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text())

    sector_ids: dict[str, int] = {}
    company_ids: dict[str, int] = {}
    counts = {"sectors": 0, "companies": 0, "sources": 0, "metrics": 0, "signals": 0}

    for sector_file in load_seed_files():
        sector = sector_file["sector"]
        cur = conn.execute(
            "INSERT INTO sectors (slug, name, description) VALUES (?, ?, ?)",
            (sector["slug"], sector["name"], sector.get("description")),
        )
        sector_ids[sector["slug"]] = cur.lastrowid
        counts["sectors"] += 1

        for company in sector_file["companies"]:
            desc_source_id = get_or_create_source(conn, company["description_source"])
            counts["sources"] += 1

            cur = conn.execute(
                """INSERT INTO companies
                   (sector_id, slug, name, is_public, ticker, hq_location,
                    founded_year, description, description_source_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sector_ids[sector["slug"]],
                    company["slug"],
                    company["name"],
                    1 if company["is_public"] else 0,
                    company.get("ticker"),
                    company.get("hq_location"),
                    company.get("founded_year"),
                    company["description"],
                    desc_source_id,
                ),
            )
            company_id = cur.lastrowid
            company_ids[company["slug"]] = company_id
            counts["companies"] += 1

            for metric in company.get("metrics", []):
                source_id = get_or_create_source(conn, metric["source"])
                conn.execute(
                    """INSERT INTO company_metrics
                       (company_id, metric_name, value_numeric, unit, period,
                        as_of_date, source_id, notes)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        company_id,
                        metric["metric_name"],
                        metric["value_numeric"],
                        metric["unit"],
                        metric["period"],
                        metric["as_of_date"],
                        source_id,
                        metric.get("notes"),
                    ),
                )
                counts["metrics"] += 1

            for signal in company.get("signals", []):
                source_id = get_or_create_source(conn, signal["source"])
                conn.execute(
                    """INSERT INTO company_signals
                       (company_id, signal_type, description, signal_date, source_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        company_id,
                        signal["signal_type"],
                        signal["description"],
                        signal["signal_date"],
                        source_id,
                    ),
                )
                counts["signals"] += 1

    conn.commit()

    # sources counted above double-counts dedup hits against the running
    # cursor; report the real row count instead of the loop counter.
    counts["sources"] = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    conn.close()

    print(f"Built {db_path.relative_to(REPO_ROOT)}")
    for key, value in counts.items():
        print(f"  {key:10s} {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Output SQLite file path")
    args = parser.parse_args()
    build(args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
