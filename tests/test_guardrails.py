"""
Unit tests for agent/guardrails.py — no LLM or network access required.
These are the tests that matter most for the "no hallucination" bar: if
compute_confidence and build_grounding_notice are wrong, the LLM-facing
guardrails built on top of them are wrong too, regardless of how the
model behaves.
"""

from datetime import date, timedelta

from agent.guardrails import build_grounding_notice, compute_confidence
from agent.schemas import ConfidenceLevel


def test_confidence_zero_when_nothing_requested():
    score, level = compute_confidence(
        companies_requested=0, companies_found=0, supporting_rows=0, most_recent_as_of=None
    )
    assert score == 0.0
    assert level == ConfidenceLevel.NONE


def test_confidence_none_when_all_companies_unresolved():
    score, level = compute_confidence(
        companies_requested=2, companies_found=0, supporting_rows=0, most_recent_as_of=None
    )
    assert score == 0.0
    assert level == ConfidenceLevel.NONE


def test_confidence_capped_when_partially_unresolved():
    """One resolved company with great data, one unresolved: confidence
    must reflect the gap, not average it away."""
    fresh = date.today().isoformat()
    score, level = compute_confidence(
        companies_requested=2, companies_found=1, supporting_rows=10, most_recent_as_of=fresh
    )
    assert score <= 0.55
    assert level in (ConfidenceLevel.LOW, ConfidenceLevel.MEDIUM)


def test_confidence_high_when_fully_resolved_and_fresh():
    fresh = date.today().isoformat()
    score, level = compute_confidence(
        companies_requested=1, companies_found=1, supporting_rows=5, most_recent_as_of=fresh
    )
    assert level == ConfidenceLevel.HIGH
    assert score >= 0.75


def test_confidence_degrades_with_staleness():
    fresh = date.today().isoformat()
    stale = (date.today() - timedelta(days=900)).isoformat()

    fresh_score, _ = compute_confidence(
        companies_requested=1, companies_found=1, supporting_rows=5, most_recent_as_of=fresh
    )
    stale_score, _ = compute_confidence(
        companies_requested=1, companies_found=1, supporting_rows=5, most_recent_as_of=stale
    )
    assert stale_score < fresh_score


def test_grounding_notice_none_when_nothing_unresolved():
    assert build_grounding_notice([]) is None


def test_grounding_notice_names_the_unresolved_companies():
    notice = build_grounding_notice(["not-a-real-co"])
    assert notice is not None
    assert "not-a-real-co" in notice
    assert "NOT in the database" in notice
