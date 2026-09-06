# Persona Agent

A persona-configurable financial analysis agent.

- Pick one of 3 personas: Mutual Fund Analyst, Equity Analyst, PE Analyst.
- Pick one of 3 sectors: Tech, Retail, Logistics.
- Ask a question. Get an answer grounded in a real SQLite database,
  retrieved exclusively through an MCP server, served identically through
  a Streamlit UI and a FastAPI endpoint.

Built for Automatisor's AI Engineer take-home.

- **LLM provider:** OpenAI, `gpt-4o-mini` by default, configurable via
  `OPENAI_MODEL`. Used through the official Python SDK.
- **API key needed to run this:** `OPENAI_API_KEY` only. See Setup below.
- Every design decision below is expanded on, with alternatives and
  tradeoffs, in **[DECISIONS.md](DECISIONS.md)**. That file is the actual
  "why." This README is setup, orientation, and the write-up that
  follows.
- Every `DECISIONS.md #N` mention below links straight to that numbered
  section, not just the top of the file.

## Write-up: schema, MCP design, and what I'd improve

The take-home asks for a short write-up here. The rest of this README
covers setup, running, and results. DECISIONS.md carries the full
alternatives-and-costs reasoning behind every point below.

**Schema.**

- Six tables (`db/schema.sql`): `sectors`, `companies`, `sources`,
  `company_metrics`, `company_signals`.
- The one decision worth explaining is what's *missing*: there is no
  `company<->source` junction table, even though an early planning pass
  assumed one.
- A source is a property of the specific claim it supports (for example,
  "GXO's FY2025 revenue is $13,178M, per this press release"), not an
  abstract many-to-many link between a company and a source. So
  `company_metrics` and `company_signals` each carry `source_id`
  directly.
- That's the more correct 3NF choice, and it makes the grounding
  guardrail a single join away from "why does the agent believe this."
