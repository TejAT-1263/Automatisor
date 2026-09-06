"""
Streamlit UI. Like api/main.py, this is a thin front door: it collects
persona/sector/query, calls agent.core.run_agent, and renders the
structured AgentResponse. No analysis logic lives here. See DECISIONS.md
#1.

The one non-agent DB touch on this page is populating the sector dropdown
- that still goes through MCPToolClient (never mcp_server.queries
directly), it's just not routed through run_agent because it isn't an
analytical query. See DECISIONS.md #8.

Run:
    streamlit run ui/app.py

Path note: `streamlit run ui/app.py` (the plain console-script form) puts
this file's own directory (ui/) on sys.path, not the project root — so
`from agent.core import ...` below fails with `ModuleNotFoundError: No
module named 'agent'` unless the project root happens to already be on
sys.path some other way (e.g. invoking as `python -m streamlit run
ui/app.py`, where the `-m` flag adds the current directory). Rather than
require one specific invocation form, the project root is added to
sys.path explicitly below, based on this file's own location - so this
works the same way regardless of how streamlit was launched or what the
shell's cwd was at the time.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import streamlit as st
from dotenv import load_dotenv

from agent.core import run_agent
from agent.mcp_client import MCPToolClient
from agent.personas import PERSONA_DEFINITIONS
from agent.schemas import Persona

load_dotenv()

st.set_page_config(page_title="Persona Agent", page_icon="📊", layout="centered")


@st.cache_data(ttl=300)
def _load_sectors() -> list[dict]:
    async def _fetch() -> list[dict]:
        async with MCPToolClient() as client:
            return await client.list_sectors()

    return asyncio.run(_fetch())


def _run_query(persona: Persona, sector: str, query: str):
    return asyncio.run(run_agent(persona, sector, query))


st.title("Persona Agent")
st.caption("A persona-configurable financial analysis agent, grounded via MCP.")

try:
    sectors = _load_sectors()
except FileNotFoundError:
    st.error("Database not found. Run `python db/build_db.py` first, then reload this page.")
    st.stop()

sector_labels = {s["name"]: s["slug"] for s in sectors}
persona_labels = {definition["display_name"]: persona for persona, definition in PERSONA_DEFINITIONS.items()}

with st.sidebar:
    st.subheader("Scope")
    persona_label = st.selectbox("Persona", list(persona_labels.keys()))
    sector_label = st.selectbox("Sector", list(sector_labels.keys()))
    st.caption(next(s["description"] for s in sectors if s["name"] == sector_label))

query = st.text_area(
    "Question",
    placeholder="e.g. Which companies in this sector look like attractive buyout targets based on the data you have?",
    height=100,
)

if st.button("Ask", type="primary", disabled=not query.strip()):
    persona = persona_labels[persona_label]
    sector = sector_labels[sector_label]
    with st.spinner(f"Answering as {persona_label}..."):
        try:
            response = _run_query(persona, sector, query)
        except Exception as exc:  # surfaced to the user, not swallowed
            st.error(f"The agent failed to answer: {exc}")
            st.stop()

    st.markdown(response.answer)

    cols = st.columns(3)
    cols[0].metric("Confidence", f"{response.confidence:.0%}", response.confidence_level.value)
    cols[1].metric("Companies referenced", len(response.companies_referenced))
    cols[2].metric("Grounded", "yes" if response.grounded else "no")

    if response.companies_referenced:
        st.caption("Companies referenced: " + ", ".join(response.companies_referenced))

    if response.limitations:
        with st.expander("Limitations noted by the agent"):
            for item in response.limitations:
                st.write(f"- {item}")

    if response.sources:
        with st.expander(f"Sources ({len(response.sources)})"):
            for source in response.sources:
                st.write(f"- [{source.publisher}]({source.url})")

    with st.expander(f"MCP tool calls made ({len(response.tool_calls)})"):
        if response.tool_calls:
            for call in response.tool_calls:
                st.code(call, language=None)
        else:
            st.write("No tool calls were made — this answer is not grounded in the database.")
