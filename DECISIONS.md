# Design decisions

This file exists because the take-home explicitly grades "how you think and
structure the system," not just whether it runs. Each entry below covers
what was decided, what else was considered, and what it costs. Code comments
reference these by number (`DECISIONS.md #3`) at the point the decision
actually matters, rather than everything living only here.

Two shapes of entry appear below:
- **Design decisions** (mostly #1-#9): decision, alternative(s) considered,
  why not, cost.
- **Live-testing bug entries** (#10-#17): a real failure found by actually
  running the agent, read as a short timeline - what happened, root cause,
  fix, verification.

## 1. One agent core, called by both the API and the UI

- **Decision:** `agent/core.py::run_agent(persona, sector, query)` is the
  only function either front door calls. `api/main.py` and `ui/app.py` both
  import it and do nothing else with the agent - no duplicated
  prompt-building, no duplicated tool-calling loop.
- **Alternative considered:** implement the tool loop and persona logic
  separately in each front door, since a Streamlit app and a FastAPI
  endpoint have different natural shapes (sync vs async, form vs JSON).
- **Why not:** the take-home explicitly names this as a graded criterion,
  "don't build two separate implementations," and it's also just correct
  engineering. Two implementations of the same reasoning would drift the
  first time someone fixes a bug in one and forgets the other.
- **Cost:** `ui/app.py` has to bridge Streamlit's synchronous execution
  model to `run_agent`'s async signature with `asyncio.run()` per call (see
  `_run_query` in `ui/app.py`). That's a small, contained cost, not a
  design compromise.
- **Open tradeoff, decided but worth restating:** Streamlit calls
  `agent.core.run_agent` in-process rather than making an HTTP call to the
  FastAPI server. Both satisfy "both paths hit the same underlying agent."
  In-process was chosen because it's simpler for a 3-day build and doesn't
  require running two processes for the UI to work. Going through the API
  instead would be closer to how a real multi-service product would
  separate concerns, and would mean the UI has zero direct Python
  dependency on the agent package. If this became a real product, I'd
  switch the UI to call the API over HTTP, noted in the README's "what I'd
  improve" section, not treated as settled.

## 2. Database schema: direct source FK on every fact, no company-source junction table

- **Decision:** `company_metrics` and `company_signals` each carry
  `source_id` directly (`db/schema.sql`). There's no `company_source_links`
  many-to-many table, even though an earlier planning pass (before any
  code was written) assumed one.
- **Why the plan changed:** a source is a property of the specific claim
  it supports ("GXO's FY2025 revenue is $13,178M, per this press
  release"), not a many-to-many relationship between a company and a
  source in the abstract. Modeling it as a junction table would let the
  schema represent states that don't make sense here, such as a source
  "linked" to a company with no specific fact attached. A direct FK on
  each fact row is the more correct 3NF choice, and it's what makes the
  grounding guardrail (`agent/guardrails.py`) a single join away from "why
  does the agent believe this."
- **Cost / known limitation:** if two different sources ever corroborate
  the exact same fact, this schema has no way to represent "two sources
  agree," only two separate metric rows. That's fine at this data volume,
  a few dozen well-sourced records, and it's called out in the README
  rather than silently accepted as scope creep to fix.

**`metric_name` is a closed vocabulary** (`revenue`, `revenue_growth_yoy`,
`gross_margin_pct`, `operating_margin_pct`, `headcount`,
`headcount_growth_yoy_pct`, `market_cap`, `valuation`, enforced by a SQL
`CHECK` constraint), not free text.

- **Alternative:** store metrics as `(label, value)` pairs with whatever
  label a source happened to use.
- **Why not:** free-text metric names would make `compare_companies`
  unreliable. "Operating Margin" vs "operating margin %" vs "Op. Margin"
  would silently fail to compare. A closed vocabulary forces every new
  metric type into a deliberate schema change instead of a silent typo.
- **Cost:** metrics I found in a source but that don't fit the vocabulary
  (e.g. Walmart's operating-income *growth rate* without an absolute
  operating-income figure to compute a margin from) are recorded as a
  `notes` field on an adjacent metric rather than as their own row, or
  omitted - see `data/seed/retail.json`'s Walmart entry. That's a real
  tradeoff: some real, sourced facts aren't queryable as structured
  metrics. It's also intentionally the more honest failure mode. I would
  rather omit a fact than force it into a category it doesn't fit and
  mislabel it.

**`period` is a free string** (`FY2025`, `Q2FY2026`, etc.) rather than a
strict fiscal-year integer column, specifically because the sourced data is
a genuine mix of full fiscal years and quarters (Costco and UPS only had
recent quarterly figures available at sourcing time, see #6). Every row's
`notes` field says explicitly when a figure is quarterly rather than
annual, so a quarterly revenue number is never silently compared against an
annual one without that being visible.

## 3. MCP as a real protocol boundary, not a decorative import

- **Decision:** `mcp_server/queries.py` is the only file in the repo that
  opens `db/agent.db`. `agent/core.py` never imports it. The agent reaches
  data exclusively through `agent/mcp_client.py`, which wraps
  `fastmcp.Client` against the real `mcp_server.server.mcp` instance and
  calls `client.call_tool(...)`, the actual MCP wire protocol, not a
  Python function call dressed up to look like one.
- **How this is verified, not just asserted:** `tests/test_mcp_tools.py`
  calls every tool through `fastmcp.Client(mcp)` and checks real return
  shapes against the real database, including the not-found path. If
  `agent/core.py` ever imported `mcp_server.queries` directly, that would
  be a regression these tests wouldn't catch by themselves, since there's
  no lint rule enforcing the import boundary in a 3-day build. A `grep -r
  "from mcp_server.queries" agent/` before submitting is the actual check,
  and finding nothing there when this was written is what "the boundary
  holds" means concretely.
- **Transport: in-memory by default.** `agent/mcp_client.py` passes the
  `FastMCP` object directly to `fastmcp.Client(...)` rather than spawning
  a subprocess speaking stdio, or standing up a second HTTP server. This
  still goes through the real `fastmcp.Client` / MCP protocol classes,
  verified directly in this session (see `DECISIONS.md #7`) by
  round-tripping a tool call and inspecting the result object, not
  assumed from memory of an older fastmcp version. It just skips a
  process boundary, which matters for a 3-day build (one fewer moving
  part to keep alive in dev) without weakening the actual
  protocol-boundary property being graded. Setting the `MCP_SERVER_URL`
  environment variable switches this to a standalone `python -m
  mcp_server.server --http` process with no code change.
  `agent/mcp_client.py::MCPToolClient._transport()` is the one place this
  decision lives.

## 4. Persona differentiation is encoded as criteria to foreground/downweight, not tone instructions

- **Decision:** `agent/personas.py::PERSONA_DEFINITIONS` stores, per
  persona, which decision criteria to lead with and which to explicitly
  downweight, plus a `recommendation_style`. The rendered system-prompt
  fragment (`render_persona_instructions`) tells the model this directly
  and adds an explicit self-check: "if your answer would read the same
  regardless of persona, you have not done this correctly."
- **Why not a "respond like a PE analyst would" one-liner:** that's
  exactly the failure mode the take-home calls out, "not just a cosmetic
  tone change." A tone instruction changes adjectives; a criteria
  instruction changes which tool calls the model makes and which fields
  of the `get_company_signals`/`compare_companies` results it leads with.

**Verification, done, and the result was not a clean pass.**
`test_cross_persona_consistency`'s check (three non-identical answer
strings) is necessary but not sufficient for "reasoning differs," exactly
as flagged above, and reading the real transcripts by hand confirmed that
gap is real, not hypothetical:

- **First live run, default sampling settings:** all 3 personas made
  exactly one identical tool call (`search_sector_context(sector=tech)`),
  retrieved only qualitative signals (that tool never returns financial
  metrics, see the bug fix below), and produced three answers that differ
  only in wording, not in which facts or criteria they lead with. The test
  passed (the 3 strings weren't literally identical) while the actual
  grading criterion, "not just a cosmetic tone change," was not met. This
  is a case where the test's necessary-but-insufficient check is exactly
  what let a real shortfall through undetected.
- **Root cause, traced precisely:** `search_sector_context` only queries
  `company_signals` (hiring, layoffs, automation investment). It never
  joins `company_metrics`, so margin, valuation, and cash-generation data
  (the exact numbers each persona's `foreground` list points at) were
  never in context for any persona to differentiate on. The system prompt
  correctly said "reach for different fields from
  get_company_signals/compare_companies," but nothing forced a second
  tool call once the first one already produced something that reads like
  a complete answer.
- **First fix, strengthened system prompt**
  (`agent/core.py::_build_system_prompt`): added an explicit rule that a
  `search_sector_context`-only lookup is insufficient grounding when the
  persona's foreground criteria reference a financial metric, and must be
  followed by `get_company_signals`/`compare_companies`. Re-run: 1 of 3
  personas (Equity Analyst) complied, 7 tool calls, 5 companies, real
  operating margins (Adobe 36.7%, Microsoft 45.6%, Salesforce 19%/33%
  GAAP/non-GAAP, ServiceNow 13.5%/31%) and a genuinely metric-grounded,
  differentiated answer. The Mutual Fund Analyst and PE Analyst did not:
  same system prompt, same model, no follow-up call either time.
- **Second fix, `temperature=0.2` on the tool-loop call** (down from the
  API default), on the reasoning that tool-call/continue-vs-stop
  decisions should be more consistent at lower sampling temperature.
  Re-run: still inconsistent. This time the Equity Analyst complied again
  (2 tool calls) but the Mutual Fund Analyst and PE Analyst again stopped
  after one `search_sector_context` call. The Mutual Fund Analyst's
  answer that run also ended with a clarifying question back to the user
  ("Would you like me to provide that information for specific
  companies?") instead of committing to an answer, itself informative:
  Phase B's JSON-schema constraint enforces the *shape* of a final
  answer, not that its content actually be a completed answer rather than
  a deferral.

- **Status at the time this entry was first written:** the mechanism to
  differentiate personas on substance existed and had been shown to work
  in one run, but did not yet work reliably across all 3 personas on every
  call. That gap turned out to be deeper and to have more than one
  distinct cause than this entry originally described. See #10, #11, #12,
  and #13 below for the full, honest account of each further live-testing
  round that found a new failure mode, what was actually wrong, and what
  fixed it. This entry is left as written at the time rather than
  rewritten in hindsight, since the wrong turns are part of the real
  record.

## 5. Confidence is computed from the tool-call trace, never self-reported by the model

- **Decision:** `agent/schemas.py::LLMDraftAnswer`, the JSON shape the
  model is constrained to produce, has no `confidence` field.
  `AgentResponse`'s `confidence`, `confidence_level`, and `grounded`
  fields are filled in by `agent/guardrails.py::compute_confidence`, which
  only sees three things: how many of the companies the agent tried to
  look up actually resolved, how many metric/signal rows backed the
  answer, and how stale the most recent one is.
- **Why not ask the model for a confidence number:** a self-reported
  confidence score is not a guardrail. It's the same model that might
  hallucinate a fact also deciding how confident to sound about it, with
  no independent signal grounding the number. A model under-confident or
  over-confident about its own retrieval is a well-documented failure
  mode, and there's no way to catch it after the fact if the number came
  from the same generation as the answer. Computing it from the actual
  tool-call results means confidence can be wrong (the formula is a
  simple weighted score, not a calibrated model, see the docstring in
  `compute_confidence`), but it can't be rationalized by the LLM the way
  a self-reported number could be.
- **Cost:** the formula is genuinely simple (a 50/30/20 weighted blend of
  resolution rate, evidence volume, and freshness) and not statistically
  validated against anything. That's stated in the function's own
  docstring and in the README rather than presented as more rigorous than
  it is.

**Three separate guardrail mechanisms exist rather than one "be careful"
system-prompt instruction**, specifically so each has an independent,
testable failure mode:

- `compute_confidence`, tested in `tests/test_guardrails.py` with 5 cases
  covering zero-data, partial-resolution, full-resolution, and staleness,
  all without touching the network.
- `build_grounding_notice`, turns an unresolved lookup into an explicit,
  named instruction ("X is NOT in the database") rather than trusting the
  model to infer scope from an empty tool result.
- `SYSTEM_SAFETY_RULES` / `wrap_untrusted`, see #9 below.

## 6. Every sourced fact traces to a URL actually fetched in this session, nothing estimated from memory

- **Decision:** every row in `data/seed/*.json` carries a `source` object
  with the URL that was fetched (via web search and page fetch, not
  recalled from training data) to get that number, and a
  `retrieved_date`. Where a source didn't have a figure I needed, Best
  Buy's exact revenue, Costco's full fiscal-year 2025 total, Palantir's
  exact HQ founding year, that field is simply absent from the seed data
  rather than filled with a plausible estimate. `notes` fields flag every
  derived number (e.g. Old Dominion's operating margin, computed as `100
  - operating ratio`, since LTL carriers report operating ratio, not
  margin) and every company-stated approximation ("more than 400,000
  associates") as exactly that, not as a precise figure.
- **Sector choice: Tech, Retail, Logistics**, not the Manufacturing option
  the take-home also offers. Logistics was chosen specifically because
  it's the closest public-market proxy to Automatisor's own product
  (contract warehousing/fulfillment operators like GXO and parcel/LTL
  carriers like FedEx, UPS, XPO, and Old Dominion all show up directly in
  signals like automation investment and layoffs tied to automation), and
  because it's the sector used in the take-home's own worked PE-analyst
  example and its API-structure test (`sector=logistics`). Tech and
  Retail were chosen for data availability, large public companies with
  well-documented, easy-to-verify quarterly/annual figures, over
  harder-to-source options, per the take-home's own note that they care
  about structure over data exhaustiveness.
- **Known gaps, stated rather than hidden:** headcount is missing for
  Microsoft, Salesforce, Adobe, ServiceNow, Target, and Costco. None of
  the sources fetched for this project disclosed a precise figure. Costco
  and UPS only have quarterly (not full fiscal-year) revenue recorded,
  because that's what was available in the sources reachable in this
  session at build time. These are exactly the kind of gaps
  `get_company_signals` should surface honestly if asked about, see the
  out-of-scope/limitations handling in #5, rather than a reason to
  fabricate a plausible number to fill the row.

## 7. Package APIs were verified against what's actually installed, not recalled from training

- **Decision:** `fastmcp` (4.0.2), `mcp` (2.1.1), and `openai` (3.8.0) in
  `requirements.txt` are all newer than what I have reliable training
  knowledge of, enough that I do not trust my own memory of their APIs.
  Before writing `mcp_server/server.py`, `agent/mcp_client.py`, and
  `agent/core.py`, I ran `inspect.signature(...)` against the actually
  installed `FastMCP.__init__`, `FastMCP.tool`, `Client.__init__`,
  `Client.call_tool`, and `OpenAI.chat.completions.create` in this
  session, and did a live round-trip tool call to confirm the
  `CallToolResult` shape (`.data`, `.is_error`) rather than assuming it.
  That's why the code reads the way it does (e.g. `result.data` rather
  than parsing `result.content[0].text` as JSON by hand).

**Update, run against a live API key on 2026-09-04.** This was the one
genuinely untested path at first submission-readiness, and running it
found two real bugs, not zero. Both are fixed and covered by tests; the
account below is deliberately specific rather than "it works now," since
what the bugs were is more informative than that they existed.

1. **`tool_choice="none"` without a `tools` array is rejected outright by
   the live API** (`Invalid value for 'tool_choice': 'tool_choice' is only
   allowed when 'tools' are specified`), a constraint that isn't visible
   from the SDK's Python type signature, only from a real request. All 4
   live tests failed on this on the first run.
   - **Fix:** the parameter was dropped from the Phase B call in
     `agent/core.py`. Phase B never passed `tools` to begin with, so the
     model already couldn't call one there. `tool_choice="none"` was
     redundant, not protective, so removing it costs nothing.
2. **The evidence-extraction step that computes
   `grounded`/`sources`/`confidence` only understood
   `get_company_signals`'s response shape.** `compare_companies` and
   `search_sector_context` return structurally different dicts (no
   top-level `"found"` key; `compare_companies`'s rows didn't even carry
   source info at the database layer, see the fix to
   `mcp_server/queries.py` below). This meant any answer built primarily
   from those two tools was silently scored `grounded=False` with
   `sources=[]`, even when it was entirely backed by real, sourced DB
   rows. This is a materially worse bug than #1: it wouldn't crash
   anything, it would just quietly under-report confidence and drop
   sources for roughly half the tool surface, on every wrong-shaped
   response, indefinitely, with no error or warning to notice by. It's
   also exactly the kind of bug static review and offline unit tests
   don't catch on their own, since each shape is valid Python and valid
   JSON individually. It only shows up when the shapes actually collide
   at runtime, which `tests/test_stress.py::test_cross_persona_consistency`
   did, once, against real data.
   - **Fix:** two changes, at two layers, not one:
     - `mcp_server/queries.py::compare_companies` was joining
       `company_metrics` without joining `sources` at all, its rows had
       no `source_url`/`source_publisher` to report even in principle.
       Added the join.
     - `mcp_server/queries.py::search_sector_context` joined `sources`
       but only selected `src.url`, not `src.publisher`, inconsistent
       with the shape `get_company_signals` produces. Added the missing
       column.
     - `agent/core.py`'s single `for message in messages: ...
       payload.get("found")` block was replaced with `_extract_evidence()`,
       which recognizes all 3 response shapes explicitly (see its
       docstring) instead of assuming they match one.
       `tests/test_core_evidence.py` (11 cases, no network, runs in under
       a second) pins down each shape individually, including the
       not-found/unknown-slug/no-matching-sector paths and a case that
       specifically guards against `list_companies`'s
       `{"companies": [...]}` list shape being misread as
       `compare_companies`'s `{"companies": {...}}` dict shape.
3. **`confidence` secretly depended on LLM self-report, contradicting
   this project's own stated guardrail design** (`agent/schemas.py`'s
   docstring: "confidence and grounded... computed from the actual MCP
   tool-call trace, not asked of the model"). `companies_requested`/
   `companies_found`, which drive `compute_confidence`'s resolution-rate
   term, were derived from `LLMDraftAnswer.companies_referenced`, a field
   the model self-reports in the same structured turn as its prose
   `answer`. A live run produced exactly the failure this design was
   supposed to prevent: the model's `answer` correctly discussed
   Palantir and Adobe by name, using real retrieved data, but left the
   separate `companies_referenced` list empty, a generation slip in one
   field, unrelated to whether the prose was actually grounded. Because
   `compute_confidence` returns `(0.0, NONE)` immediately when
   `companies_requested == 0`, this one empty field collapsed confidence
   to zero while `grounded` (computed correctly, purely from the tool
   trace) stayed `True`. Two guardrail fields contradicted each other in
   the same response, worse than either field being wrong alone, since a
   reviewer can no longer trust either one on its own.
   - **Fix:** `_extract_evidence` (already parsing every tool result for
     #2) now also returns `resolved_companies`, the set of company slugs
     actually present in real tool *results*, computed the identical way
     `source_map`/`supporting_rows` already are. `companies_requested`/
     `companies_found` in `run_agent` now come from `resolved_companies`,
     never from `draft.companies_referenced`. 4 new tests in
     `tests/test_core_evidence.py` cover this directly, including one
     that reconstructs the exact failing transcript and asserts
     `resolved_companies` still recovers both companies with no
     dependency on any self-reported field at all.
   - **Follow-up correction (same review pass, before any live
     re-run):** the first version of this fix left the *displayed*
     `AgentResponse.companies_referenced` field, the one `ui/app.py`
     actually renders to a user, via both a count (`st.metric`) and a
     caption listing the slugs, as `resolved_companies |
     set(draft.companies_referenced)`, described in an earlier draft of
     this note as folding in the model's own list "only as a display
     safety net." On reflection that framing was wrong: a union still
     lets the self-reported field add a company to what the user sees on
     its own say-so, precisely the self-report dependency this fix
     exists to remove. It had just moved from the confidence math into
     the display layer instead of being eliminated. Caught by
     proactively grepping the codebase for every remaining use of
     `companies_referenced` rather than waiting for a live run to
     surface it, and corrected before any further live testing:
     `AgentResponse.companies_referenced` is now
     `sorted(resolved_companies)` with no union and no fallback.
     `LLMDraftAnswer.companies_referenced` (the model's own field) is
     kept in the schema, asking the model to name what it used is a
     cheap self-check nudge that plausibly makes it more careful, but
     `run_agent` no longer reads it for anything shown to the user or
     fed into guardrail math. `agent/schemas.py`'s docstring and this
     field's own docstring were updated to say so explicitly, so the
     next person reading the schema isn't misled into thinking it's
     still load-bearing anywhere.

After all three fixes, `pytest tests/ -v -m "not live"`, 26 passed (13
original + 13 new in `test_core_evidence.py`), all offline.

- **Confirmed on the second live re-run:** `pytest tests/test_stress.py -v
  -m live` against `gpt-4o-mini`, 3 of 4 passed; the 4th
  (`test_out_of_scope_company_is_declined_honestly`) failed on a real but
  different kind of issue: the agent itself behaved correctly, it
  answered "I cannot find any information on Nebulon Freight Systems ...
  unable to assess it as an investment opportunity," a clean, honest
  decline exactly matching the grounding-notice guardrail's intent, but
  the test's assertion checked the answer against a fixed list of phrases
  (`"don't have"`, `"no data"`, `"unable to find"`, etc.) that didn't
  happen to include the model's actual wording that run. This is test
  brittleness against a non-deterministic model, not an agent defect, and
  it's worth stating plainly rather than quietly widening the list and
  moving on: an LLM's exact phrasing for "I don't have this" varies call
  to call, so a test that pattern-matches on wording rather than on the
  structured `confidence_level`/`grounded` fields is inherently a little
  fragile no matter how many phrases are added. `confidence_level` was
  already correctly `LOW`/`NONE` in the failing run, the fields the
  guardrail actually promises were right; only the prose-matching
  assertion was narrow.
  - **Fix:** broadened the phrase list (added "cannot find", "can't
    find", "unable to assess", "no information", "not available in",
    "don't/do not track") with a comment explaining why the list is
    deliberately broad.
  - **Confirmed at that point:** `pytest tests/test_stress.py -v -m
    live`, 4 passed. `pytest tests/ -v -m "not live"`, 24 passed. 28 of
    28 tests passing, live and offline.
- **Then, populating this README's example-output section with a real
  cross-persona run surfaced bug #3 above** (the
  `confidence`/`companies_referenced` self-report contradiction), a
  defect that none of the 4 named stress tests happened to exercise,
  since none of them asserts on `companies_referenced` being non-empty
  when `grounded` is `True`. That fix, plus a proactive code review that
  caught the same self-report dependency still sitting in the
  *displayed* `companies_referenced` field (the follow-up correction
  described above, made before any further live testing), means the
  fixes were complete, but hadn't yet been re-run live at that point in
  the process.
- **Final confirmation, run by hand on the real machine (2026-09-04):**
  `pytest tests/ -v -m "not live"`, 26 passed, offline. `pytest
  tests/test_stress.py -v -m live` against `gpt-4o-mini`, 4 passed,
  including `test_out_of_scope_company_is_declined_honestly` (the
  regex-based redesign) and `test_cross_persona_consistency`. **30 of 30
  tests passing, live and offline, on the corrected code.** This is not a
  projected or "should work now" result. It's the actual output of a run
  against a real `OPENAI_API_KEY`, pasted back verbatim. Every bug listed
  in this section (#1-#3) and its follow-up correction are closed as of
  this run.

**A fourth real bug, found through manual UI testing after that "done"
point.** Once the app was confirmed working, it was actually used by hand
through the Streamlit UI rather than only through the pytest suite, and
that surfaced something none of the 4 PDF-named stress tests exercise:
asking to compare GXO (logistics) to Microsoft (tech) while the
conversation was scoped to "logistics" produced a confident,
88%-high-confidence, fully-sourced answer using real Microsoft data, with
zero mention that Microsoft was outside the declared sector, run three
times, once per persona, with the identical result every time. That
consistency across all 3 personas was the tell: the persona-differentiation
gap in #4 below varies by persona (a soft-instruction-compliance problem),
but this didn't vary at all, which points at a missing code-level check
rather than model unreliability. And it was: `get_company_signals` and
`compare_companies` never took or checked a `sector` argument at all. "You
are scoped to the 'logistics' sector" was one sentence in the system
prompt and nothing else. Nothing in the PDF requires sector to be a hard
boundary (it only requires the sector *parameter* to be independently
switchable, the "9 valid combos" line), so this isn't a spec violation,
but it's a real, demonstrable gap between what the system prompt claims
and what the tool layer actually enforces, and worth closing on its own
merits.

- **Fix:** `agent/core.py`'s `run_agent` now resolves the current
  sector's real company slugs once per request
  (`mcp_client.list_companies(sector)`) before the tool loop starts, and
  `_dispatch` blocks any `get_company_signals`/`compare_companies` call
  for a slug outside that set *before it ever reaches the database*. It
  doesn't run the query and then discard the result, it never issues it.
  `list_companies` and `search_sector_context` both take their own
  `sector` argument controlled by the model; that argument is now always
  overridden to the conversation's real declared sector, never trusted,
  closing the same gap for those two tools with a two-line change.
  Blocked lookups are reported through a new
  `guardrails.build_sector_scope_notice`, deliberately worded differently
  from `build_grounding_notice`: these companies genuinely exist and have
  real data, so the honest claim is "out of scope for this sector-scoped
  conversation," not "no data on this company." Conflating the two would
  itself be a small honesty regression in the other direction.
  `companies_requested` (feeding `compute_confidence`) now also counts
  blocked out-of-sector slugs, so a comparison that includes one still
  has its confidence correctly capped, the same way a genuinely
  unresolved company already caps it.
- **Verification:** 8 new offline unit tests in
  `tests/test_sector_scope.py` exercise `_dispatch` directly against the
  real local DB (no LLM call needed, the block is pure code, so it's
  tested as pure code): blocking an out-of-sector `get_company_signals`
  slug, allowing an in-sector one, splitting `compare_companies`'s mixed
  in/out-of-sector slugs correctly (and confirming an out-of-sector slug
  is never miscategorized as "unknown," a different, less accurate
  claim), the all-out-of-sector case, both tools' `sector` argument
  actually being overridden, and `build_sector_scope_notice`'s wording. A
  live regression test, `test_live_cross_sector_comparison_is_now_blocked`,
  re-runs the exact GXO/Microsoft query that found this bug and asserts
  Microsoft's real figures (`45.6%`, `128.5`, `281.7`) do not appear in
  the answer and `microsoft` does not appear in `companies_referenced`,
  deliberately not asserting the model can never say the word "Microsoft"
  at all, since naming it while explaining it's out of scope is the
  correct behavior, not a failure.

## 8. The UI's sector dropdown calls MCP directly, not through `run_agent`

- **Decision:** `ui/app.py::_load_sectors` calls
  `MCPToolClient().list_sectors()` directly rather than routing "what
  sectors exist" through `agent.core.run_agent`. `list_sectors` is also
  deliberately **not** one of the 4 tools offered to the LLM in
  `agent/tool_specs.py`, the sector is already a required, validated
  parameter to `run_agent`, so the model never needs to ask "what sectors
  exist" mid-conversation.
- **Why this doesn't violate the "one agent core" rule in #1:** #1 is
  about not duplicating *analysis logic*, prompt construction, the
  tool-calling loop, persona reasoning. Listing sectors for a dropdown
  isn't an analytical query, it's UI metadata. Routing it through the
  full agent loop (system prompt, LLM call, structured output) to answer
  "what sectors exist" would be paying LLM latency and cost for a lookup
  the database can answer directly. It still goes through the MCP
  boundary from #3, never `mcp_server.queries` directly, which is the
  property that actually matters here.

## 9. Prompt-injection resistance is architectural, not a keyword blocklist

- **Decision:** `agent/guardrails.py::SYSTEM_SAFETY_RULES` instructs the
  model to treat the user's query and all tool results as data, not
  instructions (`wrap_untrusted` delimits both in the transcript). But
  the real defense is what the model is *able to do* even if an
  injection fully succeeds: its only tools are the 4 read-only,
  narrowly-typed MCP functions in `agent/tool_specs.py`, scoped to this
  project's own sector/company/metric/signal tables. It cannot write,
  execute arbitrary code, browse the web, or call anything outside that
  surface, so a successful injection's worst realistic outcome is a
  wrong-but-harmless answer or an unnecessary tool call with
  attacker-chosen arguments, not data loss or exfiltration.
- **Why not a blocklist for phrases like "ignore previous instructions":**
  those are trivially bypassed by rephrasing and would give false
  confidence that injection is "handled." The instructional wrapping is
  defense-in-depth on top of the architectural constraint, not a
  substitute for it, stated directly rather than implied, since a
  reviewer testing this seriously will try to bypass a blocklist first.

## 10. Persona reassertion right before the final answer is drafted

- **What happened:** a live run after #4's sector-scope fix found the
  persona-differentiation gap in #4 above was worse than that entry
  described. #4 diagnosed the cause as an optional follow-up tool call
  some personas skip, but a further run found all 3 personas making the
  identical, sufficient tool calls (a full `get_company_signals` record,
  not just margin) and still producing functionally the same answer: two
  margin figures and a "X is stronger" conclusion, with none of the
  personas' actual foreground criteria showing up. That rules out #4's
  tool-forcing idea for this specific failure, since there was no missing
  tool call to force.
- **Root cause:** persona instructions are stated once, at the top of the
  system prompt, then buried under one or more tool-call round trips. The
  one message that IS freshly re-injected right before drafting ("give
  your final answer now") said nothing about persona at all, unlike the
  grounding and sector-scope notices, which are re-injected at that exact
  point for exactly this reason (instructions lose weight over a long
  transcript).
- **Fix:** `agent/core.py::_build_pre_draft_notices`, which now also
  re-injects `render_persona_instructions(persona)` as a fresh system
  message immediately before Phase B drafts, in addition to its place in
  the initial system prompt.
- **Verification:** pinned down offline in `tests/test_persona_reminder.py`
  since it's a pure function, no LLM call needed to test that the reminder
  is built correctly; the model's actual compliance still needed a live
  check.

## 11. Few-shot worked examples per persona

- **What happened:** even with #10's fix, a live run of the same
  GXO/Microsoft query still produced an Equity Analyst answer that only
  stated the two margin numbers and picked a winner. The persona's
  foreground criteria were still purely abstract instructions ("foreground
  earnings quality"), the same shape of instruction #10's own finding
  showed wasn't concrete enough on its own to stop that exact shortcut.
- **Fix:** added a short, fully fictional worked example per persona to
  `agent/personas.py::PERSONA_DEFINITIONS` (`example_answer`), using
  invented company names ("Company A"/"Company B") so it can never be
  mistaken for real grounded data, showing the actual shape of a
  compliant, persona-differentiated answer. Rendered as part of
  `render_persona_instructions`, so it reaches the model at both the
  points #10 already fixed, no other code change needed.
- **A further, narrower gap found live:** the Equity Analyst's only
  worked example was a two-company comparison, and a live query (after
  #4's block) left the model with just one company's data and nothing to
  compare against. The record still had a usable competitive-position
  signal in its own notes field ("thin margin typical of contract
  logistics"), but nothing told the model to look there when there was no
  second company.
- **Fix for that gap:** added a second worked example for the
  single-company case, and reworded the Equity Analyst's
  competitive-position criterion to say explicitly: when no peer company
  is available, use sector-benchmark language already present in the
  record's own notes or signal descriptions instead of skipping that
  criterion.

## 12. Tool-forcing safeguard for a skipped required lookup

- **What happened:** a live re-run of the Equity Analyst fix in #11
  surfaced a new, more severe failure: the model called
  `list_companies(sector=logistics)` twice and then stopped, never once
  calling `get_company_signals` for the company the question was
  actually about. The answer had no real figures in it at all
  (`grounded=False`, confidence 0%). This is the specific failure shape
  #10's own writeup flagged tool-forcing as not yet built for: a required
  lookup skipped entirely, not an optional follow-up skipped after a
  required one already succeeded.
- **Fix:** a deterministic check in `agent/core.py::_run_tool_loop`: if
  the model tries to stop having never called `get_company_signals` or
  `compare_companies` at all, it gets exactly one system-message nudge
  pointing this out and one more turn, rather than being allowed to draft
  with no data. The nudge still explicitly permits stopping if the
  question genuinely has no company to look up, so it doesn't force a
  pointless lookup on a purely sector-level qualitative question.

## 13. Proactive detection of a named out-of-sector company

- **What happened:** a live run asked to compare GXO to Microsoft while
  scoped to logistics. The model never attempted the (correctly blocked)
  lookup for Microsoft at all, so #4's reactive
  `build_sector_scope_notice` never fired, since it only fires once a
  blocked attempt has actually happened. Instead the model silently
  substituted UPS as the comparison target and answered a different
  question than the one asked, with no disclosure that Microsoft was
  excluded or that a substitution had happened.
- **Checked against the spec:** reviewed against the assignment PDF: its
  "out-of-scope test" only requires clearly saying there is no data
  rather than fabricating an answer; it does not sanction silently
  answering a different question in place of the one asked.
- **Fix:** a proactive, deterministic counterpart to the reactive block:
  `agent/core.py::_find_named_out_of_scope_companies` scans the user's
  own query text against every other sector's real company directory
  (fetched fresh through MCPToolClient each request, never hardcoded) for
  a name or ticker match. Any match is folded into the same
  `out_of_scope_sector` list the reactive path already uses, so the
  disclosure notice fires either way. Matching is deliberately biased
  toward precision over recall: a company whose short name collides with
  an ordinary word (Target Corporation's short name is just "Target") is
  matched only by ticker or full legal name, never the bare short name,
  to avoid a spurious disclosure firing on an unrelated sentence like
  "what's GXO's target margin." `build_sector_scope_notice` was also
  strengthened to explicitly forbid silent substitution and require the
  model still answer fully for whichever in-scope company it does have
  data for.
- **Verification:** 7 offline tests in `tests/test_named_out_of_scope.py`
  cover the detection logic directly against the real local DB, including
  that ambiguous-word edge case.

## 14. Tool-forcing must trigger on budget exhaustion, not only on voluntary stop

- **What happened:** found by running a 3-sector by 3-persona grid script
  directly against `run_agent` (not through the earlier single-query live
  tests, which had only ever exercised one sector at a time). The Mutual
  Fund Analyst, asked the exact "which companies look like attractive
  buyout targets" query from #12's own bug report, came back with
  `grounded=False`, confidence 0%, and an empty `companies_referenced`,
  the same failure shape #12 was supposed to have closed.
- **Root cause:** the tool trace showed the model called `list_companies`
  three times and `list_sectors` once, and never once called
  `get_company_signals` or `compare_companies`. #12's fix only checks for
  the nudge on a turn where the model produces zero tool calls, on the
  theory that a model either looks something up or gives up. This model
  did neither: it kept making real, allowed tool calls (just the wrong,
  redundant ones) turn after turn until `MAX_TOOL_ITERATIONS` ran out, so
  the "if not message.tool_calls" branch never had a turn to fire on, and
  the loop fell out the bottom with no data gathered at all. This is a
  distinct and more severe case than #12's original finding: not a model
  that quits, but a model that never quits and never does the one thing
  that mattered either, until the clock runs out for it.
- **Fix:** no longer waiting for a voluntary stop. `_run_tool_loop` now
  also checks, after every turn regardless of whether that turn made
  tool calls, whether only one iteration remains and the required lookup
  still has not been attempted. If so, the nudge fires right then,
  guaranteeing the model's last remaining turn is spent under an
  explicit instruction to call the real lookup tool, rather than
  silently drafting once the loop exhausts its budget. This keeps the
  same one-nudge-per-run contract #12 established, it just adds a second
  condition that triggers it, alongside the original "gave up" condition
  rather than replacing it.
- **Verification:** two new offline tests in
  `tests/test_tool_forcing_budget.py` build a fake OpenAI client with no
  live API call at all, since this is a pure control-flow bug, not a
  prompt-wording one. One simulates a model that calls `list_companies`
  on every single turn for the full `MAX_TOOL_ITERATIONS` budget and
  asserts the nudge still gets injected into the transcript exactly once
  before the budget runs out. The other re-confirms #12's original "gives
  up immediately" case still nudges correctly, so the fix is additive,
  not a replacement that could regress the case it was already handling.
  `pytest tests/ -v -m "not live"` after this fix: 52 passed (the 50 from
  before plus these 2), offline.

## 15. A malformed tool call must degrade gracefully, not crash the whole request

- **What happened:** found by a deliberate audit pass, not a live
  failure: reading `_run_tool_loop` line by line while writing coverage
  for #14 turned up `args = json.loads(tool_call.function.arguments or
  "{}")` with no exception handling around it, and `_dispatch`'s
  `args["company_slug"]` with no `.get()` fallback either. A model that
  returns invalid JSON in a tool call's arguments, or a syntactically
  valid call missing a required key, would raise `json.JSONDecodeError`
  or `KeyError` straight out of the tool loop, failing the entire request
  even if several other tool calls that same turn, or in earlier turns,
  had already succeeded and gathered real data. `api/main.py` and
  `ui/app.py` both catch broadly at their own layer, so this wouldn't
  crash the server or the app, but it would throw away an
  otherwise-recoverable turn and burn the tokens already spent on it, for
  a single bad tool call among possibly many good ones.
- **Fix:** the same way the existing "unknown tool name" case already
  works: wrapped the per-tool-call argument parsing and dispatch in a
  `try` that catches `json.JSONDecodeError`, `KeyError`, and `TypeError`,
  and reports back a normal tool-error message (`{"error": "invalid
  arguments for tool '...': ..."}`) instead of propagating. The model
  sees the error like any other tool result and can retry with corrected
  arguments; the rest of the request is unaffected.
- **A real ordering bug found in this fix itself, caught while writing
  its own test, before ever shipping it.** The first version set
  `attempted_company_lookup = True` as the first line inside the `try`
  block, after `json.loads`. That meant a malformed-JSON call (the
  exception firing inside `json.loads` itself) never reached that line
  at all, so it did NOT count as an attempt, exactly the wrong behavior:
  a model that tried `get_company_signals` and got the arguments wrong
  should not also be told by the #8/#14 tool-forcing nudge that it
  "never tried at all" as if it had made no effort. Writing
  `test_malformed_lookup_attempt_still_counts_as_attempted_no_false_nudge`
  in `tests/test_edge_cases.py` surfaced this immediately: the test's
  scripted fake client ran out of canned turns because the (wrongly)
  un-set flag caused an extra, unplanned nudge cycle.
  - **Fix for that:** moved the `attempted_company_lookup = True`
    assignment out of the `try` block entirely, to before argument
    parsing even begins, so it is set regardless of whether parsing or
    dispatch later fails.
- **Verification:** `tests/test_edge_cases.py`, 10 offline tests, no
  OPENAI_API_KEY needed. Four of them exercise this fix directly:
  malformed JSON arguments don't crash the loop, a missing required key
  doesn't crash the loop, a malformed-but-real attempt does not also
  trigger the "never tried" nudge, and (a related case pinned down at the
  same time since it shares the same risk) a correctly *blocked*
  out-of-sector attempt also does not trigger that nudge on top of the
  sector-scope block. The other six cover defensive ground that was
  never observed to fail but was worth confirming directly rather than
  assuming: `compare_companies` with an empty slug list, with duplicate
  slugs (dict-keyed, so they collapse to one entry rather than erroring
  or duplicating), and with a metric name outside the closed vocabulary
  (returns zero rows honestly, not a SQL error); sector-slug case
  sensitivity ("Logistics" is not "logistics", confirmed explicitly at
  the pydantic layer so nobody "fixes" this by loosening the wrong
  check); and the query-length boundary at exactly 2000 vs 2001
  characters. `pytest tests/ -v -m "not live"` after both fixes: 62
  passed, offline.

## 16. A nudge is a request, not a constraint - forcing tool_choice is what actually works

- **What happened:** a live re-run of the exact Mutual Fund Analyst /
  "buyout targets" query that #14 was found and fixed on showed #14's own
  fix firing exactly as designed: with one iteration left and no lookup
  yet attempted, the proactive nudge message was injected into the
  transcript, right on schedule. And the model still produced a turn with
  zero tool calls anyway, plainly ignoring the instruction ("you must
  call one of those tools now"), and drafted a full answer from no data
  at all (`grounded=False`, confidence 0%, `companies_referenced=[]`),
  the identical failure #14 was supposed to have closed.
- **What this demonstrates:** the clearest possible demonstration of a
  distinction worth stating plainly: a system message telling a model
  what it must do is a request, not an enforced constraint. The model can
  read it and simply not comply. #14's fix (guaranteeing the nudge fires
  before the budget runs out) was necessary but not sufficient, since it
  never addressed whether the model actually listens once nudged.
- **Fix:** making the API call immediately following any nudge, either
  the original bug #8 "gave up" case or bug #14's "burned the budget"
  case, pass `tool_choice="required"` instead of `tool_choice="auto"` for
  that one turn only, then reset back to `"auto"` immediately after. This
  is a real constraint enforced by the OpenAI API itself: on a
  `"required"` turn the model must call *some* tool, not specifically the
  correct one, but combined with the nudge text naming the exact tool to
  call, this is the strongest guarantee available short of picking the
  tool call ourselves and skipping the model's judgment entirely (which
  would remove its ability to correctly decide the question needs no
  lookup at all, the same escape hatch the nudge text has always
  explicitly preserved).
- **Verification:** two new offline tests in
  `tests/test_tool_choice_forcing.py`, no live API key needed, since
  forcing an API parameter is itself a pure code-level property to test,
  not a question of model behavior. One builds a fake client that
  reproduces the exact live failure (always calls a non-essential tool
  under `"auto"`) and asserts that once `tool_choice` actually is
  `"required"` on some turn, `get_company_signals` gets called, proving
  the constraint changes the outcome, not just that a nudge message
  exists somewhere in the transcript. The other guards against
  overcorrecting: a model that never needed nudging must never see
  anything but `"auto"`, confirming this doesn't quietly turn into a
  blanket policy. `pytest tests/ -v -m "not live"` after this fix: 64
  passed, offline.
- **The general lesson drawn at the time (revisited and corrected in
  #17):** this bug, more than any other in this file, is worth
  internalizing as a general lesson about building on top of an LLM: an
  instruction in a prompt, however clearly worded, is a hint the model
  can decline. Where compliance actually matters, the fix has to live at
  the API-constraint level (`tool_choice`, a JSON schema, a hard block in
  `_dispatch`), not in the wording of what you ask for. Every guardrail
  in this project that has proven reliable under live testing (#4's
  sector block, #9's guardrail formula, this one) is a constraint the
  code enforces regardless of what the model wants to do; every one that
  turned out fragile (#5's original persona reminder, #12's original
  nudge-only fix) was a request phrased as an instruction and hoped for.

## 17. `tool_choice="required"` constrains that a tool is called, not which one

- **What happened:** a live re-run after #16 shipped, with all 64
  offline tests passing locally including the new
  `test_tool_choice_forcing.py` tests written specifically for this
  mechanism, showed the exact same Mutual Fund Analyst / "buyout targets"
  failure a third time, identical down to the tool-call trace:
  `grounded=False`, confidence 0%, `companies_referenced=[]`. The fix
  that was supposed to close this had not changed the outcome at all.
- **Root cause:** the gap was in what `tool_choice="required"` actually
  promises. It constrains the model to call *some* function that turn. It
  says nothing about *which* function. This persona's model consistently
  preferred `list_companies` across every turn in the failing trace, and
  `list_companies` is a completely valid way to satisfy `"required"`, the
  model was never being disobedient on this turn the way #16 assumed; it
  was fully compliant with the actual constraint it had been given, which
  just wasn't the constraint that mattered. Because #14's nudge fires
  with exactly one iteration left, the forced turn is also the *last*
  turn of the loop: if the model satisfies `"required"` with another
  `list_companies` call, the loop ends immediately after and Phase B
  drafts from zero real data, exactly as if no fix had shipped at all.
- **Why #16's own test missed this:** the regression test written for
  #16 hardcodes the assumption being tested for: `if tool_choice ==
  "required": call get_company_signals`. That is a description of what
  the fix was hoping would happen, not of what the API contract actually
  guarantees. A test built around the fix's own assumption cannot catch
  a gap in that assumption.
- **Fix:** narrowing the `tools` array itself on the one forced turn, not
  just `tool_choice`. `agent/core.py` now defines `LOOKUP_TOOLS`, the
  subset of `OPENAI_TOOLS` containing only `get_company_signals` and
  `compare_companies`, the two tools that actually retrieve
  company-level data, as opposed to `list_companies`/`search_sector_context`
  which never do. On the forced turn, the API call passes
  `tools=LOOKUP_TOOLS` instead of the full list, combined with
  `tool_choice="required"`. Now "call some tool" and "the only tools on
  offer are the two real lookups" together add up to an actual
  guarantee, not tool_choice alone doing work it was never able to do by
  itself.
- **Verification:** a new offline test,
  `tests/test_tool_choice_narrowing.py`, built specifically to not
  repeat #16's test's mistake. Its fake client always prefers
  `list_companies` regardless of what `tool_choice` says, reproducing
  the live failure honestly instead of assuming compliance, the only way
  to make it call `get_company_signals` is to take `list_companies` off
  the menu entirely. One test proves the narrowed turn actually happens
  and that it produces the real lookup call; a second guards against
  overcorrection, confirming every ordinary turn still sees the full
  four-tool list and only the one forced turn is narrowed. `pytest
  tests/ -v -m "not live"` after this fix: 66 passed, offline (up from
  64).
- **Correcting #16's lesson, not just restating it:** an API parameter is
  not automatically a real constraint just because it isn't a prompt
  string. `tool_choice="required"` felt like exactly the kind of enforced
  guardrail #16's closing paragraph praised, mechanism over wording, and
  it still wasn't enough, because the constraint it enforces was
  narrower than the problem it was applied to. The actual discipline is
  to read what a constraint literally guarantees, not what it was
  clearly intended to guarantee, and to check that guarantee against the
  exact failure being fixed rather than against a test written from the
  same assumption as the fix. #4's sector block and #9's guardrail
  formula hold up under that scrutiny because they check the one
  specific fact that matters (is this slug in this sector's set) with no
  room for a technically-compliant wrong answer; this fix now holds up
  the same way, because the narrowed tool list leaves the model no
  technically-compliant way to avoid the real lookup.
- **Live-confirmed 2026-09-06:** a fourth re-run of the exact Mutual Fund
  Analyst / logistics query, the same one bugs #14/#16/#17 were all found
  and fixed on, finally returned `grounded=True`, confidence 0.96, and
  all 5 real logistics companies (fedex, gxo, old-dominion, ups, xpo)
  referenced with a `get_company_signals` call for each. The same pass
  also re-ran the full offline suite (66 passed),
  `tests/test_prompt_injection.py` and `tests/test_stress.py` live (7
  passed, covering both PDF example queries, cross-persona consistency,
  and the out-of-scope-company honest decline), and two targeted manual
  checks: a named out-of-scope company mid-query (GXO vs Microsoft while
  scoped to logistics), which correctly disclosed the exclusion instead
  of silently substituting, per #9/#13; and a query at the length
  boundary, which completed normally. Nothing regressed across any of the
  eleven previously-fixed bugs in this file.

## What I'd improve with more time

Documented here rather than only in the README, since it's a decision in
its own right, what was deliberately left out of a 3-day scope:

- Run the full live-model stress-test suite (#7) and hand-read the three
  cross-persona transcripts, not just assert they're textually different.
  **Done**, see #4's update and #10-#13: the transcripts were read by
  hand repeatedly, real gaps were found each time, and each one was
  fixed and re-checked live rather than assumed fixed from the code
  alone.
- `AgentResponse.sources` and `companies_referenced` currently include
  every company `search_sector_context` happened to touch during the
  tool loop, not only the ones actually discussed in the final prose.
  This showed up in a live run where a sector-wide qualitative lookup
  pulled in two companies' sources that were never mentioned in the
  answer. Not wrong exactly, those were genuinely retrieved, but it
  overstates what the answer is actually sourced on. Would need
  `_extract_evidence` to cross-reference the final answer text or
  `draft.companies_referenced`, which reopens the self-report question
  #5 exists to avoid, worth solving carefully, not a quick fix.
- `LLMDraftAnswer.limitations` is still self-reported and inconsistent,
  the model sometimes discloses a data gap in prose but leaves this
  structured field empty, or vice versa. Unlike
  `confidence`/`grounded`/`companies_referenced`, this field was never
  moved off self-report. Doing so would mean either a stricter schema
  requirement or computing known gaps (like the headcount gaps in #6) in
  code instead of trusting the model to notice and say so every time.
- Corroborating sources for the same fact (#2), right now one fact has
  exactly one source, by schema design; a second corroborating source
  would need either a second metric row or a real junction table.
- Fill the headcount and full-fiscal-year gaps noted in #6 for the
  companies missing them, rather than leaving those cells empty.
- Move the Streamlit UI to call the FastAPI endpoint over HTTP instead of
  importing `agent.core` in-process (#1), so the two front doors have
  zero shared Python runtime, only a shared HTTP contract.
- Replace the simple weighted `compute_confidence` formula (#5) with
  something that's actually been checked against labeled examples of
  "should have been high/low confidence," rather than a formula that's
  only unit-tested for monotonicity.
