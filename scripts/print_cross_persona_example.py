"""
Runs the exact take-home-named cross-persona query (PDF: "Is this sector
a good place to be putting money to work right now?", Tech) through all 3
personas and prints a markdown table plus the full answers, so the output
can be pasted straight into README.md section 11.

This is a one-off reporting script, not part of the tested application
surface (api/main.py, ui/app.py) - it exists only to produce real,
non-fabricated example output for the README, using the same run_agent
entrypoint everything else goes through.

Usage:
    OPENAI_API_KEY=sk-... python scripts/print_cross_persona_example.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.core import run_agent
from agent.schemas import Persona


async def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set. Run this with your key set in the environment.", file=sys.stderr)
        sys.exit(1)

    question = "Is this sector a good place to be putting money to work right now?"
    sector = "tech"
    results = {}

    for persona in Persona:
        response = await run_agent(persona, sector, question)
        results[persona.value] = response

    print("=" * 100)
    print(f"QUERY: {question!r}  |  SECTOR: {sector}")
    print("=" * 100)
    for persona_value, response in results.items():
        print(f"\n--- {persona_value} ---")
        print(f"confidence: {response.confidence} ({response.confidence_level.value})")
        print(f"grounded: {response.grounded}")
        print(f"companies_referenced: {response.companies_referenced}")
        print(f"tool_calls: {response.tool_calls}")
        print(f"sources: {[s.url for s in response.sources]}")
        print(f"\nanswer:\n{response.answer}")
        if response.limitations:
            print(f"\nlimitations: {response.limitations}")

    print("\n" + "=" * 100)
    print("RAW JSON (for pasting exact text into README without retyping):")
    print("=" * 100)
    dump = {
        persona_value: {
            "answer": response.answer,
            "confidence": response.confidence,
            "confidence_level": response.confidence_level.value,
            "grounded": response.grounded,
            "companies_referenced": response.companies_referenced,
            "tool_calls": response.tool_calls,
            "sources": [{"url": s.url, "publisher": s.publisher} for s in response.sources],
            "limitations": response.limitations,
        }
        for persona_value, response in results.items()
    }
    print(json.dumps(dump, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