- Cost: this schema cannot represent two sources corroborating the same
  fact. That's a real, accepted limitation, not an oversight. See
  [DECISIONS.md #2](DECISIONS.md#2-database-schema-direct-source-fk-on-every-fact-no-company-source-junction-table).
- `metric_name` is a closed vocabulary enforced by a SQL `CHECK`
  constraint, not free text. This is specifically so `compare_companies`
  never silently fails to match "Operating Margin" against "operating
  margin %." A metric that doesn't fit the vocabulary is recorded as a
  note on an adjacent row, or omitted outright, never mislabeled into the
  wrong bucket.

**MCP design.**

- `mcp_server/queries.py` is the only file in the repo that opens
  `db/agent.db`.
- `agent/core.py` never imports it. The agent reaches data exclusively
  through `agent/mcp_client.py`, which wraps `fastmcp.Client` against the
  real `mcp_server.server.mcp` instance and calls `client.call_tool(...)`,
  the actual MCP wire protocol, not a Python function call dressed up to
  look like one.
- This is verified, not just asserted: `tests/test_mcp_tools.py` calls
  every tool through `fastmcp.Client(mcp)` against the real database.
- Transport is in-memory by default. `fastmcp.Client` talks to the
  `FastMCP` object directly, skipping a process boundary, one fewer
  moving part for a 3-day build. Setting `MCP_SERVER_URL` switches the
  exact same code to a standalone HTTP server process with zero code
  changes. See
  [DECISIONS.md #3](DECISIONS.md#3-mcp-as-a-real-protocol-boundary-not-a-decorative-import).
- Four tools are exposed to the model: `list_companies`,
  `get_company_signals`, `compare_companies`, `search_sector_context`.
  All are read-only and scoped to this project's own tables.
- The actual prompt-injection defense is architectural: even a fully
  successful injection can't do more than make a wrong-but-harmless tool
  call. The wrapping prompt is defense-in-depth, not the primary defense,
  since it can't be expected to catch every phrasing. See
  [DECISIONS.md #9](DECISIONS.md#9-prompt-injection-resistance-is-architectural-not-a-keyword-blocklist).

**One thing I'd improve with more time.**

- `compute_confidence` (`agent/guardrails.py`) is a simple weighted
  formula: a 50/30/20 blend of resolution rate, evidence volume, and
  freshness.
- It is only unit-tested for the *properties* it should have (monotonic
  in each of those three inputs), never validated against a labeled set
  of "a human would call this high, medium, or low confidence" examples.
- This is a stated limitation, not a hidden one, but it's the piece of
  this project I'd trust least under real use. Two answers with very
  different real reliability could land at the same score if the
  formula's weights don't happen to capture what matters for a given
  question.
- Fixing it properly would mean building a small labeled eval set first
  (a dozen or so real query/answer pairs with a human-assigned confidence
  tier), then tuning or replacing the formula against that set, rather
  than only against its own internal consistency.
- Everything else on the "what I'd improve" list is in
  [DECISIONS.md's closing section](DECISIONS.md#what-id-improve-with-more-time).

## 1. Problem statement

Financial analysts need answers that are grounded in real, sourced data
and honest about their own gaps, not fluent-sounding guesses. This
project builds one configurable agent that can be asked the same
question through 3 different analytical lenses, always pulling from the
same underlying facts. When asked about something outside its database,
it says "I don't have data on that" instead of inventing an answer.

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

- The only two front doors are `api/main.py` and `ui/app.py`. Both call
  `agent.core.run_agent` and nothing else. See
  [DECISIONS.md #1](DECISIONS.md#1-one-agent-core-called-by-both-the-api-and-the-ui).
- The only file that opens the database is `mcp_server/queries.py`. The
  agent reaches it exclusively through the real MCP protocol. See
  [DECISIONS.md #3](DECISIONS.md#3-mcp-as-a-real-protocol-boundary-not-a-decorative-import).

## 3. Why this design

See **[DECISIONS.md](DECISIONS.md)** for the full reasoning, alternatives
considered, and costs of every choice below. This is a summary, not the
argument. Each item links straight to its section:

1. [One agent core shared by both interfaces.](DECISIONS.md#1-one-agent-core-called-by-both-the-api-and-the-ui)
2. [Direct source FK on every fact (no company<->source junction table);
   a closed vocabulary for metric names.](DECISIONS.md#2-database-schema-direct-source-fk-on-every-fact-no-company-source-junction-table)
3. [MCP as a real protocol boundary, verified with tests, not just
   convention.](DECISIONS.md#3-mcp-as-a-real-protocol-boundary-not-a-decorative-import)
4. [Personas encoded as decision criteria to foreground/downweight, not
   tone instructions.](DECISIONS.md#4-persona-differentiation-is-encoded-as-criteria-to-foregrounddownweight-not-tone-instructions)
5. [Confidence computed from the tool-call trace, never self-reported by
   the model.](DECISIONS.md#5-confidence-is-computed-from-the-tool-call-trace-never-self-reported-by-the-model)
6. [Every sourced fact traces to a URL fetched in this session; gaps are
   left empty, not estimated.](DECISIONS.md#6-every-sourced-fact-traces-to-a-url-actually-fetched-in-this-session-nothing-estimated-from-memory)
7. [Third-party package APIs (fastmcp, mcp, openai) verified against
   what's actually installed in this environment, not recalled from
   training.](DECISIONS.md#7-package-apis-were-verified-against-whats-actually-installed-not-recalled-from-training)
8. [The UI's sector dropdown goes through MCP but not through the full
   agent loop, since it isn't an analytical query.](DECISIONS.md#8-the-uis-sector-dropdown-calls-mcp-directly-not-through-run_agent)
9. [Prompt-injection resistance is architectural (a narrow, read-only
   tool surface) with instructional wrapping as defense-in-depth, not a
   keyword blocklist.](DECISIONS.md#9-prompt-injection-resistance-is-architectural-not-a-keyword-blocklist)

Bugs #10 through #17 are each a real failure found and fixed through live
testing, after the first submission-ready pass. They're not summarized
here since they're a timeline, not a standalone decision. Read them
directly:
[#10](DECISIONS.md#10-persona-reassertion-right-before-the-final-answer-is-drafted),
[#11](DECISIONS.md#11-few-shot-worked-examples-per-persona),
[#12](DECISIONS.md#12-tool-forcing-safeguard-for-a-skipped-required-lookup),
[#13](DECISIONS.md#13-proactive-detection-of-a-named-out-of-sector-company),
[#14](DECISIONS.md#14-tool-forcing-must-trigger-on-budget-exhaustion-not-only-on-voluntary-stop),
[#15](DECISIONS.md#15-a-malformed-tool-call-must-degrade-gracefully-not-crash-the-whole-request),
[#16](DECISIONS.md#16-a-nudge-is-a-request-not-a-constraint---forcing-tool_choice-is-what-actually-works),
[#17](DECISIONS.md#17-tool_choicerequired-constrains-that-a-tool-is-called-not-which-one).

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

Standalone, for manual testing. Not required for the API/UI, which
connect to it in-memory by default. See
[DECISIONS.md #3](DECISIONS.md#3-mcp-as-a-real-protocol-boundary-not-a-decorative-import).

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
any LLM. Useful to confirm the process is up before debugging further.

## 7. Running Streamlit

```bash
streamlit run ui/app.py
```

Select a persona and sector in the sidebar, ask a question, and expand
"MCP tool calls made" under the answer to see the actual tool-call trace
that produced it.

## 8. Data model / schema

- Full schema in `db/schema.sql`.
- Six tables: `sectors`, `companies`, `sources`, `company_metrics`,
  `company_signals`. No company<->source junction table. See
  [DECISIONS.md #2](DECISIONS.md#2-database-schema-direct-source-fk-on-every-fact-no-company-source-junction-table)
  for why.
- Every fact in `company_metrics`/`company_signals` carries a
  `source_id` and an `as_of_date`/`signal_date`.

## 9. Data sources and caveats

- 15 companies across 3 sectors:
  - Tech: Microsoft, Salesforce, Adobe, ServiceNow, Palantir.
  - Retail: Walmart, Target, Costco, Kroger, Best Buy.
  - Logistics: FedEx, UPS, XPO, Old Dominion Freight Line, GXO Logistics.
- The assignment PDF's own sample queries mention a fourth sector,
  Manufacturing, as an example. This project picked Tech, Retail, and
  Logistics instead, which the PDF explicitly permits ("pick any 3"). If
  you're adapting that one PDF sample query (Equity Analyst /
  Manufacturing / margin profile) to test this build, point it at one of
  the three sectors above instead. Manufacturing isn't in this database.
- All 15 companies are public, sourced from official earnings
  releases/10-Ks and, where a company's own release didn't have a
  figure, reputable financial data aggregators. Every one of the 16
  distinct sources is cited by URL on the fact it supports, and dated
  with when it was retrieved.

**Known gaps** (see
[DECISIONS.md #6](DECISIONS.md#6-every-sourced-fact-traces-to-a-url-actually-fetched-in-this-session-nothing-estimated-from-memory)
for the full list):

- Headcount is missing for 6 of the 15 companies. No source fetched for
  this project disclosed a precise figure.
- Costco and UPS have only quarterly, not full fiscal-year, revenue
  recorded.
- Both are left empty rather than estimated. This is a deliberate
  choice, not an oversight. The agent is instructed to treat an empty
  field the same way it treats an unknown company: say so, don't guess.

## 10. Persona design

| Persona | Foregrounds | Downweights |
|---|---|---|
| Mutual Fund Analyst | growth durability, benchmark-relative valuation, portfolio fit | leverage/deal mechanics, exit timing |
| Equity Analyst | earnings quality, margin trend, competitive position | portfolio construction, deal structuring |
| PE Analyst | cash generation, operational-improvement levers, entry thesis | benchmark relevance, quarterly earnings nuance |

Full criteria and the rendered prompt fragment: `agent/personas.py`. See
[DECISIONS.md #4](DECISIONS.md#4-persona-differentiation-is-encoded-as-criteria-to-foregrounddownweight-not-tone-instructions)
for why this is criteria-based rather than a tone instruction.

## 11. Example prompts and outputs

Real output from `scripts/print_cross_persona_example.py` against
`gpt-4o-mini`, run on 2026-09-04. Query: *"Is this sector a good place to
be putting money to work right now?"*, sector = `tech`, all 3 personas.
This is pasted verbatim from an actual run, not written by hand. Re-run
the script yourself with `OPENAI_API_KEY=sk-... python
scripts/print_cross_persona_example.py` to reproduce or refresh it.

**Equity Analyst** (7 tool calls: `search_sector_context` then
`get_company_signals` for 5 companies), confidence 0.86 (high):

> Adobe Inc. (ADBE): Operating Margin 36.7% (FY2025), Revenue $23.77B
> (+11% YoY digital media). Microsoft Corporation (MSFT): Operating
> Margin 45.6% (FY2025), Revenue $281.72B (+15% YoY). Palantir
> Technologies (PLTR): Revenue $4.48B (FY2025, +56.2% YoY), headcount
> +12.53% YoY. Salesforce (CRM): Operating Margin 19% GAAP / 33%
> non-GAAP, Revenue $37.89B (+9%), lower margins than peers, neutral
> outlook. ServiceNow (NOW): Operating Margin 13.5% GAAP / 31% non-GAAP,
> Revenue $13.28B (+21% YoY). **Recommendation: Constructive**,
> particularly supported by Microsoft's operating margin of 45.6%.

This is the differentiation the persona design is meant to produce:
margin-led, naming the specific metric behind the call, per
`agent/personas.py`'s "earnings quality and margin trend" foreground
criterion.

**Mutual Fund Analyst** and **PE Analyst**, same run, same question:

- Both made only the single `search_sector_context` call.
- Both produced answers built on the same two companies (Palantir,
  Adobe) and the same two qualitative signals (headcount growth, revenue
  growth), differing from each other mainly in phrasing rather than in
  which criteria they led with.
- **This was a real, then-open gap.** See
  [DECISIONS.md #4](DECISIONS.md#4-persona-differentiation-is-encoded-as-criteria-to-foregrounddownweight-not-tone-instructions)
  for the full account of two rounds of fixes (strengthening the system
  prompt, lowering sampling temperature) that narrowed but did not close
  it. An LLM's compliance with a soft instruction to make an optional
  extra tool call is probabilistic. The Equity Analyst complying while
  the other two didn't, on the identical prompt and model, is direct
  evidence of that.
- The mechanism works, the Equity Analyst answer above is proof, it just
  didn't fire reliably for every persona on every call at the time this
  section was written.
- The eventual, deterministic fix (a code-level gate rather than a
  prompt-level request) is what
  [DECISIONS.md #10 through #17](DECISIONS.md#10-persona-reassertion-right-before-the-final-answer-is-drafted)
  cover, the live-testing bug timeline that closes this gap for good.
  See section 12 below for the current, resolved status.

## 12. Known limitations

- **Resolved and confirmed.** The live OpenAI tool-calling loop and
  structured-output call were run against a real API key (`gpt-4o-mini`)
  on 2026-09-04, and found three real bugs plus one test-brittleness
  issue. Full account in
  [DECISIONS.md #7](DECISIONS.md#7-package-apis-were-verified-against-whats-actually-installed-not-recalled-from-training):
  1. `tool_choice="none"` without a `tools` array is rejected by the live
     API, even though it type-checks fine in the SDK.
  2. The confidence/grounding computation only recognized
     `get_company_signals`'s response shape, so answers built from
     `compare_companies` or `search_sector_context` were silently scored
     ungrounded with no sources, even when backed by real DB rows.
  3. `confidence` secretly depended on the model's self-reported
     `companies_referenced` field. A run where the model's answer
     correctly discussed real companies but left that one structured
     field empty collapsed confidence to `0.0/NONE`, while `grounded`
     correctly stayed `True`, a direct contradiction between two
     guardrail fields. Caught live and fixed by deriving both from the
     tool trace instead. A proactive code review after that fix found
     the same self-report dependency had been left in place one field
     over: the *displayed* `companies_referenced` list was still
     unioning in the model's self-reported field as a "safety net." That
     was corrected to be purely trace-derived too, before any further
     live testing. See
     [DECISIONS.md #7](DECISIONS.md#7-package-apis-were-verified-against-whats-actually-installed-not-recalled-from-training)
     for the full mechanism and the correction.
  - **Final confirmation, run by hand on the real machine on
    2026-09-04:** `pytest tests/ -v -m "not live"`, 26 passed, offline;
    `pytest tests/test_stress.py -v -m live`, 4 passed. **30 of 30 tests
    passing, live and offline, against the corrected code.** Not a
    projected result, the actual output of a real run.
- **Persona differentiation: originally unreliable, later closed by a
  deterministic fix.** See
  [DECISIONS.md #4](DECISIONS.md#4-persona-differentiation-is-encoded-as-criteria-to-foregrounddownweight-not-tone-instructions)
  and section 11 above for the real transcripts behind the original
  finding, and
  [DECISIONS.md #10 through #17](DECISIONS.md#10-persona-reassertion-right-before-the-final-answer-is-drafted)
  for the full live-testing timeline that eventually closed it: a
  code-level tool-forcing gate
  ([#12](DECISIONS.md#12-tool-forcing-safeguard-for-a-skipped-required-lookup),
  [#14](DECISIONS.md#14-tool-forcing-must-trigger-on-budget-exhaustion-not-only-on-voluntary-stop),
  [#16](DECISIONS.md#16-a-nudge-is-a-request-not-a-constraint---forcing-tool_choice-is-what-actually-works),
  [#17](DECISIONS.md#17-tool_choicerequired-constrains-that-a-tool-is-called-not-which-one))
  rather than a prompt-level request, replacing the two mitigations
  (stronger system prompt, lower sampling temperature) that only
  narrowed the gap without closing it.
- 6 of 15 companies are missing a headcount figure; 2 have only
  quarterly revenue. See
  [DECISIONS.md #6](DECISIONS.md#6-every-sourced-fact-traces-to-a-url-actually-fetched-in-this-session-nothing-estimated-from-memory).
- `compute_confidence` (agent/guardrails.py) is a simple weighted
  formula, unit-tested for the properties it should have (monotonic in
  resolution rate, evidence volume, and freshness), not calibrated
  against labeled examples.
- No corroborating multi-source facts. The schema supports exactly one
  source per fact by design. See
  [DECISIONS.md #2](DECISIONS.md#2-database-schema-direct-source-fk-on-every-fact-no-company-source-junction-table).

## 13. What I'd improve with more time

See the dedicated
**["What I'd improve with more time"](DECISIONS.md#what-id-improve-with-more-time)**
section at the end of DECISIONS.md. Kept there, not duplicated here, so
it stays attached to the reasoning it follows from.
