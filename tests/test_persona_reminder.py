"""
Unit tests for agent.core._build_pre_draft_notices — no LLM or network
access required, since this only assembles system-message strings.

Why this exists: manual UI testing on 2026-09-04 (bug #5 in
agent/core.py's docstring) found all 3 personas produced functionally
the same answer when comparing GXO to Microsoft, despite making
identical, sufficient tool calls — ruling out the tool-forcing fix
proposed for a different, earlier finding (DECISIONS.md #4). The real
gap: the system message injected immediately before Phase B drafts the
final answer ("Give your final answer now...") never mentioned persona
at all, while the grounding and sector-scope notices already got fresh
re-injection at that exact point. These tests pin down that the persona
reminder is now always present at this point (it isn't conditional the
way the other two notices are — it must run every time, since every
query is answered as some persona) and that it actually contains the
correct persona's own foreground criteria, not a generic reminder that
could pass for any persona.
"""

from agent.core import _build_pre_draft_notices
from agent.personas import PERSONA_DEFINITIONS
from agent.schemas import Persona


def test_persona_reminder_always_present_even_with_nothing_else_to_report():
    notices = _build_pre_draft_notices(Persona.EQUITY_ANALYST, "tech", [], [])
    assert len(notices) == 1  # no grounding/sector notice - just the persona reminder
    assert "Equity Analyst" in notices[0]


def test_persona_reminder_is_last_when_other_notices_present():
    """Recency is the entire point of this fix - if grounding or
    sector-scope notices exist, the persona reminder must still be the
    LAST thing before the "give your final answer" message, not buried
    under the others."""
    notices = _build_pre_draft_notices(Persona.PE_ANALYST, "logistics", ["not-a-real-co"], ["microsoft"])
    assert len(notices) == 3
    assert "GROUNDING NOTICE" in notices[0]
    assert "SECTOR SCOPE NOTICE" in notices[1]
    assert "PE Analyst" in notices[2]


def test_each_persona_reminder_contains_its_own_foreground_criteria_language():
    """Regression test for the actual live finding: all 3 personas
    produced the same margin-only comparison. Each persona's reminder
    must name that persona's OWN distinguishing concepts - not a generic
    "be different" message that would look the same across personas."""
    equity_notice = _build_pre_draft_notices(Persona.EQUITY_ANALYST, "tech", [], [])[0]
    mf_notice = _build_pre_draft_notices(Persona.MUTUAL_FUND_ANALYST, "tech", [], [])[0]
    pe_notice = _build_pre_draft_notices(Persona.PE_ANALYST, "tech", [], [])[0]

    assert "earnings quality" in equity_notice.lower()
    assert "growth durability" in mf_notice.lower() or "benchmark" in mf_notice.lower()
    assert "cash generation" in pe_notice.lower()

    # and they must genuinely differ from each other, not share one body
    assert equity_notice != mf_notice != pe_notice


def test_persona_reminder_explicitly_warns_against_the_observed_shortcut():
    """The observed failure mode was the model treating a narrow, factual
    question as fully answered by stating two numbers. The reminder
    should explicitly guard against that shortcut, not just restate the
    criteria the same way the initial system prompt already does."""
    notice = _build_pre_draft_notices(Persona.EQUITY_ANALYST, "tech", [], [])[0]
    assert "simple factual answer" in notice or "skip this" in notice


def test_all_three_personas_covered_by_definitions_used_here():
    """Sanity check that this test file and _build_pre_draft_notices are
    exercising all 3 real personas, not a stale subset."""
    assert set(PERSONA_DEFINITIONS.keys()) == {
        Persona.MUTUAL_FUND_ANALYST,
        Persona.EQUITY_ANALYST,
        Persona.PE_ANALYST,
    }
