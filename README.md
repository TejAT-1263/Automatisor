# Persona Agent

A persona-configurable financial analysis agent. Pick one of 3 personas
(Mutual Fund Analyst, Equity Analyst, PE Analyst) and one of 3 sectors
(Tech, Retail, Logistics); ask a question; get an answer grounded in a
real SQLite database, retrieved exclusively through an MCP server, served
identically through a Streamlit UI and a FastAPI endpoint.

Built for Automatisor's AI Engineer take-home. Every design decision below
is expanded on, with alternatives and tradeoffs, in **[DECISIONS.md](DECISIONS.md)**
— that file is the actual "why," this README is setup and orientation.

## 1. Problem statement

Revenue teams (and, in this exercise, financial analysts) need answers
that are grounded in real, sourced data and honest about their own gaps —
not fluent-sounding guesses. This project builds one configurable agent
that can be asked the same question through 3 different analytical
lenses, always pulling from the same underlying facts, and that will say
"I don't have data on that" instead of inventing an answer when asked
about something outside its database.

## 2. Architecture

```
 Streamlit UI  ──┐                    ┌── FastAPI  POST /query
 (ui/app.py)     │                    │   (api/main.py)
                 ▼                    ▼
            agent.core.run_agent(persona, sector, query)
                 │
                 │  OpenAI tool-calling loop (agent/core.py)
                 ▼
         agent/mcp_client.py  ──MCP protocol──▶  mcp_server/server.py
         (fastmcp.Client)                        (FastMCP tools)
                                                       │
                                                       ▼
                                              mcp_server/queries.py
                                                       │
                                                       ▼
                                                  db/agent.db (SQLite)
```

