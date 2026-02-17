"""
FPL Agent — main entry point.

Usage:
    python main.py                 # interactive REPL
    python main.py "Who should I captain this week?"   # one-shot
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown

from fpl.agent import build_agent

load_dotenv()  # loads .env → sets OPENAI_API_KEY, FPL_TEAM_ID, etc.

console = Console()


def run_query(agent, query: str) -> str:
    """Send a single query through the agent graph and return the final answer."""
    result = agent.invoke(
        {"messages": [{"role": "user", "content": query}]}
    )
    # The last message is the assistant's final answer
    return result["messages"][-1].content


def main():
    console.print("[bold green]⚽ FPL Agent[/bold green] — powered by LangGraph\n")

    team_id = os.getenv("FPL_TEAM_ID", "").strip()
    if team_id:
        console.print(f"  Team ID: [bold]{team_id}[/bold]  ✅  (\"my team\" queries enabled)")
    else:
        console.print("  Team ID: [dim]not set[/dim]")
        console.print("  [dim]To enable \"my team\" queries, either:[/dim]")
        console.print("    [dim]• Add FPL_TEAM_ID=<your_id> to .env[/dim]")
        console.print("    [dim]• Run: [bold]python -m fpl.login[/bold]  (logs in with email/password to fetch it)[/dim]")
    console.print()

    agent = build_agent()

    # One-shot mode: pass query as CLI arg
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
        answer = run_query(agent, query)
        console.print(Markdown(answer))
        return

    # Interactive REPL
    console.print("Type your FPL question (or [bold]quit[/bold] to exit).\n")
    while True:
        try:
            query = console.input("[bold cyan]You:[/bold cyan] ")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("quit", "exit", "q"):
            break
        answer = run_query(agent, query)
        console.print()
        console.print(Markdown(answer))
        console.print()


if __name__ == "__main__":
    main()
