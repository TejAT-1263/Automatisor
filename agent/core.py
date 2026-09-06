"""
The single agent entrypoint. Both api/main.py and ui/app.py call
`run_agent()` and nothing else — see DECISIONS.md #1 for why that's the
one rule this whole project is organized around.

Two-phase loop, not a single freeform tool-calling call:
  Phase A: a bounded tool-calling loop (max MAX_TOOL_ITERATIONS turns) in
    which the model may call any of the 4 MCP-backed tools, executed via
    agent/mcp_client.py.
  Phase B: exactly one final call with tool calls disabled and a JSON
    schema response_format, so the model cannot end the conversation with
    a free-text answer - it must emit an LLMDraftAnswer.

`confidence`, `confidence_level`, `grounded`, and `tool_calls` in the
returned AgentResponse are computed by agent/guardrails.py from the actual
tool-call trace, never taken from the model's own output.

VERIFIED against a live OPENAI_API_KEY on 2026-09-04 (see DECISIONS.md
#7 for the full account): live runs surfaced five real bugs, all fixed
here and pinned down by tests/test_stress.py + tests/test_prompt_injection.py
(live) and tests/test_core_evidence.py + tests/test_guardrails.py (offline):
  1. Phase B was passing tool_choice="none" without a `tools` array,
     which the API rejects outright (`'tool_choice' is only allowed when
     'tools' are specified`). Fixed by dropping the parameter — Phase B
     never passed `tools` to begin with, so the model already couldn't
     call one; the parameter was redundant, not protective.
  2. The evidence-extraction step below only recognized
     get_company_signals's response shape. compare_companies and
     search_sector_context return structurally different dicts with no
     top-level "found" key, so answers built from those two tools were
     silently scored grounded=False with zero sources even when backed by
     real DB rows. Fixed by _extract_evidence, which handles all 3 shapes
     explicitly instead of assuming they match.
  3. companies_requested/companies_found (which drive `confidence`) were
     derived from LLMDraftAnswer.companies_referenced — a field the model
     self-reports in the same structured turn as its prose answer. When
     the model's answer correctly discussed real companies but left that
     separate field empty, confidence silently collapsed to 0.0/NONE
     while `grounded` (computed purely from the trace) stayed True — a
     direct contradiction between two fields, and confidence quietly
     depending on LLM self-report despite agent/schemas.py's own
     docstring promising otherwise. Fixed by having _extract_evidence
     also return resolved_companies, computed from the tool trace the
     same way source_map/supporting_rows already are — confidence can no
     longer collapse on a model formatting slip in an unrelated field.
  4. Manual UI testing (all 3 personas, same "compare GXO to Microsoft"
     query, sector scoped to "logistics") found the model would freely
     retrieve and confidently compare Microsoft — a tech-sector company —
     with 88% confidence and no mention that it was out of scope, in all
     3 runs. The cause: get_company_signals and compare_companies never
     took or checked a sector argument at all; "you are scoped to the
     'logistics' sector" was a sentence in the system prompt and nothing
     else, so it was pure soft-instruction hope, not enforcement — and
     unlike the persona-differentiation gap (DECISIONS.md #4), where
     compliance genuinely varies by persona, this failed identically
     across all 3, which is the signature of a missing code-level check
     rather than model unreliability. Fixed by resolving the current
     sector's real company slugs once per request and having _dispatch
     block any get_company_signals/compare_companies call for a slug
     outside that set before it ever reaches the database (list_companies
     and search_sector_context's own `sector` argument is now also always
     overridden to the declared sector, ignoring whatever the model
     passed, closing the same gap for those two tools). Blocked lookups
     are reported via guardrails.build_sector_scope_notice, worded
     distinctly from build_grounding_notice ("out of scope for this
     sector" vs. "not in the database") since these companies do exist,
     just not here.
  5. Further manual UI testing (same "compare GXO to Microsoft" query,
     re-run post-fix-#4 as Equity/MF/PE Analyst on GXO alone) found the
     persona-differentiation gap in DECISIONS.md #4 was worse than that
     entry described. #4 diagnosed the cause as an optional follow-up
     tool call some personas skip - but here all 3 personas made the
     IDENTICAL, sufficient tool calls (get_company_signals returns the
     full record - revenue, margin, headcount, signals - not just
     margin) and still produced functionally the same answer: the two
     margin figures and a "Microsoft is stronger" conclusion, with zero
     engagement of any persona's actual foreground criteria. This ruled
     out #4's proposed tool-forcing fix for this failure mode - there was
     no missing tool call to force. The real mechanism: persona
     instructions are stated once, at the top of the system prompt, then
     buried under one or more tool-call round trips; the one message
     that WAS freshly re-injected right before drafting ("Give your
     final answer now...") said nothing about persona at all, unlike the
     grounding/sector-scope notices, which ARE re-injected at that exact
     point for exactly this reason (instructions lose weight over a long
     transcript). Fixed by _build_pre_draft_notices, which now also
     re-injects render_persona_instructions(persona) immediately before
     the final drafting call - see its own docstring for the full
     account. This is a genuinely different fix from #4's, aimed at a
     genuinely different failure mode the same underlying gap can
     produce; #4's tool-forcing idea may still be worth building
     separately for the case it actually fits (a persona skipping a
     needed lookup entirely), but it would not have fixed this one.
  6. NOT YET LIVE-CONFIRMED (added 2026-09-05, proactively, ahead of a
     live re-run of bug #5's fix, because persona differentiation is the
     one thing this whole exercise will be judged on): the re-injected
     persona reminder from fix #5 was still purely abstract criteria
     language ("foreground earnings quality") — the exact shape of
     instruction that fix #5's own root-cause finding showed wasn't
     concrete enough on its own to stop a two-number-and-a-winner answer.
     Added a short, fully fictional ("Company A"/"Company B" — never a
     real slug, so it can't be mistaken for real grounded data) worked
     example per persona in agent/personas.py's PERSONA_DEFINITIONS
     (`example_answer`), rendered as part of the same
     render_persona_instructions() block that already reaches the model
     at both the initial system prompt and the fix-#5 pre-draft
     reassertion — so this required no other code change. This is a
     second, independent lever stacked on top of fix #5, not a
     replacement for it: fix #5 ensures the persona instructions are
     freshly weighted right before drafting; this fix makes those
     instructions concrete enough to actually change the answer's shape.
     Unconfirmed by a live run as of this writing — see
     tests/test_persona_differentiation_live.py for the exact live check
     this is expected to move the needle on.
  7. Live-run results for fix #6 (2026-09-05, tests/test_persona_differentiation_live.py
     against the real "compare GXO's operating margin to Microsoft's"
     query, sector-scoped to logistics): Mutual Fund Analyst and PE
     Analyst both passed - their answers now genuinely engage
     portfolio-fit/core-holding and buyout/margin-headroom framing
     respectively, not just the two numbers. Equity Analyst failed. Root
     cause, confirmed against the seed data (data/seed/logistics.json):
     GXO's record has exactly one operating_margin_pct figure, with no
     GAAP-vs-adjusted split at all - so the model correctly declined to
     invent a GAAP/adjusted comparison it has no data for, which is
     correct grounding behavior, not a persona defect. The real gap: fix
     #6's example_answer only modeled the two-company comparison case:
     the live query, after bug #4's sector-scope block, left the model
     with only GXO's data and nothing to compare it against, and the
     Equity persona's instructions had no guidance for that shape of
     question. The record does carry a usable competitive-position signal
     even with one company - GXO's metric notes literally say "Thin
     margin typical of contract logistics" - but the model only gestured
     at this vaguely ("reflects the competitive landscape") instead of
     using it to make an explicit competitive-position judgment, because
     nothing told it to look there. Fixed by (a) rewording the Equity
     Analyst's competitive-position foreground criterion to explicitly
     say: when no peer company's data is available for direct comparison,
     use sector-benchmark language already present in the record's own
     notes/signal descriptions rather than skipping this criterion, and
     (b) adding a second example_answer specifically for the
     single-company case, matching the live failure's exact shape (one
     company, "typical for the sector" language already in the data,
     comparison unavailable). Mutual Fund and PE Analyst definitions were
     left untouched since they already passed. Not yet re-confirmed by a
     live run as of this writing.
  8. Live re-run of fix #6 for Equity Analyst (2026-09-05, same query)
     surfaced a NEW, more severe failure than #6 was fixing: the model
     called list_companies(sector=logistics) twice in a row and then
     stopped, never once calling get_company_signals for GXO - the
     company the question was actually about. The final answer contained
     no real figures at all (it didn't even state the 1.9% margin),
     grounded=False, confidence=0%. This is not a wording/emphasis
     problem like #6 was - it's the model skipping a required data
     lookup entirely, which is exactly the failure shape bug #5's
     docstring flagged as NOT what tool-forcing (as proposed for #4) was
     built to fix, but noted might be worth building separately "for the
     case it actually fits (a persona skipping a needed lookup
     entirely)" - this is that case. Fixed by a deterministic safeguard
     in _run_tool_loop (see its own docstring): if the model tries to
     stop having never called get_company_signals/compare_companies at
     all, it gets exactly one system-message nudge pointing this out and
     one more turn, rather than being allowed to draft blind - the nudge
     explicitly still permits stopping if the question has no specific
     company to look up, so this doesn't force a lookup on genuinely
     sector-level qualitative questions. Not yet re-confirmed by a live
     run as of this writing; unlike fixes #6/#7 this one is a code-level
     behavior change to the tool loop itself, not a prompt/instruction
     change, so it is tested here against the exact failure shape
     observed (zero company lookups attempted) rather than by wording.
  9. A later live run of fix #7's Equity Analyst query (2026-09-05,
     "compare GXO's operating margin to Microsoft's") surfaced a further,
     distinct failure: the model never attempted the (correctly blocked)
     get_company_signals(microsoft) call, and never disclosed Microsoft
     at all - it silently substituted UPS as the comparison target and
     answered THAT question instead, with a real, well-grounded,
     confident (96%) answer. build_sector_scope_notice (bug #4) only
     fires when out_of_scope_sector is non-empty, which only happens if
     the model actually attempts the blocked lookup and gets told no -
     here it never tried, so the reactive path never engaged and nothing
     told the user their actual question had been silently changed.
     Reviewed against the assignment PDF (Agent_JD.pdf): the PDF's own
     "out-of-scope test" only requires clearly saying there's no data
     rather than fabricating an answer - it does not address a company
     that exists but under a different sector, and does not sanction
     silently answering a different question in its place. Fixed with a
     proactive, deterministic counterpart to the reactive block:
     _find_named_out_of_scope_companies scans the user's own query text
     against every OTHER sector's real company directory (fetched fresh
     via MCPToolClient, not hardcoded) for a name/ticker match, and
     run_agent folds any match into out_of_scope_sector before the
     pre-draft notices are built - so build_sector_scope_notice fires
     regardless of what the model itself attempts. That notice was also
     strengthened to explicitly forbid silent substitution and to
     require the model still give a complete, real, persona-lens answer
     for whichever in-sector company it does have grounded data for,
     rather than letting the exclusion collapse into a refusal. Not yet
     confirmed by a live run as of this writing.
  10. Bug #16's fix (forcing tool_choice="required" on the turn right
      after a nudge) was confirmed present in the code (64/64 offline
      tests passing, including a dedicated regression test) but a live
      re-run of the exact same Mutual Fund Analyst / logistics query
      still failed identically - grounded=False, confidence 0%,
      companies_referenced=[], the same tool-call trace as every prior
      failing run. The gap: tool_choice="required" only forces the model
      to call *some* tool that turn - it does not forbid it from calling
      list_companies again, which is exactly the tool this persona kept
      preferring on every earlier turn. On MAX_TOOL_ITERATIONS=6, the
      nudge (per bug #14) fires with exactly one iteration left, so the
      forced turn is also the LAST turn; if the model satisfies
      "required" by calling list_companies one more time instead of
      get_company_signals, the loop ends right there with no further
      chance to correct course, and Phase B drafts from zero real data
      regardless of how insistent the nudge text was. The regression
      test added for bug #16 did not catch this because its fake client
      hardcodes "if tool_choice == required, call get_company_signals" -
      an assumption about what "required" does that the real API does
      not make true; "required" constrains that a tool is called, not
      which one. Fixed (bug #17): on the forced turn, the `tools` array
      itself is narrowed to just get_company_signals and
      compare_companies (LOOKUP_TOOLS) - list_companies and
      search_sector_context are not offered at all that turn, so
      "required" against that narrowed list is a real guarantee the
      right kind of call happens, not merely a call. Confirmed by a live
      re-run of the exact Mutual Fund Analyst / logistics query on
      2026-09-06: grounded=True, confidence 0.96, all 5 real logistics
      companies referenced, get_company_signals called for each one. The
      same live pass also re-confirmed bugs #8/#9/#14/#15/#16 did not
      regress: the full offline suite (66/66), tests/test_prompt_injection.py,
      tests/test_stress.py (7/7, includes both PDF example queries and the
      out-of-scope-company decline), and two fresh manual checks (a named
      out-of-scope company mid-query, which still gets disclosed rather
      than silently substituted; a query at the length boundary) all
      passed.
"""

