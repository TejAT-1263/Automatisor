"""
Pydantic models for the agent's structured response.

Three things are deliberately NOT trusted from the LLM's own output:
`confidence`, `grounded`, and (as of the fix described in agent/core.py's
module docstring and tests/test_core_evidence.py) the displayed
`AgentResponse.companies_referenced` list. All three are computed in
agent/core.py / agent/guardrails.py from the actual MCP tool-call trace,
not asked of the model — see DECISIONS.md #5 for why a self-reported
confidence score is not a guardrail. LLMDraftAnswer.companies_referenced
below still exists and is still populated by the model, but only as a
soft nudge (asking the model to name its sources tends to make it more
careful about using them) — agent/core.py's run_agent() does not read it
for anything shown to the user or fed into confidence math.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Persona(str, Enum):
    MUTUAL_FUND_ANALYST = "mutual_fund_analyst"
    EQUITY_ANALYST = "equity_analyst"
    PE_ANALYST = "pe_analyst"


class ConfidenceLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"  # no grounding data was found at all


class SourceRef(BaseModel):
    url: str
    publisher: str


class LLMDraftAnswer(BaseModel):
    """What the LLM itself is asked to produce, via a JSON-schema-constrained
    final turn. Deliberately narrow: it supplies the prose and which
    companies/sources it used, and nothing else. Confidence, grounding
    status, and the tool-call trace are computed independently.

    NOTE: companies_referenced below is asked of the model as a self-check
    nudge only. AgentResponse.companies_referenced (the field actually
    shown in the UI/API) is computed purely from the tool-call trace in
    agent/core.py and never reads this field - see the module docstring
    above for why."""

    answer: str = Field(description="The persona-voiced answer to the user's query.")
    companies_referenced: list[str] = Field(
        default_factory=list,
        description="Slugs of companies (from get_company_signals/compare_companies results) the answer actually draws on.",
    )
    limitations: list[str] = Field(
        default_factory=list,
        description="Specific gaps the model noticed in the data it retrieved (e.g. 'no headcount figure for Best Buy').",
    )


class AgentResponse(BaseModel):
    """The full structured response returned by both the API and the
    Streamlit UI — the shape the take-home's API-structure test checks."""

    answer: str
    persona: Persona
    sector: str
    companies_referenced: list[str] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_level: ConfidenceLevel
    limitations: list[str] = Field(default_factory=list)
    grounded: bool = Field(
        description="True only if at least one MCP tool call returned real data used in the answer."
    )
    tool_calls: list[str] = Field(
        default_factory=list,
        description="Ordered log of MCP tool calls made while answering, e.g. 'get_company_signals(gxo)'. Proof MCP was load-bearing, not decorative.",
    )
