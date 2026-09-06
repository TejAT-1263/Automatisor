"""
Unit tests for agent.core._extract_evidence — no LLM or network access
required.

This function exists because the 4 MCP tools return 3 structurally
different response shapes, and two separate live runs against a real
OPENAI_API_KEY (2026-09-04) surfaced real bugs in how evidence was pulled
from the transcript:

1. The original implementation only understood get_company_signals's
   shape: any answer built from compare_companies or search_sector_context
   was silently scored as ungrounded with zero sources, even when it was
   built entirely from real, sourced DB rows.
2. `companies_requested`/`companies_found` (which drive `confidence`) were
   derived from LLMDraftAnswer.companies_referenced - a field the model
   self-reports in the same structured turn as its prose answer. When the
   model's answer correctly discussed real companies but left that
   separate list field empty, confidence silently collapsed to 0.0/NONE
   while `grounded` (computed purely from the trace) stayed True - two
   guardrail fields contradicting each other in the same response, despite
   this module's own docstring promising confidence is never self-reported.

Both are fixed here: `_extract_evidence` now also returns
`resolved_companies`, computed the same way `source_map`/`supporting_rows`
already are - from what the tools actually returned, never from what the
model claims it used. See DECISIONS.md #7 for the full account. These
tests pin down each response shape and the resolved-companies extraction
directly, so a future change to _dispatch or the query layer that breaks
one of them fails here, in under a second, rather than only showing up as
a mysterious `grounded=True, confidence=0.0` on a real API call.
"""

from agent.core import _extract_evidence
from agent.guardrails import wrap_untrusted


def _tool_message(payload: dict) -> dict:
    """Builds a transcript entry exactly the way _run_tool_loop does,
    including the wrap_untrusted delimiters _extract_evidence has to
    parse back out."""
    import json

    return {"role": "tool", "content": wrap_untrusted("TOOL RESULT", json.dumps(payload))}


def test_ignores_non_tool_messages():
    messages = [{"role": "system", "content": "irrelevant"}, {"role": "user", "content": "irrelevant"}]
    source_map, supporting_rows, most_recent, resolved_companies = _extract_evidence(messages)
    assert source_map == {}
    assert supporting_rows == 0
    assert most_recent is None
    assert resolved_companies == set()


def test_ignores_malformed_or_non_json_tool_content():
    messages = [{"role": "tool", "content": "not wrapped, not json"}]
    source_map, supporting_rows, most_recent, resolved_companies = _extract_evidence(messages)
    assert supporting_rows == 0
    assert resolved_companies == set()


def test_get_company_signals_shape_is_counted():
    """{"found": True, "company": {...}, "metrics": [...], "signals": [...]}
    — the shape this function was originally (and correctly) written
    against. The company's own slug must be captured as resolved."""
    payload = {
        "found": True,
        "company": {"slug": "fedex"},
        "metrics": [
            {
                "metric_name": "revenue",
                "value_numeric": 87693000000,
                "as_of_date": "2025-05-31",
                "source_url": "https://example.com/fedex-10k",
                "source_publisher": "FedEx Corp 10-K",
            }
        ],
        "signals": [
            {
                "signal_type": "automation_investment",
                "signal_date": "2025-03-01",
                "source_url": "https://example.com/fedex-automation",
                "source_publisher": "FedEx press release",
            }
        ],
    }
    source_map, supporting_rows, most_recent, resolved_companies = _extract_evidence([_tool_message(payload)])
    assert supporting_rows == 2
    assert source_map["https://example.com/fedex-10k"] == "FedEx Corp 10-K"
    assert source_map["https://example.com/fedex-automation"] == "FedEx press release"
    assert most_recent == "2025-05-31"
    assert resolved_companies == {"fedex"}


def test_get_company_signals_not_found_contributes_nothing():
    payload = {"found": False, "company_slug": "not-a-real-co"}
    source_map, supporting_rows, most_recent, resolved_companies = _extract_evidence([_tool_message(payload)])
    assert supporting_rows == 0
    assert source_map == {}
    assert resolved_companies == set()


def test_compare_companies_shape_is_counted():
    """{"companies": {slug: [rows]}, "unknown_slugs": [...]} — the shape
    that was previously invisible to this function entirely because it
    has no top-level "found" key. Both resolved slugs must be captured."""
    payload = {
        "companies": {
            "gxo": [
                {
                    "metric_name": "operating_margin_pct",
                    "value_numeric": 4.2,
                    "as_of_date": "2025-06-30",
                    "source_url": "https://example.com/gxo-10q",
                    "source_publisher": "GXO Logistics 10-Q",
                }
            ],
            "xpo": [
                {
                    "metric_name": "operating_margin_pct",
                    "value_numeric": 10.1,
                    "as_of_date": "2025-06-30",
                    "source_url": "https://example.com/xpo-10q",
                    "source_publisher": "XPO Inc 10-Q",
                }
            ],
        },
        "unknown_slugs": [],
    }
    source_map, supporting_rows, most_recent, resolved_companies = _extract_evidence([_tool_message(payload)])
    assert supporting_rows == 2
    assert "https://example.com/gxo-10q" in source_map
    assert "https://example.com/xpo-10q" in source_map
    assert most_recent == "2025-06-30"
    assert resolved_companies == {"gxo", "xpo"}


def test_compare_companies_unknown_slug_contributes_nothing():
    payload = {"companies": {}, "unknown_slugs": ["not-a-real-co"]}
    source_map, supporting_rows, most_recent, resolved_companies = _extract_evidence([_tool_message(payload)])
    assert supporting_rows == 0
    assert resolved_companies == set()


