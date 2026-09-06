-- Persona Agent — database schema
-- SQLite. See DECISIONS.md #2 for the reasoning behind every choice below.
--
-- Design summary: every fact the agent can state (a metric or a signal)
-- carries a direct foreign key to the source it came from. There is no
-- separate company<->source junction table — a source is a property of
-- the specific claim it supports, not a many-to-many relation between a
-- company and a source. This keeps "why does the agent believe X" a single
-- join away, which is what the grounding guardrail (agent/guardrails.py)
-- relies on.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- sectors: the 3 industries the agent can be scoped to.
-- ---------------------------------------------------------------------
CREATE TABLE sectors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL UNIQUE,   -- used in API/agent params, e.g. "logistics"
    name        TEXT NOT NULL UNIQUE,   -- display name, e.g. "Logistics & Fulfillment"
    description TEXT
);

-- ---------------------------------------------------------------------
-- sources: every URL a fact in this DB was pulled from. retrieved_date is
-- when *this project* looked at it (not the same as when it was published) -
-- that distinction matters for judging data freshness.
-- ---------------------------------------------------------------------
CREATE TABLE sources (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    url            TEXT NOT NULL UNIQUE,
    publisher      TEXT NOT NULL,
    source_type    TEXT NOT NULL CHECK (source_type IN (
                       'sec_filing', 'press_release', 'news_article',
                       'company_website', 'data_provider', 'earnings_report'
                   )),
    published_date TEXT,          -- ISO 8601 date the source itself was published; nullable (evergreen pages)
    retrieved_date TEXT NOT NULL  -- ISO 8601 date this project captured the fact
);

-- ---------------------------------------------------------------------
-- companies
-- ---------------------------------------------------------------------
CREATE TABLE companies (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sector_id         INTEGER NOT NULL REFERENCES sectors(id),
    slug              TEXT NOT NULL UNIQUE,   -- stable id used in MCP tool args
    name              TEXT NOT NULL,
    is_public         INTEGER NOT NULL CHECK (is_public IN (0, 1)),
    ticker            TEXT,                    -- NULL if private
    hq_location       TEXT,
    founded_year      INTEGER,
    description       TEXT NOT NULL,           -- 1-2 sentence factual description, sourced
    description_source_id INTEGER REFERENCES sources(id)
);

CREATE INDEX idx_companies_sector ON companies(sector_id);

-- ---------------------------------------------------------------------
-- company_metrics: quantitative facts. One row per (company, metric,
-- period). metric_name is a controlled vocabulary (see DECISIONS.md #2)
-- so the agent and the MCP tools can reason about units consistently
-- instead of parsing free text.
-- ---------------------------------------------------------------------
CREATE TABLE company_metrics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    INTEGER NOT NULL REFERENCES companies(id),
    metric_name   TEXT NOT NULL CHECK (metric_name IN (
                      'revenue', 'revenue_growth_yoy', 'gross_margin_pct',
                      'operating_margin_pct', 'headcount', 'headcount_growth_yoy_pct',
                      'market_cap', 'valuation'
                  )),
    value_numeric REAL NOT NULL,
    unit          TEXT NOT NULL,   -- 'usd_millions' | 'percent' | 'count'
    period        TEXT NOT NULL,   -- e.g. 'FY2025', 'Q2FY2026' — the reporting period the
                                    -- value covers; NOT necessarily a full fiscal year, so
                                    -- 'revenue' here means "revenue for `period`", quarterly
                                    -- or annual alike. See DECISIONS.md #2.
    as_of_date    TEXT NOT NULL,   -- ISO 8601 — the date the figure is true as of
    source_id     INTEGER NOT NULL REFERENCES sources(id),
    notes         TEXT
);

CREATE INDEX idx_metrics_company ON company_metrics(company_id);
CREATE INDEX idx_metrics_name ON company_metrics(metric_name);

-- ---------------------------------------------------------------------
-- company_signals: qualitative/event facts — the "labor strain, broken
-- processes, expansion plans" kind of signal the JD itself describes.
-- ---------------------------------------------------------------------
CREATE TABLE company_signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    INTEGER NOT NULL REFERENCES companies(id),
    signal_type   TEXT NOT NULL CHECK (signal_type IN (
                      'hiring', 'expansion', 'labor_strain', 'leadership_change',
                      'ma_activity', 'product_launch', 'layoff', 'automation_investment',
                      'other'
                  )),
    description   TEXT NOT NULL,
    signal_date   TEXT NOT NULL,   -- ISO 8601 — when the underlying event happened/was reported
    source_id     INTEGER NOT NULL REFERENCES sources(id)
);

CREATE INDEX idx_signals_company ON company_signals(company_id);
CREATE INDEX idx_signals_date ON company_signals(signal_date);
