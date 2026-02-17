"""
LangChain tools that wrap the FPL API client.

Each tool is a plain function decorated with @tool so LangGraph / LangChain
can bind them to an LLM automatically.
"""

from __future__ import annotations

import json
import os
from langchain_core.tools import tool
from fpl.api_client import FPLClient


# We keep a module-level client so it's reused across tool calls.
_client = FPLClient()


# ── Helper to map element_type ids to readable positions ──────────────
_POSITION_MAP = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


def _get_team_id() -> int | None:
    """Read FPL_TEAM_ID from environment. Returns None if not set."""
    raw = os.getenv("FPL_TEAM_ID", "").strip()
    return int(raw) if raw else None


def _current_gameweek() -> int | None:
    """Find the current gameweek number from bootstrap data."""
    events = _client.get_gameweeks()
    current = next((e for e in events if e["is_current"]), None)
    return current["id"] if current else None


# ======================================================================
#  TOOLS
# ======================================================================


@tool
def get_top_players_by_form(top_n: int = 10) -> str:
    """Return the top N players ranked by current form (points per match
    over the recent window).  Useful for picking differentials or
    captaincy candidates.
    """
    players = _client.get_all_players()
    teams = {t["id"]: t["short_name"] for t in _client.get_all_teams()}

    sorted_players = sorted(
        players,
        key=lambda p: float(p.get("form", 0)),
        reverse=True,
    )[:top_n]

    results = []
    for p in sorted_players:
        results.append(
            {
                "name": p["web_name"],
                "team": teams.get(p["team"], "?"),
                "position": _POSITION_MAP.get(p["element_type"], "?"),
                "form": p["form"],
                "price": p["now_cost"] / 10,
                "total_points": p["total_points"],
                "minutes": p["minutes"],
                "selected_by": p["selected_by_percent"],
            }
        )
    return json.dumps(results, indent=2)


@tool
def get_player_details(player_name: str) -> str:
    """Look up a player by (partial) name and return their full stats
    including upcoming fixtures and recent history.
    """
    players = _client.get_all_players()
    teams = {t["id"]: t["short_name"] for t in _client.get_all_teams()}
    name_lower = player_name.lower()

    match = next(
        (
            p
            for p in players
            if name_lower in p["web_name"].lower()
            or name_lower in p["second_name"].lower()
            or name_lower in p["first_name"].lower()
        ),
        None,
    )

    if match is None:
        return json.dumps({"error": f"No player found matching '{player_name}'"})

    summary = _client.get_player_summary(match["id"])

    return json.dumps(
        {
            "name": f"{match['first_name']} {match['second_name']}",
            "web_name": match["web_name"],
            "team": teams.get(match["team"], "?"),
            "position": _POSITION_MAP.get(match["element_type"], "?"),
            "price": match["now_cost"] / 10,
            "total_points": match["total_points"],
            "form": match["form"],
            "goals": match["goals_scored"],
            "assists": match["assists"],
            "clean_sheets": match["clean_sheets"],
            "minutes": match["minutes"],
            "xG": match.get("expected_goals"),
            "xA": match.get("expected_assists"),
            "selected_by": match["selected_by_percent"],
            "news": match["news"],
            "chance_of_playing": match.get("chance_of_playing_next_round"),
            "upcoming_fixtures": summary.get("fixtures", [])[:5],
            "recent_history": summary.get("history", [])[-5:],
        },
        indent=2,
    )


@tool
def get_current_gameweek_info() -> str:
    """Return information about the current (or next) gameweek —
    deadlines, most-captained, chip plays, etc.
    """
    events = _client.get_gameweeks()
    current = next((e for e in events if e["is_current"]), None)
    upcoming = next((e for e in events if e["is_next"]), None)

    return json.dumps(
        {"current_gameweek": current, "next_gameweek": upcoming},
        indent=2,
        default=str,
    )


@tool
def get_fixtures_for_gameweek(gameweek: int) -> str:
    """Return all fixtures for a given gameweek number."""
    fixtures = _client.get_fixtures(gameweek=gameweek)
    teams = {t["id"]: t["short_name"] for t in _client.get_all_teams()}

    results = []
    for f in fixtures:
        results.append(
            {
                "home": teams.get(f["team_h"], "?"),
                "away": teams.get(f["team_a"], "?"),
                "home_difficulty": f.get("team_h_difficulty"),
                "away_difficulty": f.get("team_a_difficulty"),
                "kickoff": f.get("kickoff_time"),
                "finished": f.get("finished"),
                "score": f"{f.get('team_h_score', '?')}-{f.get('team_a_score', '?')}",
            }
        )
    return json.dumps(results, indent=2, default=str)


@tool
def get_best_value_players(position: str = "MID", top_n: int = 10) -> str:
    """Return the best value players (points per £m) for a given position.
    Position must be one of: GKP, DEF, MID, FWD.
    """
    pos_id = {"GKP": 1, "DEF": 2, "MID": 3, "FWD": 4}.get(position.upper())
    if pos_id is None:
        return json.dumps({"error": f"Unknown position '{position}'. Use GKP/DEF/MID/FWD."})

    players = _client.get_all_players()
    teams = {t["id"]: t["short_name"] for t in _client.get_all_teams()}

    filtered = [p for p in players if p["element_type"] == pos_id and p["minutes"] > 0]

    for p in filtered:
        price = p["now_cost"] / 10
        p["_value"] = p["total_points"] / price if price > 0 else 0

    filtered.sort(key=lambda p: p["_value"], reverse=True)

    results = []
    for p in filtered[:top_n]:
        results.append(
            {
                "name": p["web_name"],
                "team": teams.get(p["team"], "?"),
                "price": p["now_cost"] / 10,
                "total_points": p["total_points"],
                "value": round(p["_value"], 2),
                "form": p["form"],
                "selected_by": p["selected_by_percent"],
            }
        )
    return json.dumps(results, indent=2)