def test_compare_companies_resolved_company_with_zero_metric_rows_still_counts():
    """A company can be found (a real row in `companies`) but have zero
    metrics returned (e.g. metric_name filter matched nothing) - it should
    still count as resolved, since resolution and evidence volume are
    different questions."""
    payload = {"companies": {"gxo": []}, "unknown_slugs": []}
    source_map, supporting_rows, most_recent, resolved_companies = _extract_evidence([_tool_message(payload)])
    assert supporting_rows == 0
    assert resolved_companies == {"gxo"}


def test_search_sector_context_shape_is_counted():
    """{"sector": {...}, "signals": [...]} — the exact shape that produced
    a real false-negative in test_cross_persona_consistency: a Mutual
    Fund Analyst answer built entirely from this tool was scored
    grounded=False before that fix. Each signal's company_slug must be
    captured as resolved."""
    payload = {
        "sector": {"slug": "tech", "name": "Technology"},
        "signals": [
            {
                "company_slug": "palantir",
                "signal_type": "headcount_change",
                "signal_date": "2025-08-01",
                "source_url": "https://example.com/palantir-headcount",
                "source_publisher": "Palantir 10-Q",
            },
            {
                "company_slug": "adobe",
                "signal_type": "revenue_growth",
                "signal_date": "2025-07-15",
                "source_url": "https://example.com/adobe-earnings",
                "source_publisher": "Adobe Q3 earnings release",
            },
        ],
    }
    source_map, supporting_rows, most_recent, resolved_companies = _extract_evidence([_tool_message(payload)])
    assert supporting_rows == 2
    assert source_map["https://example.com/palantir-headcount"] == "Palantir 10-Q"
    assert most_recent == "2025-08-01"
    assert resolved_companies == {"palantir", "adobe"}


def test_search_sector_context_no_matching_sector_contributes_nothing():
    payload = {"sector": None, "signals": []}
    source_map, supporting_rows, most_recent, resolved_companies = _extract_evidence([_tool_message(payload)])
    assert supporting_rows == 0
    assert resolved_companies == set()


def test_list_companies_shape_is_never_miscounted_as_compare_companies():
    """_dispatch wraps list_companies's result as {"companies": [...]} —
    a LIST, not a dict keyed by slug. This must not be misread as
    compare_companies's {"companies": {slug: [...]}} shape and crash or
    silently count list entries as evidence rows or resolved companies -
    a company merely appearing in a directory listing has not actually
    been looked up for any data."""
    payload = {"companies": [{"slug": "fedex", "name": "FedEx"}, {"slug": "ups", "name": "UPS"}]}
    source_map, supporting_rows, most_recent, resolved_companies = _extract_evidence([_tool_message(payload)])
    assert supporting_rows == 0
    assert source_map == {}
    assert resolved_companies == set()


def test_multiple_tool_calls_across_shapes_accumulate():
    """A realistic multi-turn transcript: one search_sector_context call
    followed by one get_company_signals call. Evidence and resolved
    companies from both must be counted, not just the last one seen."""
    sector_payload = {
        "sector": {"slug": "logistics"},
        "signals": [
            {
                "company_slug": "fedex",
                "signal_date": "2025-01-01",
                "source_url": "https://example.com/a",
                "source_publisher": "Publisher A",
            }
        ],
    }
    company_payload = {
        "found": True,
        "company": {"slug": "gxo"},
        "metrics": [
            {"as_of_date": "2025-06-01", "source_url": "https://example.com/b", "source_publisher": "Publisher B"}
        ],
        "signals": [],
    }
    messages = [_tool_message(sector_payload), _tool_message(company_payload)]
    source_map, supporting_rows, most_recent, resolved_companies = _extract_evidence(messages)
    assert supporting_rows == 2
    assert set(source_map) == {"https://example.com/a", "https://example.com/b"}
    assert most_recent == "2025-06-01"
    assert resolved_companies == {"fedex", "gxo"}


def test_row_missing_source_url_is_not_counted():
    """A row that somehow lacks a source_url (should not happen given the
    schema's NOT NULL FK, but the parser must not crash or fabricate a
    source for it) contributes nothing as evidence, but the company is
    still resolved - a company can genuinely exist with zero sourced
    metrics for the field queried, and that's a different fact than "this
    company was never looked up.\""""
    payload = {
        "found": True,
        "company": {"slug": "fedex"},
        "metrics": [{"as_of_date": "2025-01-01"}],
        "signals": [],
    }
    source_map, supporting_rows, most_recent, resolved_companies = _extract_evidence([_tool_message(payload)])
    assert supporting_rows == 0
    assert source_map == {}
    assert resolved_companies == {"fedex"}


def test_resolved_companies_survive_even_when_model_self_report_would_not():
    """Regression test for the actual live bug: a transcript where real
    tool data was retrieved for two companies must yield those two slugs
    in resolved_companies regardless of anything the model later claims
    in LLMDraftAnswer.companies_referenced - this function never looks at
    that field at all, which is the point."""
    payload = {
        "sector": {"slug": "tech"},
        "signals": [
            {
                "company_slug": "palantir",
                "signal_date": "2025-08-01",
                "source_url": "https://example.com/palantir",
                "source_publisher": "Bullfincher",
            },
            {
                "company_slug": "adobe",
                "signal_date": "2025-07-15",
                "source_url": "https://example.com/adobe",
                "source_publisher": "AnalystLens",
            },
        ],
    }
    _, _, _, resolved_companies = _extract_evidence([_tool_message(payload)])
    # Even if a caller passed an empty draft.companies_referenced (the
    # observed live failure mode), resolved_companies alone still proves
    # 2 companies were genuinely looked up.
    assert resolved_companies == {"palantir", "adobe"}
    assert len(resolved_companies) == 2
