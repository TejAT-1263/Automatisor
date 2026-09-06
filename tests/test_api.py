"""
Tests for the FastAPI layer (api/main.py) itself, over a real HTTP
request via FastAPI's TestClient — not just calling agent.core.run_agent
directly the way tests/test_stress.py's test_api_response_structure does.

This file exists because a proactive review found api/main.py had zero
test coverage of its own: every existing check exercised run_agent
directly, so /health, request validation (422s from pydantic), the new
sector-validation 400, and the full request/response cycle through
FastAPI's routing had never actually been run, live or offline. See
DECISIONS.md #7's follow-up note for the full account.

The offline tests below need no network or API key — they either hit
/health or fail validation before request handling ever reaches
run_agent (invalid persona, empty query, unknown sector), so nothing here
calls OpenAI. The one exception is test_query_live_smoke, which needs a
real OPENAI_API_KEY and is skipped without one, same as
tests/test_stress.py.
"""

import os

import pytest
from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_query_rejects_invalid_persona():
    """persona is a Persona enum - a value outside it must fail FastAPI's
    request validation (422) before run_agent is ever called, not reach
    the agent and fail some other way."""
    response = client.post(
        "/query",
        json={"query": "What's the outlook here?", "persona": "not_a_real_persona", "sector": "tech"},
    )
    assert response.status_code == 422


def test_query_rejects_empty_query():
    response = client.post(
        "/query",
        json={"query": "", "persona": "equity_analyst", "sector": "tech"},
    )
    assert response.status_code == 422


def test_query_rejects_unknown_sector():
    """The sector-validation gap this test guards against: before this
    fix, any string passed pydantic's Field(min_length=1, max_length=64)
    and reached run_agent, burning a real LLM call just to come back as a
    low-confidence "I don't know" instead of a clean 4xx. This test needs
    no OPENAI_API_KEY, because a correctly-behaving endpoint must reject
    this request before ever constructing an LLM call."""
    response = client.post(
        "/query",
        json={"query": "What's the outlook here?", "persona": "equity_analyst", "sector": "not-a-real-sector"},
    )
    assert response.status_code == 400
    assert "not-a-real-sector" in response.json()["detail"]
    # the error should be genuinely useful, not just "invalid" - it must
    # name the sectors that ARE valid
    assert "tech" in response.json()["detail"]


@pytest.mark.live
def test_query_live_smoke():
    """One real request all the way through FastAPI's routing, pydantic
    validation, and the full agent loop - not just run_agent() called
    directly in-process. Requires a live OPENAI_API_KEY; run with:
        OPENAI_API_KEY=sk-... pytest tests/test_api.py -v -m live
    """
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("requires a live OPENAI_API_KEY")
    response = client.post(
        "/query",
        json={
            "query": "What's the most recent headcount or hiring signal you have for GXO?",
            "persona": "equity_analyst",
            "sector": "logistics",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["answer"], str) and body["answer"]
    assert isinstance(body["confidence"], float)
    assert isinstance(body["companies_referenced"], list)
    assert isinstance(body["sources"], list)