from __future__ import annotations

import json
import os
import re

from openai import OpenAI

from agent.guardrails import (
    build_grounding_notice,
    build_sector_scope_notice,
    compute_confidence,
    SYSTEM_SAFETY_RULES,
    wrap_untrusted,
)
from agent.mcp_client import MCPToolClient
from agent.personas import render_persona_instructions
from agent.schemas import AgentResponse, ConfidenceLevel, LLMDraftAnswer, Persona, SourceRef
from agent.tool_specs import OPENAI_TOOLS, TOOL_NAMES

MAX_TOOL_ITERATIONS = int(os.environ.get("MAX_TOOL_ITERATIONS", "6"))

# Bug #17: the two tools that actually retrieve company-level data, as
# opposed to list_companies/search_sector_context which never do. Used to
# narrow the tool list (not just tool_choice) on the one forced turn right
# after a nudge - see _run_tool_loop's docstring bug #17 for why
# tool_choice="required" alone was not enough.
_LOOKUP_TOOL_NAMES = {"get_company_signals", "compare_companies"}
LOOKUP_TOOLS = [tool for tool in OPENAI_TOOLS if tool["function"]["name"] in _LOOKUP_TOOL_NAMES]


def _client() -> OpenAI:
    return OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
    )


def _build_system_prompt(persona: Persona, sector: str) -> str:
    return (
        f"{SYSTEM_SAFETY_RULES}\n\n"
        f"You are the analysis engine behind a facility/company intelligence tool. "
        f"You are scoped to the '{sector}' sector for this conversation — this is "
        f"enforced, not a suggestion: any get_company_signals or compare_companies call "
        f"for a company outside this sector will be blocked before it reaches the "
        f"database, and list_companies/search_sector_context will always run against "
        f"'{sector}' regardless of what sector argument you pass them. Do not attempt to "
        f"look up or compare companies from other sectors. "
        f"{render_persona_instructions(persona)}\n\n"
        f"Ground every factual claim in a tool call. Never state a number you did "
        f"not retrieve via list_companies, get_company_signals, compare_companies, "
        f"or search_sector_context. If you did not look something up, say you don't "
        f"know rather than estimating from general knowledge.\n\n"
        f"search_sector_context ONLY returns qualitative signals (hiring, layoffs, "
        f"automation investment, expansion) - it never returns financial metrics "
        f"(margin, revenue, valuation, headcount level, cash generation). If your "
        f"foreground criteria above reference any financial metric, a "
        f"search_sector_context call alone is not sufficient grounding for your "
        f"answer - follow it with get_company_signals or compare_companies for the "
        f"specific companies it surfaces, so you can foreground the metric your "
        f"persona actually cares about rather than only the qualitative narrative "
        f"every persona would see identically. Do not give your final answer after "
        f"only a sector-level qualitative lookup if your persona's foreground "
        f"criteria require metric-level evidence you have not yet retrieved."
    )


