"""
FastAPI layer. Deliberately thin: this file validates the request shape
and calls agent.core.run_agent — it contains no analysis logic of its
own. See DECISIONS.md #1.

Run:
    uvicorn api.main:app --reload --port 8000

Two things this file validates beyond request shape, both added after a
proactive review (no request had ever reached this endpoint in earlier
testing — every prior check called agent.core.run_agent directly, so
these gaps were real and untested until now, not just theoretical):

1. `sector` was previously just Field(min_length=1, max_length=64) — any
   string passed pydantic. A request for a nonexistent sector (a typo, a
   stale client) would silently reach run_agent, burn an LLM call, and
   come back as a low-confidence "I don't know" instead of a clean 4xx.
   _validate_sector now checks the request against the database's own
   list_sectors() (through MCPToolClient, the same MCP boundary
   DECISIONS.md #3 requires — never mcp_server.queries directly, mirroring
   ui/app.py's sector dropdown per DECISIONS.md #8) before run_agent is
   ever called.
2. The only exception this endpoint used to catch was FileNotFoundError
   (an unbuilt database). Anything else run_agent could raise — an
   OpenAI SDK error, a malformed LLM structured-output response — fell
   through as a raw FastAPI 500 with a full traceback in the response
   body, which is both an unhelpful error for a client and a needless
   internals leak. Now caught broadly and turned into a clean 502 with a
   short message; the real exception is still logged server-side (and
   chained via `from exc`), never silently swallowed.
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from agent.core import run_agent
from agent.mcp_client import MCPToolClient
from agent.schemas import AgentResponse, Persona

load_dotenv()

logger = logging.getLogger("persona_agent.api")

app = FastAPI(
    title="Persona Agent API",
    description="Persona-configurable financial analysis agent, grounded via MCP.",
    version="0.1.0",
)


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    persona: Persona
    sector: str = Field(min_length=1, max_length=64)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


async def _validate_sector(sector: str) -> None:
    """Raises HTTPException(400) if `sector` isn't a real sector slug.
    Goes through the MCP boundary (never mcp_server.queries directly),
    same as every other DB read in this project — see DECISIONS.md #3."""
    async with MCPToolClient() as client:
        sectors = await client.list_sectors()
    valid_slugs = {s["slug"] for s in sectors}
    if sector not in valid_slugs:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown sector '{sector}'. Valid sectors: {', '.join(sorted(valid_slugs))}.",
        )


@app.post("/query", response_model=AgentResponse)
async def query(request: QueryRequest) -> AgentResponse:
    try:
        await _validate_sector(request.sector)
    except FileNotFoundError as exc:
        # raised by mcp_server/queries.py if db/agent.db hasn't been built yet
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        return await run_agent(request.persona, request.sector, request.query)
    except FileNotFoundError as exc:
        # raised by mcp_server/queries.py if db/agent.db hasn't been built yet
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        # Anything else (an OpenAI SDK error, a malformed structured-output
        # response, ...) — caught broadly so a client gets a clean error
        # instead of a raw 500 with a traceback in the body. Logged with
        # the real exception so it's still visible server-side, not
        # silently swallowed.
        logger.exception("run_agent failed for persona=%s sector=%s", request.persona, request.sector)
        raise HTTPException(status_code=502, detail="The agent failed to produce an answer. See server logs.") from exc