The only two front doors are `api/main.py` and `ui/app.py`; both call
`agent.core.run_agent` and nothing else (DECISIONS.md #1). The only file
that opens the database is `mcp_server/queries.py`; the agent reaches it
exclusively through the real MCP protocol (DECISIONS.md #3).

## 3. Why this design

See **DECISIONS.md** for the full reasoning, alternatives considered, and
costs of every choice below — this is a summary, not the argument:

1. One agent core shared by both interfaces.
2. Direct source FK on every fact (no company↔source junction table); a
   closed vocabulary for metric names.
3. MCP as a real protocol boundary — verified with tests, not just
   convention.
4. Personas encoded as decision criteria to foreground/downweight, not
   tone instructions.
5. Confidence computed from the tool-call trace, never self-reported by
   the model.
6. Every sourced fact traces to a URL fetched in this session; gaps are
   left empty, not estimated.
7. Third-party package APIs (fastmcp, mcp, openai) verified against what's
   actually installed in this environment, not recalled from training.
8. The UI's sector dropdown goes through MCP but not through the full
   agent loop, since it isn't an analytical query.
9. Prompt-injection resistance is architectural (a narrow, read-only tool
   surface) with instructional wrapping as defense-in-depth, not a
   keyword blocklist.

## 4. Setup

```bash
git clone <this-repo>
cd persona-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in OPENAI_API_KEY
python db/build_db.py  # builds db/agent.db from db/schema.sql + data/seed/*.json
```

`python db/build_db.py` prints a row-count summary when it succeeds:

```
Built db/agent.db
  sectors    3
  companies  15
  sources    16
  metrics    50
  signals    10
```

## 5. Running the MCP server

Standalone, for manual testing (not required for the API/UI, which
connect to it in-memory by default — see DECISIONS.md #3):

```bash
python -m mcp_server.server            # stdio transport
python -m mcp_server.server --http     # streamable-http on :8300
```

To point the API/UI at that standalone process instead of the in-memory
transport, set `MCP_SERVER_URL=http://127.0.0.1:8300/mcp` before running
them.

## 6. Running FastAPI

```bash
uvicorn api.main:app --reload --port 8000
```

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{
        "query": "Which companies here look like attractive buyout targets?",
        "persona": "pe_analyst",
        "sector": "logistics"
      }'
```

`GET /health` returns `{"status": "ok"}` without touching the database or
any LLM — useful to confirm the process is up before debugging further.

## 7. Running Streamlit

```bash
streamlit run ui/app.py
```

Select a persona and sector in the sidebar, ask a question, and expand
"MCP tool calls made" under the answer to see the actual tool-call trace
that produced it.

## 8. Data model / schema

Full schema in `db/schema.sql`. Six tables: `sectors`, `companies`,
`sources`, `company_metrics`, `company_signals` — no
company↔source junction table; see DECISIONS.md #2 for why. Every fact in
`company_metrics`/`company_signals` carries a `source_id` and an
`as_of_date`/`signal_date`.

## 9. Data sources and caveats

15 companies across Tech (Microsoft, Salesforce, Adobe, ServiceNow,
Palantir), Retail (Walmart, Target, Costco, Kroger, Best Buy), and
Logistics (FedEx, UPS, XPO, Old Dominion Freight Line, GXO Logistics) —
the assignment PDF's own sample queries mention a fourth sector,
Manufacturing, as an example; this project picked Tech/Retail/Logistics
instead, which the PDF explicitly permits ("pick any 3"). If you're
adapting that one PDF sample query (Equity Analyst / Manufacturing /
margin profile) to test this build, point it at one of the three sectors
above instead — Manufacturing isn't in this database. All 15 companies
are public, sourced from official earnings releases/10-Ks and (where
a company's own release didn't have a figure) reputable financial data
aggregators — every one of the 16 distinct sources is cited by URL on the
fact it supports, and dated with when it was retrieved.

**Known gaps** (see DECISIONS.md #6 for the full list): headcount is
missing for 6 of the 15 companies because no source fetched for this
project disclosed a precise figure; Costco and UPS have only quarterly,
not full fiscal-year, revenue recorded. These are left empty rather than
estimated — this is a deliberate choice, not an oversight, and the agent
is instructed to treat an empty field the same way it treats an unknown
company: say so, don't guess.

## 10. Persona design

| Persona | Foregrounds | Downweights |
|---|---|---|
| Mutual Fund Analyst | growth durability, benchmark-relative valuation, portfolio fit | leverage/deal mechanics, exit timing |
| Equity Analyst | earnings quality, margin trend, competitive position | portfolio construction, deal structuring |
| PE Analyst | cash generation, operational-improvement levers, entry thesis | benchmark relevance, quarterly earnings nuance |

Full criteria and the rendered prompt fragment: `agent/personas.py`.
DECISIONS.md #4 explains why this is criteria-based rather than a tone
instruction.

## 11. Example prompts and outputs

Real output from `scripts/print_cross_persona_example.py` against
`gpt-4o-mini`, run on 2026-09-04. Query: *"Is this sector a good place to
be putting money to work right now?"*, sector = `tech`, all 3 personas.
This is pasted verbatim from an actual run, not written by hand — re-run
the script yourself with `OPENAI_API_KEY=sk-... python
scripts/print_cross_persona_example.py` to reproduce or refresh it.

**Equity Analyst** (7 tool calls: `search_sector_context` then
`get_company_signals` for 5 companies) — confidence 0.86 (high):

> Adobe Inc. (ADBE): Operating Margin 36.7% (FY2025), Revenue $23.77B
> (+11% YoY digital media). Microsoft Corporation (MSFT): Operating
> Margin 45.6% (FY2025), Revenue $281.72B (+15% YoY). Palantir
> Technologies (PLTR): Revenue $4.48B (FY2025, +56.2% YoY), headcount
> +12.53% YoY. Salesforce (CRM): Operating Margin 19% GAAP / 33%
> non-GAAP, Revenue $37.89B (+9%) — lower margins than peers, neutral
> outlook. ServiceNow (NOW): Operating Margin 13.5% GAAP / 31% non-GAAP,
> Revenue $13.28B (+21% YoY). **Recommendation: Constructive** —
> particularly supported by Microsoft's operating margin of 45.6%.

This is the differentiation the persona design is meant to produce:
margin-led, names the specific metric behind the call, per
`agent/personas.py`'s "earnings quality and margin trend" foreground
criterion.

**Mutual Fund Analyst** and **PE Analyst**, same run, same question — both
made only the single `search_sector_context` call and produced answers
built on the same two companies (Palantir, Adobe) and the same two
qualitative signals (headcount growth, revenue growth), differing from
each other mainly in phrasing rather than in which criteria they led
with. **This is a real, currently-open gap, not smoothed over here** —
see DECISIONS.md #4 for the full account of two rounds of fixes
(strengthening the system prompt, lowering sampling temperature) that
narrowed but did not close it: an LLM's compliance with a soft
instruction to make an optional extra tool call is probabilistic, and the
Equity Analyst complying while the other two didn't, on the identical
prompt and model, is direct evidence of that. The mechanism works — the
Equity Analyst answer above is proof — it just doesn't fire reliably for
every persona on every call yet. DECISIONS.md #4's "what I'd improve"
entry proposes the actual fix: a deterministic code-level gate rather
than a prompt-level request.

## 12. Known limitations

- **Resolved and confirmed.** The live OpenAI tool-calling loop and
  structured-output call were run against a real API key (`gpt-4o-mini`)
  on 2026-09-04 and found three real bugs plus one test-brittleness issue
  — full account in DECISIONS.md #7: (1) `tool_choice="none"` without a
  `tools` array is rejected by the live API even though it type-checks
  fine in the SDK; (2) the confidence/grounding computation only
  recognized `get_company_signals`'s response shape, so answers built
  from `compare_companies` or `search_sector_context` were silently
  scored ungrounded with no sources even when backed by real DB rows; (3)
  `confidence` secretly depended on the model's self-reported
  `companies_referenced` field, so a run where the model's answer
  correctly discussed real companies but left that one structured field
  empty collapsed confidence to `0.0/NONE` while `grounded` correctly
  stayed `True` — a direct contradiction between two guardrail fields,
  caught live and fixed by deriving both from the tool trace instead. A
  proactive code review after that fix found the same self-report
  dependency had been left in place one field over — the *displayed*
  `companies_referenced` list was still unioning in the model's
  self-reported field as a "safety net" — and corrected it to be purely
  trace-derived too, before any further live testing; see DECISIONS.md #7
  for the full mechanism and the correction. **Final confirmation, run by
  hand on the real machine on 2026-09-04:** `pytest tests/ -v -m "not
  live"` — 26 passed, offline; `pytest tests/test_stress.py -v -m live` —
  4 passed. **30 of 30 tests passing, live and offline, against the
  corrected code** — not a projected result, the actual output of a real
  run.
- **Persona differentiation works but not reliably yet** — see DECISIONS.md
  #4 and section 11 above for the real transcripts. The Equity Analyst
  reliably pulls persona-specific metrics (margin, revenue) when it makes
  the follow-up tool call the system prompt asks for; the Mutual Fund and
  PE Analysts did not make that call in either of two live re-runs, so
  their answers currently differ from each other mostly in wording rather
  than in which data they lead with. Two mitigations (a stronger system
  prompt, lower sampling temperature) narrowed this without closing it —
  it's an LLM soft-instruction-compliance problem, not a code defect, and
  the real fix (a deterministic tool-loop gate) is scoped but not yet
  built, per DECISIONS.md #4's "what I'd improve" note.
- 6 of 15 companies are missing a headcount figure; 2 have only quarterly
  revenue. See DECISIONS.md #6.
- `compute_confidence` (agent/guardrails.py) is a simple weighted formula,
  unit-tested for the properties it should have (monotonic in resolution
  rate, evidence volume, and freshness), not calibrated against labeled
  examples.
- No corroborating multi-source facts — the schema supports exactly one
  source per fact by design (DECISIONS.md #2).

## 13. What I'd improve with more time

See the dedicated section at the end of **DECISIONS.md** — kept there,
not duplicated here, so it stays attached to the reasoning it follows
from.