def _draft_answer_schema() -> dict:
    schema = LLMDraftAnswer.model_json_schema()
    schema["additionalProperties"] = False
    # Structured Outputs strict mode requires every property listed as required.
    schema["required"] = list(schema.get("properties", {}).keys())
    return {
        "type": "json_schema",
        "json_schema": {"name": "draft_answer", "schema": schema, "strict": True},
    }


async def _run_tool_loop(
    client: OpenAI,
    mcp_client: MCPToolClient,
    messages: list[dict],
    sector: str,
    sector_company_slugs: set[str],
) -> tuple[list[dict], list[str], list[str]]:
    """Runs the bounded tool-calling loop. Returns the updated message
    transcript, the list of company slugs that came back not-found across
    all tool calls made (used to build the grounding notice), and the
    list of company slugs that exist but were blocked as out-of-sector
    (used to build the sector-scope notice) - see build_sector_scope_notice's
    docstring and this module's docstring bug #4 for why that's tracked
    separately from "not found at all".

    Also implements the tool-forcing safeguard from this module's
    docstring bug #8: a live run found the Equity Analyst persona call
    list_companies(sector=logistics) TWICE and then stop, never once
    calling get_company_signals/compare_companies for GXO - the company
    the user's question was actually about - and draft an answer with no
    real figures in it (grounded=False, confidence 0%). This is exactly
    the failure mode bug #5's docstring flagged as NOT fixed by the
    persona-reassertion fix and separately not fixed by fix #4's original
    tool-forcing proposal (that one target a different, earlier finding
    where an OPTIONAL follow-up call was skipped after a REQUIRED one had
    already succeeded; this is a REQUIRED call being skipped entirely).
    If the model tries to stop having never once called
    get_company_signals or compare_companies, it gets exactly one nudge
    system message pointing this out and is given another turn - not a
    hard requirement, since a genuinely sector-level qualitative question
    may have no specific company to look up, so the nudge explicitly
    permits proceeding without one. Nudges at most once per run (tracked
    by `nudged`) so a model that still won't look anything up cannot loop
    forever within the MAX_TOOL_ITERATIONS budget.

    Bug #14 (found by running a 3-sector x 3-persona grid through the real
    agent, not through the earlier single-query live tests): the original
    #8 fix only checked for the nudge on a turn where the model produced
    NO tool calls at all - i.e. it only caught a model that gives up. A
    live run of the Mutual Fund Analyst on the exact "buyout targets"
    query from #8 never gave up; it just spent its entire tool budget
    re-calling list_companies/list_sectors (5 calls, several of them
    redundant re-listings of sectors it had already listed) and never
    once called get_company_signals, so the loop exhausted
    MAX_TOOL_ITERATIONS while `message.tool_calls` was still truthy on
    every turn - the "not message.tool_calls" nudge check never got a
    single chance to run, and the model drafted from zero real data
    anyway (grounded=False, confidence 0%, companies_referenced=[]).
    This is a more severe variant of #8: not "stops without looking
    anything up" but "never stops, but never looks up the right thing
    either, until the budget itself runs out". Fixed by also checking,
    after every turn regardless of whether that turn made tool calls,
    whether only one iteration remains and the required lookup still
    hasn't been attempted - if so, the nudge fires right then,
    guaranteeing the model's last remaining turn is spent under an
    explicit instruction to call the real lookup tool, rather than
    silently drafting once the loop falls out of the `for` naturally."""
    unresolved: list[str] = []
    out_of_scope_sector: list[str] = []
    attempted_company_lookup = False
    nudged = False
    # Bug #16: a live run (Mutual Fund Analyst, the exact query bug #14 was
    # found on) showed the #14 fix firing correctly - the nudge message DID
    # get injected with one iteration left - and the model STILL produced a
    # turn with zero tool calls anyway, ignoring the explicit instruction,
    # and drafted from no data regardless. A system message is a request,
    # not a constraint; a model can read "you must call one of those tools
    # now" and simply not comply. Fixed by making the very next API call
    # after ANY nudge (both the bug #8 "gave up" case and the bug #14
    # "burned the budget" case) pass tool_choice="required" instead of
    # "auto" - this is an actual API-level constraint on that one turn
    # (the model must call *some* tool, not specifically the right one,
    # but combined with the nudge text naming the right one, this is the
    # strongest guarantee available short of picking the tool call
    # ourselves), not a suggestion it can decline.
    force_tool_choice_next_turn = False

    nudge_message = {
        "role": "system",
        "content": (
            "You have not called get_company_signals or compare_companies at "
            "all yet - no company-level financial data has actually been "
            "retrieved. If the user's question concerns a specific company or a "
            "comparison between companies, you must call one of those tools now "
            "before answering - do not draft a final answer from general "
            "knowledge, from a company name alone, or from sector-level context "
            "only. If the question genuinely has no specific company to look up "
            "(e.g. it's asking only about sector-wide qualitative signals), you "
            "may proceed without one."
        ),
    }

    for iteration in range(MAX_TOOL_ITERATIONS):
        response = client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            messages=messages,
            # Bug #17: on the one forced turn right after a nudge, the tool
            # list itself is narrowed to LOOKUP_TOOLS (get_company_signals +
            # compare_companies only) - see this function's docstring bug
            # #17 for why tool_choice="required" against the FULL tool list
            # was not enough (it forces "call something", and something
            # included list_companies, which the model kept re-choosing).
            tools=LOOKUP_TOOLS if force_tool_choice_next_turn else OPENAI_TOOLS,
            # "required" (not "auto") for exactly one turn right after a
            # nudge fires - see bug #16 above. Reset immediately after use
            # so this doesn't force a tool call forever if the model still
            # doesn't produce the right one on that turn.
            tool_choice="required" if force_tool_choice_next_turn else "auto",
            # Low, not zero: tool-call/stop-vs-continue decisions should be
            # consistent run to run (see DECISIONS.md #4's live-verification
            # note - at the default temperature, 2 of 3 personas skipped the
            # follow-up tool call the system prompt asks for; this reduces
            # that variance without pinning to greedy decoding).
            temperature=0.2,
        )
        force_tool_choice_next_turn = False
        message = response.choices[0].message
        messages.append(message.model_dump(exclude_none=True))

        if not message.tool_calls:
            if not attempted_company_lookup and not nudged:
                nudged = True
                force_tool_choice_next_turn = True
                messages.append(nudge_message)
                continue
            break

        for tool_call in message.tool_calls:
            name = tool_call.function.name
            if name not in TOOL_NAMES:
                result = {"error": f"unknown tool '{name}'"}
            else:
                if name in ("get_company_signals", "compare_companies"):
                    # Set BEFORE parsing arguments or dispatching,
                    # deliberately: a call that attempts the right tool but
                    # turns out to be malformed (see the except branch
                    # below) still counts as an attempt, not a skip - it
                    # should not trigger bug #8/#14's tool-forcing nudge,
                    # which exists to catch a model that never tries at
                    # all, not one that tried and made a mistake in the
                    # arguments. This must run even if json.loads or
                    # _dispatch below raises, which is why it's outside the
                    # try block, not the first line inside it.
                    attempted_company_lookup = True
                try:
                    args = json.loads(tool_call.function.arguments or "{}")
                    result = await _dispatch(mcp_client, name, args, sector, sector_company_slugs)
                    if name == "get_company_signals" and result.get("found") is False:
                        if result.get("out_of_sector"):
                            out_of_scope_sector.append(result.get("company_slug", "?"))
                        else:
                            unresolved.append(result.get("company_slug", args.get("company_slug", "?")))
                    if name == "compare_companies":
                        unresolved.extend(result.get("unknown_slugs", []))
                        out_of_scope_sector.extend(result.get("out_of_sector_slugs", []))
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    # A malformed tool call from the model (invalid JSON in
                    # arguments, or a required key like company_slug
                    # missing) used to propagate as an uncaught exception,
                    # failing the ENTIRE request even though only one of
                    # possibly several tool calls in this turn was bad,
                    # and even though real data may already have been
                    # gathered earlier in the same loop. Reported back to
                    # the model as a normal tool error instead (same shape
                    # as the "unknown tool" case above) so it gets a
                    # chance to retry with corrected arguments and the rest
                    # of the request can still succeed.
                    result = {"error": f"invalid arguments for tool '{name}': {exc}"}

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": wrap_untrusted("TOOL RESULT", json.dumps(result)),
                }
            )

        # Bug #14: don't wait for the model to voluntarily stop calling
        # tools before nudging it. A model that keeps calling non-essential
        # tools (list_companies, list_sectors) turn after turn never
        # produces the "no tool_calls" turn the check above waits for, so
        # left unchecked it can burn the entire MAX_TOOL_ITERATIONS budget
        # without ever attempting the required lookup. Once only one
        # iteration remains, force the nudge here instead, guaranteeing the
        # model's last turn is spent under an explicit instruction to call
        # the real lookup tool rather than silently falling out of the loop.
        remaining_iterations = MAX_TOOL_ITERATIONS - (iteration + 1)
        if not attempted_company_lookup and not nudged and remaining_iterations <= 1:
            nudged = True
            force_tool_choice_next_turn = True
            messages.append(nudge_message)

    return messages, unresolved, out_of_scope_sector