# ======================================================================
#  USER / MANAGER TEAM TOOLS
# ======================================================================


@tool
def get_my_team(gameweek: int | None = None) -> str:
    """Return the user's current squad for a given gameweek (defaults to the
    current GW).  Shows each player with position, team, price, form, points,
    whether they are on the bench, and who is captain / vice-captain.
    Also includes the manager's overall rank, total points, bank, and squad value.
    """
    team_id = _get_team_id()
    if team_id is None:
        return json.dumps({"error": "FPL_TEAM_ID is not set in .env. Please add it."})

    gw = gameweek or _current_gameweek()
    if gw is None:
        return json.dumps({"error": "Could not determine the current gameweek."})

    # Fetch data
    picks_data = _client.get_team_picks(team_id, gw)
    team_info = _client.get_team_info(team_id)
    all_players = {p["id"]: p for p in _client.get_all_players()}
    teams = {t["id"]: t["short_name"] for t in _client.get_all_teams()}

    picks = picks_data.get("picks", [])
    active_chip = picks_data.get("active_chip")
    entry_history = picks_data.get("entry_history", {})

    squad = []
    for pick in picks:
        player = all_players.get(pick["element"], {})
        squad.append(
            {
                "name": player.get("web_name", "?"),
                "position": _POSITION_MAP.get(player.get("element_type"), "?"),
                "team": teams.get(player.get("team"), "?"),
                "price": player.get("now_cost", 0) / 10,
                "form": player.get("form", "0"),
                "total_points": player.get("total_points", 0),
                "gw_points": pick.get("points", "?"),
                "is_captain": pick.get("is_captain", False),
                "is_vice_captain": pick.get("is_vice_captain", False),
                "on_bench": pick.get("position", 0) > 11,
                "multiplier": pick.get("multiplier", 1),
            }
        )

    return json.dumps(
        {
            "manager": f"{team_info.get('player_first_name', '')} {team_info.get('player_last_name', '')}",
            "team_name": team_info.get("name", "?"),
            "gameweek": gw,
            "overall_points": team_info.get("summary_overall_points"),
            "overall_rank": team_info.get("summary_overall_rank"),
            "gw_points": entry_history.get("points"),
            "bank": entry_history.get("bank", 0) / 10,
            "squad_value": entry_history.get("value", 0) / 10,
            "transfers_made": entry_history.get("event_transfers", 0),
            "transfer_cost": entry_history.get("event_transfers_cost", 0),
            "active_chip": active_chip,
            "squad": squad,
        },
        indent=2,
    )


@tool
def get_my_season_history() -> str:
    """Return the user's gameweek-by-gameweek history for the current season.
    Shows points, rank, bank, squad value, and transfers per gameweek.
    Also includes past-season summaries if available.
    """
    team_id = _get_team_id()
    if team_id is None:
        return json.dumps({"error": "FPL_TEAM_ID is not set in .env. Please add it."})

    history = _client.get_team_history(team_id)

    current = []
    for gw in history.get("current", []):
        current.append(
            {
                "gameweek": gw["event"],
                "points": gw["points"],
                "total_points": gw["total_points"],
                "rank": gw["rank"],
                "overall_rank": gw["overall_rank"],
                "bank": gw["bank"] / 10,
                "squad_value": gw["value"] / 10,
                "transfers": gw["event_transfers"],
                "transfer_cost": gw["event_transfers_cost"],
                "bench_points": gw["points_on_bench"],
            }
        )

    past_seasons = history.get("past", [])

    return json.dumps(
        {"current_season": current, "past_seasons": past_seasons},
        indent=2,
    )


@tool
def get_my_transfers() -> str:
    """Return the user's full transfer history — every transfer made this season,
    showing the player bought, player sold, prices, and when it happened.
    """
    team_id = _get_team_id()
    if team_id is None:
        return json.dumps({"error": "FPL_TEAM_ID is not set in .env. Please add it."})

    transfers = _client.get_team_transfers(team_id)
    all_players = {p["id"]: p for p in _client.get_all_players()}

    results = []
    for t in transfers:
        player_in = all_players.get(t["element_in"], {})
        player_out = all_players.get(t["element_out"], {})
        results.append(
            {
                "gameweek": t.get("event"),
                "time": t.get("time"),
                "player_in": player_in.get("web_name", f"id:{t['element_in']}"),
                "price_in": t.get("element_in_cost", 0) / 10,
                "player_out": player_out.get("web_name", f"id:{t['element_out']}"),
                "price_out": t.get("element_out_cost", 0) / 10,
            }
        )

    return json.dumps(results, indent=2, default=str)


# ── Public list so the agent graph can import all tools at once ───────
ALL_TOOLS = [
    get_top_players_by_form,
    get_player_details,
    get_current_gameweek_info,
    get_fixtures_for_gameweek,
    get_best_value_players,
    get_my_team,
    get_my_season_history,
    get_my_transfers,
]
