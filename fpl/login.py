"""
FPL Login helper — fetch your Team ID using your FPL email & password.

Usage:
    python -m fpl.login                  # interactive prompt
    python -m fpl.login you@email.com    # prompt for password only

This script:
  1. Logs in to users.premierleague.com
  2. Calls /api/me/ to get your team ID
  3. Saves FPL_TEAM_ID to your .env file (creates it if needed)

Your password is NEVER stored — only the numeric team ID is saved.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

from rich.console import Console

from fpl.api_client import FPLClient

console = Console()
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _update_env_file(team_id: int) -> None:
    """Write or update FPL_TEAM_ID in the .env file."""
    if ENV_PATH.exists():
        content = ENV_PATH.read_text(encoding="utf-8")
    else:
        content = ""

    # Replace existing FPL_TEAM_ID line, or append
    lines = content.splitlines()
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("FPL_TEAM_ID") and "=" in stripped:
            lines[i] = f"FPL_TEAM_ID={team_id}"
            found = True
            break

    if not found:
        lines.append(f"FPL_TEAM_ID={team_id}")

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def login_and_fetch_team_id(email: str, password: str) -> dict:
    """Login and return the /me/ profile. Raises on failure."""
    return FPLClient.login(email, password)


def main():
    console.print("[bold green]⚽ FPL Login[/bold green] — fetch your Team ID\n")
    console.print("[dim]Your password is used once to call the FPL API and is never stored.[/dim]\n")

    # Get email from CLI arg or prompt
    if len(sys.argv) > 1:
        email = sys.argv[1]
        console.print(f"  Email: [bold]{email}[/bold]")
    else:
        email = console.input("[bold cyan]FPL Email:[/bold cyan] ").strip()

    if not email:
        console.print("[red]Email cannot be empty.[/red]")
        sys.exit(1)

    # Always prompt for password (hidden input)
    password = getpass.getpass("FPL Password: ")
    if not password:
        console.print("[red]Password cannot be empty.[/red]")
        sys.exit(1)

    console.print("\n[dim]Logging in…[/dim]")

    try:
        profile = login_and_fetch_team_id(email, password)
    except Exception as e:
        console.print(f"\n[red]Login failed:[/red] {e}")
        console.print("[dim]Check your email/password and try again.[/dim]")
        sys.exit(1)

    team_id = profile.get("player", {}).get("entry")
    if team_id is None:
        # Fallback: some API versions put it at top level
        team_id = profile.get("entry")

    if team_id is None:
        console.print("\n[red]Login succeeded but no team ID found in profile.[/red]")
        console.print("[dim]You may not have an FPL team registered for this season.[/dim]")
        console.print(f"\n[dim]Profile response: {profile}[/dim]")
        sys.exit(1)

    # Show results
    player = profile.get("player", profile)
    first = player.get("first_name", "")
    last = player.get("last_name", "")

    console.print(f"\n[green]✅ Logged in as:[/green] {first} {last}")
    console.print(f"[green]   Team ID:[/green]     [bold]{team_id}[/bold]")

    # Save to .env
    _update_env_file(team_id)
    console.print(f"\n[green]Saved[/green] FPL_TEAM_ID={team_id} → [bold]{ENV_PATH}[/bold]")
    console.print("[dim]You can now run: python main.py[/dim]\n")

    # Also set in current process env so subsequent code can use it
    os.environ["FPL_TEAM_ID"] = str(team_id)

    return team_id


if __name__ == "__main__":
    main()