async def _dispatch(
    mcp_client: MCPToolClient, name: str, args: dict, sector: str, sector_company_slugs: set[str]
) -> dict:
    """Executes one tool call. Two of the four tools (list_companies,
    search_sector_context) take a `sector` argument the model controls -
    that argument is always overridden to the conversation's real,
    declared `sector` here, never trusted from the model. The other two
    (get_company_signals, compare_companies) take company slugs with no
    sector argument at all; those are checked against
    sector_company_slugs and blocked before any DB call if the slug
    belongs to a different sector. See this module's docstring bug #4 and
    guardrails.build_sector_scope_notice for the live bug this closes."""
    if name == "list_companies":
        return {"companies": await mcp_client.list_companies(sector)}
    if name == "get_company_signals":
        slug = args["company_slug"]
        if slug not in sector_company_slugs:
            return {"found": False, "company_slug": slug, "out_of_sector": True}
        return await mcp_client.get_company_signals(slug)
    if name == "compare_companies":
        requested = args["company_slugs"]
        in_sector = [s for s in requested if s in sector_company_slugs]
        out_of_sector = [s for s in requested if s not in sector_company_slugs]
        if in_sector:
            result = await mcp_client.compare_companies(in_sector, args.get("metric_name"))
        else:
            result = {"companies": {}, "unknown_slugs": []}
        result["out_of_sector_slugs"] = out_of_sector
        return result
    if name == "search_sector_context":
        return await mcp_client.search_sector_context(sector, args.get("signal_type"))
    raise ValueError(f"unhandled tool '{name}'")


