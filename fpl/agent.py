"""
LangGraph agent — a simple ReAct loop that can call FPL tools.

Graph shape
-----------

  ┌──────────┐     tool calls?     ┌───────────┐
  │  agent   │ ──── yes ──────────►│   tools   │
  │  (LLM)   │◄────────────────────│ (execute) │
  └──────────┘      results        └───────────┘
       │
       │ no tool calls → END
       ▼
    __end__

This is the canonical "tool-calling agent" pattern from the LangGraph
docs.  It's the simplest useful agent you can build.
"""

from __future__ import annotations

import logging

from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from fpl.tools import ALL_TOOLS

log = logging.getLogger(__name__)

# ── System prompt that gives the LLM its FPL personality ─────────────
SYSTEM_PROMPT = """\
You are **FPL Advisor**, an expert Fantasy Premier League assistant.

You help managers with:
• Reviewing their current squad, bench, captain choice, and chips used
• Suggesting transfers based on the user's squad, budget, and free transfers
• Picking the best squad and captain each gameweek
• Finding value picks & differentials
• Analysing fixture difficulty and form
• Comparing players side-by-side

When the user asks about "my team", "my squad", or "my players":
1. Use the team tools (get_my_team, get_my_season_history, get_my_transfers) to fetch their data.
2. Cross-reference their players with form/fixture data from other tools.
3. Give concrete, personalised advice.

Rules:
1. Always back up opinions with data — call a tool first.
2. State player prices in £m (e.g. £7.5m).
3. When comparing players, show a short table.
4. If the user asks something outside FPL, politely decline.
"""


def build_agent(model_name: str = "gpt-4o-mini", temperature: float = 0):
    """Build and return a compiled LangGraph ReAct agent."""
    log.info("Building agent (model=%s, temp=%.1f, tools=%d)",
             model_name, temperature, len(ALL_TOOLS))
    llm = ChatOpenAI(model=model_name, temperature=temperature)

    agent = create_react_agent(
        model=llm,
        tools=ALL_TOOLS,
        prompt=SYSTEM_PROMPT,
    )

    log.info("Agent built ✓")
    return agent