def _extract_evidence(messages: list[dict]) -> tuple[dict[str, str], int, str | None, set[str]]:
    """Walks the transcript's own tool results (ground truth for what the
    model actually saw, rather than re-querying the DB) to compute evidence
    volume, freshness, and the set of companies actually resolved by a tool
    call - for the confidence score.

    The 4 MCP tools return 3 structurally different shapes, and each one is
    handled explicitly here rather than assuming they all look like
    get_company_signals's output:
      - get_company_signals: {"found": bool, "metrics": [...], "signals": [...]}
      - compare_companies:   {"companies": {slug: [rows]}, "unknown_slugs": [...]}
      - search_sector_context: {"sector": {...}, "signals": [...]}
    A row counts as evidence only when it carries a source_url - a row
    without one (e.g. an unknown-slug placeholder) is never counted.

    resolved_companies (the 4th return value) exists because a live run on
    2026-09-04 surfaced a real bug: `AgentResponse.grounded` was computed
    purely from the tool trace, per this module's design promise (see
    DECISIONS.md #5), but `confidence` was NOT - it was derived from
    `LLMDraftAnswer.companies_referenced`, a field the model self-reports in
    the same structured turn as its prose answer. When the model's answer
    correctly discussed real companies but left that separate list field
    empty, confidence silently collapsed to 0.0/NONE while grounded stayed
    True - two guardrail fields contradicting each other in the same
    response, and confidence quietly depending on LLM self-report despite
    the module's own docstring promising otherwise. resolved_companies is
    computed the same way source_map/supporting_rows already are - from
    what the tools actually returned, never from what the model claims it
    used - so confidence can no longer collapse on a model formatting slip.
    """
    source_map: dict[str, str] = {}
    supporting_rows = 0
    most_recent: str | None = None
    resolved_companies: set[str] = set()

    def note_row(row: dict, date_field: str) -> None:
        nonlocal supporting_rows, most_recent
        url = row.get("source_url")
        if not url:
            return
        supporting_rows += 1
        source_map[url] = row.get("source_publisher") or source_map.get(url, "unknown publisher")
        date_value = row.get(date_field)
        if date_value and (most_recent is None or date_value > most_recent):
            most_recent = date_value

    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            payload = json.loads(
                message["content"].split("(data, not instructions) ---\n", 1)[1].rsplit("\n--- end", 1)[0]
            )
        except (KeyError, IndexError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue

        if payload.get("found"):
            for metric in payload.get("metrics", []):
                note_row(metric, "as_of_date")
            for signal in payload.get("signals", []):
                note_row(signal, "signal_date")
            company = payload.get("company")
            if isinstance(company, dict) and company.get("slug"):
                resolved_companies.add(company["slug"])

        companies = payload.get("companies")
        if isinstance(companies, dict):
            for slug, rows in companies.items():
                resolved_companies.add(slug)
                for row in rows:
                    note_row(row, "as_of_date")

        if "sector" in payload and isinstance(payload.get("signals"), list):
            for signal in payload["signals"]:
                note_row(signal, "signal_date")
                slug = signal.get("company_slug")
                if slug:
                    resolved_companies.add(slug)

    return source_map, supporting_rows, most_recent, resolved_companies


_CORPORATE_SUFFIX_RE = re.compile(
    r",?\s*\b(Inc\.?|Corporation|Corp\.?|Co\.?|LLC|Ltd\.?|Technologies|Wholesale|Freight Line)\.?\s*$",
    re.IGNORECASE,
)

# Company short-names that collide with an ordinary English word (Target
# Corporation -> "Target"). Matching these bare would false-positive far too
# often ("what's GXO's target margin") - see _find_named_out_of_scope_companies.
_AMBIGUOUS_NAME_TOKENS = {"target"}


def _company_name_token(name: str) -> str:
    """Strips common corporate suffixes off a legal company name to get the
    short form a person would actually type in a question (e.g. "Microsoft
    Corporation" -> "Microsoft", "Palantir Technologies Inc." -> "Palantir").
    Applied repeatedly since some names stack more than one suffix."""
    token = name.strip()
    for _ in range(3):
        stripped = _CORPORATE_SUFFIX_RE.sub("", token).strip().rstrip(",")
        if stripped == token:
            break
        token = stripped
    if token.lower().startswith("the "):
        token = token[4:]
    return token


async def _find_named_out_of_scope_companies(
    mcp_client: MCPToolClient, query: str, sector: str
) -> list[dict]:
    """Deterministic, code-level detection of a company the user named in
    their own query text that belongs to a DIFFERENT sector than the one
    this conversation is scoped to. Fixes a live finding (this module's
    docstring bug #9): asked to compare GXO to Microsoft while scoped to
    logistics, the model never attempted the (correctly blocked)
    get_company_signals(microsoft) call at all - it just silently used UPS
    as the comparison target instead, producing a real, well-grounded
    answer to a DIFFERENT question than the one asked, with no disclosure
    that Microsoft was excluded or that a substitution had happened.

    build_sector_scope_notice (bug #4's fix) already handles this
    correctly WHEN the model actually attempts the blocked tool call - but
    that path is reactive: if the model reasons straight from "don't look
    up other sectors" to "I'll just use a different company" without ever
    trying, out_of_scope_sector stays empty and the notice never fires.
    This function is the proactive counterpart: it checks the user's own
    query text against every OTHER sector's real company directory
    (fetched fresh via MCPToolClient for this one request - never
    hardcoded, so it can't drift from the database as sectors/companies
    change), and returns a match regardless of what the model does.
    run_agent folds any match into out_of_scope_sector before building the
    pre-draft notices, so the disclosure requirement in
    build_sector_scope_notice fires either way.

    Deliberately biased toward precision over recall, per PDF review
    (docs/... the assignment's own "out-of-scope test" only requires
    honestly saying there's no data, which this generalizes to "no data
    IN SCOPE HERE"): matching is case-insensitive against a company's
    suffix-stripped short name (see _company_name_token), and case-
    SENSITIVE + word-bounded against its ticker (so lowercase "ups" in
    ordinary prose doesn't false-positive on the company UPS, but "UPS"
    does). A handful of companies have a short name that collides with an
    ordinary word (Target Corporation -> "Target"; see
    _AMBIGUOUS_NAME_TOKENS) - those match only by ticker or full legal
    name, never the bare short name, trading a few possible missed
    detections for not degrading an unrelated answer with a spurious
    "X is out of scope" disclosure. Any real cross-sector attempt this
    misses is still caught by the reactive path in _dispatch/
    _run_tool_loop if the model actually tries the lookup.
    """
    matches: list[dict] = []
    sectors = await mcp_client.list_sectors()
    for sector_row in sectors:
        other_sector = sector_row["slug"]
        if other_sector == sector:
            continue
        for company in await mcp_client.list_companies(other_sector):
            name = company.get("name") or ""
            ticker = company.get("ticker") or ""
            short_name = _company_name_token(name)
            hit = False
            if ticker and re.search(rf"\b{re.escape(ticker)}\b", query):
                hit = True
            elif short_name.lower() in _AMBIGUOUS_NAME_TOKENS:
                if re.search(rf"\b{re.escape(name)}\b", query, re.IGNORECASE):
                    hit = True
            elif short_name and re.search(rf"\b{re.escape(short_name)}\b", query, re.IGNORECASE):
                hit = True
            if hit:
                matches.append({"slug": company["slug"], "name": name, "sector": other_sector})
    return matches


def _build_pre_draft_notices(
    persona: Persona, sector: str, unresolved: list[str], out_of_scope_sector: list[str]
) -> list[str]:
    """The system messages appended right before Phase B drafts the final
    answer - the exact point where a real live-testing finding (bug #5,
    see this module's docstring) showed persona differentiation collapses.

    Manual UI testing on 2026-09-04 asked all 3 personas to compare GXO
    to Microsoft. All 3 made the identical, sufficient tool calls
    (get_company_signals returns the full record - revenue, margin,
    headcount, signals - not just margin), so every persona already had
    everything its own foreground criteria needed. All 3 still produced
    functionally the same answer: the two margin figures and a "Microsoft
    is stronger" conclusion, with zero engagement of Equity Analyst's
    earnings-quality lens, MF Analyst's growth-durability/benchmark
    framing, or PE Analyst's cash-generation/entry-thesis framing. This
    ruled out the tool-forcing fix that had been proposed for a DIFFERENT
    earlier finding (DECISIONS.md #4's cross-persona Tech-sector test,
    where 2 of 3 personas skipped an optional follow-up tool call) - here
    there was no missing tool call, so forcing one changes nothing.

    The actual mechanism: the persona instructions are stated once, at
    the very top of the system prompt, then buried under one or more
    tool-call round trips. The one system message that WAS freshly
    injected right before drafting ("Give your final answer now, as JSON
    matching the required schema.") said nothing about persona at all -
    an asymmetry with the grounding and sector-scope notices, which ARE
    re-injected at this exact point specifically because instructions
    lose weight over a long transcript. For a narrow, closed-form
    question ("which one is stronger"), that gave the model an easy,
    ungrounded-in-persona shortcut: state the numbers, declare a winner.

    Fix: re-inject render_persona_instructions(persona) - the same
    content already used to build the initial system prompt - as its own
    system message at this point too, so persona is the last substantive
    thing the model sees before it writes, not just the schema
    requirement. Extracted as a pure function (no OpenAI client
    involved) specifically so this can be tested offline without an LLM
    call - the model's actual compliance still needs a live check, but
    "did we remember to tell it" does not.
    """
    notices: list[str] = []
    grounding = build_grounding_notice(unresolved)
    if grounding:
        notices.append(grounding)
    sector_notice = build_sector_scope_notice(out_of_scope_sector, sector)
    if sector_notice:
        notices.append(sector_notice)
    notices.append(
        "Before drafting your final answer, re-apply your persona lens - do not skip this "
        "even if the question looks like it has a simple factual answer:\n"
        f"{render_persona_instructions(persona)}"
    )
    return notices


async def run_agent(persona: Persona | str, sector: str, query: str) -> AgentResponse:
    """The one agent entrypoint. Both api/main.py and ui/app.py call this
    and nothing else."""
    if isinstance(persona, str):
        persona = Persona(persona)

    messages: list[dict] = [
        {"role": "system", "content": _build_system_prompt(persona, sector)},
        {"role": "user", "content": wrap_untrusted("USER QUERY", query)},
    ]

    client = _client()

    async with MCPToolClient() as mcp_client:
        # Resolved once per request, before the tool loop, so _dispatch can
        # block any get_company_signals/compare_companies call for a slug
        # outside this sector without a per-call DB round trip - see this
        # module's docstring bug #4 for the live finding this closes.
        sector_company_slugs = {c["slug"] for c in await mcp_client.list_companies(sector)}

        # Proactive, deterministic detection of a named out-of-sector company
        # in the user's own query text - see _find_named_out_of_scope_companies'
        # docstring and this module's docstring bug #9 for the live finding
        # this closes (the model silently substituting UPS for Microsoft
        # without ever disclosing the exclusion). Folded into
        # out_of_scope_sector alongside whatever the reactive tool-call path
        # finds, deduplicated, so build_sector_scope_notice fires either way.
        named_out_of_scope = await _find_named_out_of_scope_companies(mcp_client, query, sector)

        messages, unresolved, out_of_scope_sector = await _run_tool_loop(
            client, mcp_client, messages, sector, sector_company_slugs
        )
        for match in named_out_of_scope:
            if match["slug"] not in out_of_scope_sector:
                out_of_scope_sector.append(match["slug"])

        for pre_draft_notice in _build_pre_draft_notices(persona, sector, unresolved, out_of_scope_sector):
            messages.append({"role": "system", "content": pre_draft_notice})
        messages.append(
            {
                "role": "system",
                "content": "Give your final answer now, as JSON matching the required schema. Do not call any more tools.",
            }
        )

        final = client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            messages=messages,
            response_format=_draft_answer_schema(),
        )
        draft = LLMDraftAnswer.model_validate_json(final.choices[0].message.content)

        source_map, supporting_rows, most_recent, resolved_companies = _extract_evidence(messages)

        # companies_requested/found drive the resolution-rate term of
        # compute_confidence and MUST come from the tool trace
        # (resolved_companies), not from draft.companies_referenced - see
        # _extract_evidence's docstring for the live bug this fixes. The
        # displayed companies_referenced field below is now ALSO computed
        # purely from resolved_companies, with no fallback to
        # draft.companies_referenced at all. An earlier version of this
        # line unioned in draft.companies_referenced as a "safety net" -
        # that was a mistake: it silently reintroduced the exact
        # self-report dependency this fix exists to remove, just moved
        # from the confidence math into the user-facing display field
        # (ui/app.py renders this list directly). If a real company the
        # model discussed is missing here, the fix is to teach
        # _extract_evidence to recognize the tool shape that surfaced it,
        # not to fall back to trusting the model's own list. Blocked
        # out-of-sector slugs count toward companies_requested the same
        # way unresolved ones do - they were requested and did not
        # resolve to usable in-scope data, which is exactly what that
        # denominator measures; see build_sector_scope_notice for why
        # they're tracked in a separate list from `unresolved` despite
        # folding into the same confidence-math role here.
        companies_requested = len(resolved_companies | set(unresolved) | set(out_of_scope_sector))
        companies_found = len(resolved_companies)
        confidence, confidence_level = compute_confidence(
            companies_requested=companies_requested,
            companies_found=companies_found,
            supporting_rows=supporting_rows,
            most_recent_as_of=most_recent,
        )

        return AgentResponse(
            answer=draft.answer,
            persona=persona,
            sector=sector,
            companies_referenced=sorted(resolved_companies),
            sources=[SourceRef(url=url, publisher=publisher) for url, publisher in source_map.items()],
            confidence=confidence,
            confidence_level=confidence_level,
            limitations=draft.limitations,
            grounded=supporting_rows > 0,
            tool_calls=mcp_client.call_log_summary(),
        )
