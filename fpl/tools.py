"""
LangChain tools that wrap the FPL API client.

Each tool is a plain function decorated with @tool so LangGraph / LangChain
can bind them to an LLM automatically.

Sections:
  1. Shared private helpers (name resolution, team maps, fixture utils)
  2. Behaviour / predictability helpers
  3. General FPL Tools
  4. My Team / Manager Tools
  5. Planning & Transfer Tools
  6. Behaviour & Risk Tools
  7. Draft Builder Tools
  8. ALL_TOOLS export
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from langchain_core.tools import tool
from fpl.api_client import FPLClient

log = logging.getLogger(__name__)

# We keep a module-level client so it's reused across tool calls.
_client = FPLClient()


# ── Helper to map element_type ids to readable positions ──────────────
_POSITION_MAP = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
_POS_ID = {"GKP": 1, "DEF": 2, "MID": 3, "FWD": 4}


# ======================================================================
#  SHARED PRIVATE HELPERS
# ======================================================================

def _resolve_player(player_name: str) -> dict | None:
    """Case-insensitive partial match on web_name, second_name, first_name.
    Returns the full player element dict or None."""
    players = _client.get_all_players()
    name_lower = player_name.lower()
    return next(
        (
            p
            for p in players
            if name_lower in p["web_name"].lower()
            or name_lower in p["second_name"].lower()
            or name_lower in p["first_name"].lower()
        ),
        None,
    )


def _get_teams_map() -> dict[int, str]:
    """Return {team_id: short_name} map from bootstrap data (cached by client)."""
    return {t["id"]: t["short_name"] for t in _client.get_all_teams()}


def _avg_fixture_difficulty(fix_map: dict, team_id: int) -> float:
    """Average fixture difficulty for *team_id* within a pre-fetched fix_map.
    Returns 3.0 (neutral) when no fixtures are available."""
    fixtures = fix_map.get(team_id, [])
    if not fixtures:
        return 3.0
    return sum(f.get("difficulty", 3) for f in fixtures) / len(fixtures)


# ======================================================================
#  BEHAVIOUR / PREDICTABILITY HELPERS
# ======================================================================

def _compute_player_gw_stats(player_id: int) -> list[dict]:
    """Return per-GW history rows with the fields the behaviour layer needs.

    Source: /element-summary/{id}/  →  ["history"]
    """
    summary = _client.get_player_summary(player_id)
    history = summary.get("history", [])
    rows: list[dict] = []
    for h in history:
        rows.append({
            "gw": h.get("round"),
            "minutes": h.get("minutes", 0),
            "total_points": h.get("total_points", 0),
            "goals_scored": h.get("goals_scored", 0),
            "assists": h.get("assists", 0),
            "xg": float(h.get("expected_goals", 0) or 0),
            "xa": float(h.get("expected_assists", 0) or 0),
            "xgi": float(h.get("expected_goal_involvements", 0) or 0),
            "ict_index": float(h.get("ict_index", 0) or 0),
        })
    return rows


def _compute_explosiveness_and_consistency(
    history: list[dict], window: int = 10
) -> dict:
    """Compute explosiveness / consistency metrics over the last *window* GWs
    where the player actually appeared (minutes > 0)."""
    played = [h for h in history if h.get("minutes", 0) > 0]
    recent = played[-window:] if len(played) >= window else played

    if not recent:
        return {
            "avg_points": 0.0,
            "std_points": 0.0,
            "max_points": 0,
            "haul_rate": 0.0,
            "blank_rate": 0.0,
            "gws_used": 0,
        }

    pts = [h["total_points"] for h in recent]
    n = len(pts)
    avg = sum(pts) / n
    variance = sum((p - avg) ** 2 for p in pts) / n
    std = variance ** 0.5

    return {
        "avg_points": round(avg, 2),
        "std_points": round(std, 2),
        "max_points": max(pts),
        "haul_rate": round(sum(1 for p in pts if p >= 10) / n, 2),
        "blank_rate": round(sum(1 for p in pts if p <= 2) / n, 2),
        "gws_used": n,
    }


def _compute_regression_metrics(
    history: list[dict], season_totals: dict
) -> dict:
    """Compare actual goals/assists with xG/xA to flag over/under-performance."""
    season_goals = int(season_totals.get("goals_scored", 0))
    season_assists = int(season_totals.get("assists", 0))
    season_xg = float(season_totals.get("expected_goals", 0) or 0)
    season_xa = float(season_totals.get("expected_assists", 0) or 0)

    delta_g = round(season_goals - season_xg, 2)
    delta_a = round(season_assists - season_xa, 2)

    # Flag logic — threshold of ±1.5
    if delta_g > 1.5 or delta_a > 1.0:
        flag = "overperforming"
    elif delta_g < -1.5 or delta_a < -1.0:
        flag = "underperforming"
    else:
        flag = "in_line"

    return {
        "season_goals": season_goals,
        "season_xg": round(season_xg, 2),
        "season_assists": season_assists,
        "season_xa": round(season_xa, 2),
        "delta_goals_vs_xg": delta_g,
        "delta_assists_vs_xa": delta_a,
        "regression_flag": flag,
    }


def _compute_talisman_index(player_id: int) -> dict:
    """How much of a club's attacking output flows through this player?

    Uses season totals for the player and aggregates across the club.
    """
    all_players = _client.get_all_players()
    player = next((p for p in all_players if p["id"] == player_id), None)
    if player is None:
        return {"error": "player not found"}

    club_id = player["team"]
    teammates = [p for p in all_players if p["team"] == club_id]

    team_goals = sum(int(p.get("goals_scored", 0)) for p in teammates)
    team_xgi = sum(float(p.get("expected_goal_involvements", 0) or 0) for p in teammates)

    player_goals = int(player.get("goals_scored", 0)) + int(player.get("assists", 0))
    player_xgi = float(player.get("expected_goal_involvements", 0) or 0)

    goals_share = (player_goals / team_goals) if team_goals > 0 else 0.0
    xgi_share = (player_xgi / team_xgi) if team_xgi > 0 else 0.0
    talisman_index = round((goals_share + xgi_share) / 2, 3)

    return {
        "team_goals": team_goals,
        "player_goal_involvements": player_goals,
        "player_goals_share": round(goals_share, 3),
        "team_xgi": round(team_xgi, 2),
        "player_xgi": round(player_xgi, 2),
        "player_xgi_share": round(xgi_share, 3),
        "talisman_index": talisman_index,
    }


def _compute_reliability_score(player_id: int) -> dict:
    """Nailedness, injury risk, rotation risk → overall reliability 0-100."""
    all_players = _client.get_all_players()
    player = next((p for p in all_players if p["id"] == player_id), None)
    if player is None:
        return {"error": "player not found"}

    # -- nailedness: ratio of minutes played to max possible --
    gw_history = _compute_player_gw_stats(player_id)
    total_gws = len(gw_history)
    if total_gws == 0:
        return {
            "nailedness_score": 0.0,
            "injury_risk": "high",
            "rotation_risk": "high",
            "reliability_score": 0,
        }

    started_gws = sum(1 for h in gw_history if h["minutes"] >= 60)
    appeared_gws = sum(1 for h in gw_history if h["minutes"] > 0)
    nailedness = round(started_gws / total_gws, 2) if total_gws > 0 else 0.0

    # -- injury risk --
    chance = player.get("chance_of_playing_next_round")
    news = (player.get("news") or "").lower()
    if chance is not None and chance < 50:
        injury_risk = "high"
    elif chance is not None and chance < 75:
        injury_risk = "medium"
    elif any(kw in news for kw in ("injury", "knock", "hamstring", "groin", "muscle")):
        injury_risk = "medium"
    else:
        injury_risk = "low"

    # -- rotation risk --
    appearance_rate = appeared_gws / total_gws if total_gws else 0
    if nailedness >= 0.8 and appearance_rate >= 0.85:
        rotation_risk = "low"
    elif nailedness >= 0.55:
        rotation_risk = "medium"
    else:
        rotation_risk = "high"

    # -- composite reliability score 0-100 --
    base = nailedness * 60  # up to 60
    base += appearance_rate * 20  # up to 20
    # Injury adjustment
    inj_adj = {"low": 20, "medium": 10, "high": 0}
    base += inj_adj.get(injury_risk, 10)
    reliability_score = min(100, max(0, round(base)))

    return {
        "nailedness_score": nailedness,
        "starts": started_gws,
        "appearances": appeared_gws,
        "total_gws_elapsed": total_gws,
        "injury_risk": injury_risk,
        "rotation_risk": rotation_risk,
        "reliability_score": reliability_score,
    }


def _classify_archetype(
    explosiveness: dict, regression: dict, talisman: dict, reliability: dict
) -> tuple[str, list[str]]:
    """Rule-based mapping from metrics → archetype label + tags."""
    tags: list[str] = []

    # Explosive vs consistent
    haul_rate = explosiveness.get("haul_rate", 0)
    blank_rate = explosiveness.get("blank_rate", 0)
    std = explosiveness.get("std_points", 0)

    if haul_rate >= 0.25 and std >= 3.5:
        tags.append("explosive")
    elif blank_rate <= 0.2 and std <= 2.5:
        tags.append("consistent")
    elif haul_rate >= 0.15:
        tags.append("semi-explosive")
    else:
        tags.append("steady")

    # Talisman
    ti = talisman.get("talisman_index", 0)
    if ti >= 0.25:
        tags.append("talisman")
    elif ti >= 0.15:
        tags.append("key_player")

    # Regression
    flag = regression.get("regression_flag", "in_line")
    if flag == "overperforming":
        tags.append("overperforming")
    elif flag == "underperforming":
        tags.append("underperforming")

    # Reliability
    rel = reliability.get("reliability_score", 0)
    if rel >= 80:
        tags.append("nailed")
    elif rel < 50:
        tags.append("rotation_risk")

    nail = reliability.get("nailedness_score", 0)
    inj = reliability.get("injury_risk", "low")
    if inj in ("medium", "high"):
        tags.append("injury_concern")

    # Derive archetype label
    if "explosive" in tags and "talisman" in tags:
        archetype = "explosive talisman"
    elif "explosive" in tags:
        archetype = "explosive differential"
    elif "consistent" in tags and "nailed" in tags:
        archetype = "consistent grinder"
    elif "consistent" in tags:
        archetype = "safe pick"
    elif "overperforming" in tags:
        archetype = "overperforming finisher"
    elif "underperforming" in tags and "talisman" in tags:
        archetype = "underperforming talisman (buy-low)"
    elif "underperforming" in tags:
        archetype = "underperforming asset (buy-low candidate)"
    elif "talisman" in tags:
        archetype = "team talisman"
    elif "rotation_risk" in tags:
        archetype = "rotation risk"
    elif "semi-explosive" in tags:
        archetype = "boom-or-bust"
    else:
        archetype = "functional asset"

    return archetype, tags


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
#  GENERAL FPL TOOLS
# ======================================================================


@tool
def get_top_players_by_form(top_n: int = 10) -> str:
    """Return the top N players ranked by current form (points per match
    over the recent window).  Useful for picking differentials or
    captaincy candidates.
    """
    log.info("Tool called: get_top_players_by_form(top_n=%d)", top_n)
    players = _client.get_all_players()
    teams = _get_teams_map()

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
    log.info("Tool called: get_player_details(player_name=%r)", player_name)
    players = _client.get_all_players()
    teams = _get_teams_map()
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
    log.info("Tool called: get_current_gameweek_info()")
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
    log.info("Tool called: get_fixtures_for_gameweek(gameweek=%d)", gameweek)
    fixtures = _client.get_fixtures(gameweek=gameweek)
    teams = _get_teams_map()

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
    log.info("Tool called: get_best_value_players(position=%r, top_n=%d)", position, top_n)
    pos_id = {"GKP": 1, "DEF": 2, "MID": 3, "FWD": 4}.get(position.upper())
    if pos_id is None:
        return json.dumps({"error": f"Unknown position '{position}'. Use GKP/DEF/MID/FWD."})

    players = _client.get_all_players()
    teams = _get_teams_map()

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
#  MY TEAM / MANAGER TOOLS
# ======================================================================


@tool
def get_my_team(gameweek: int | None = None) -> str:
    """Return the user's current squad for a given gameweek (defaults to the
    current GW).  Shows each player with position, team, price, form, points,
    whether they are on the bench, and who is captain / vice-captain.
    Also includes the manager's overall rank, total points, bank, and squad value.
    """
    log.info("Tool called: get_my_team(gameweek=%s)", gameweek)
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
    teams = _get_teams_map()

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
    log.info("Tool called: get_my_season_history()")
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
    log.info("Tool called: get_my_transfers()")
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


# ======================================================================
#  PLANNING & TRANSFER TOOLS
# ======================================================================


def _next_fixture_short(player_id: int, teams: dict) -> str:
    """Return a short string like 'BOU(H)' for a player's next fixture."""
    try:
        summary = _client.get_player_summary(player_id)
        upcoming = summary.get("fixtures", [])
        if not upcoming:
            return "—"
        nxt = upcoming[0]
        is_home = nxt.get("is_home", False)
        opp_team_id = nxt.get("team_a") if is_home else nxt.get("team_h")
        opp = teams.get(opp_team_id, "?")
        return f"{opp}({'H' if is_home else 'A'})"
    except Exception:
        return "—"


@tool
def get_my_team_structured(gameweek: int | None = None) -> str:
    """Return the user's full squad (XI + bench) structured for pitch rendering.
    Includes position grouping, captain/vice badges, price, form, next fixture.
    Also includes manager metadata: bank, squad value, free transfers (if available).
    """
    log.info("Tool called: get_my_team_structured(gameweek=%s)", gameweek)
    team_id = _get_team_id()
    if team_id is None:
        return json.dumps({"error": "FPL_TEAM_ID is not set. Link your team first."})

    gw = gameweek or _current_gameweek()
    if gw is None:
        return json.dumps({"error": "Could not determine the current gameweek. FPL may be off-season."})

    try:
        picks_data = _client.get_team_picks(team_id, gw)
    except Exception as e:
        return json.dumps({"error": f"Could not fetch picks for GW{gw}: {e}"})

    team_info = _client.get_team_info(team_id)
    all_players = {p["id"]: p for p in _client.get_all_players()}
    teams = _get_teams_map()

    picks = picks_data.get("picks", [])
    entry_history = picks_data.get("entry_history", {})
    active_chip = picks_data.get("active_chip")

    starters = []
    bench = []

    for pick in picks:
        player = all_players.get(pick["element"], {})
        pos = _POSITION_MAP.get(player.get("element_type"), "?")
        nxt_fix = _next_fixture_short(pick["element"], teams)
        rec = {
            "element_id": pick["element"],
            "name": player.get("web_name", "?"),
            "position": pos,
            "team": teams.get(player.get("team"), "?"),
            "price": player.get("now_cost", 0) / 10,
            "form": player.get("form", "0.0"),
            "total_points": player.get("total_points", 0),
            "is_captain": pick.get("is_captain", False),
            "is_vice_captain": pick.get("is_vice_captain", False),
            "multiplier": pick.get("multiplier", 1),
            "pick_position": pick.get("position", 0),
            "next_fixture": nxt_fix,
        }
        if pick.get("position", 0) <= 11:
            starters.append(rec)
        else:
            bench.append(rec)

    return json.dumps({
        "gameweek": gw,
        "manager": f"{team_info.get('player_first_name', '')} {team_info.get('player_last_name', '')}".strip(),
        "team_name": team_info.get("name", "?"),
        "overall_points": team_info.get("summary_overall_points"),
        "overall_rank": team_info.get("summary_overall_rank"),
        "bank": entry_history.get("bank", 0) / 10,
        "squad_value": entry_history.get("value", 0) / 10,
        "free_transfers": entry_history.get("event_transfers", None),
        "active_chip": active_chip,
        "starters": starters,
        "bench": bench,
    }, indent=2)


@tool
def get_dream_team_full15(gameweek: int) -> str:
    """Compute the best possible 15-man squad for a given gameweek using
    live points data.  Enforces FPL constraints:
    • Squad: 2 GKP, 5 DEF, 5 MID, 3 FWD
    • Max 3 players per real-life club
    • Best XI from the 15 with legal formation (1 GKP, ≥3 DEF, ≥2 MID, ≥1 FWD, 11 total)
    • Captain = highest scorer in XI
    Returns JSON with starters, bench, captain, total_points.
    """
    log.info("Tool called: get_dream_team_full15(gameweek=%d)", gameweek)

    try:
        live_data = _client.get_live_gameweek(gameweek)
    except Exception as e:
        return json.dumps({"error": f"Could not fetch live data for GW{gameweek}: {e}"})

    all_players = {p["id"]: p for p in _client.get_all_players()}
    teams = _get_teams_map()
    live_elements = live_data.get("elements", [])

    # Build player list with live points
    player_pool: list[dict] = []
    for el in live_elements:
        pid = el["id"]
        player = all_players.get(pid)
        if player is None:
            continue
        stats = el.get("stats", {})
        pts = stats.get("total_points", 0)
        if player.get("minutes", 0) == 0 and pts == 0:
            continue  # skip players with no involvement
        pos_id = player.get("element_type", 0)
        player_pool.append({
            "id": pid,
            "name": player.get("web_name", "?"),
            "position": _POSITION_MAP.get(pos_id, "?"),
            "pos_id": pos_id,
            "team_id": player.get("team", 0),
            "team": teams.get(player.get("team"), "?"),
            "price": player.get("now_cost", 0) / 10,
            "points": pts,
        })

    # Sort by points descending
    player_pool.sort(key=lambda p: p["points"], reverse=True)

    # Greedy squad selection: 2 GKP, 5 DEF, 5 MID, 3 FWD, max 3/club
    squad_limits = {1: 2, 2: 5, 3: 5, 4: 3}
    squad_counts = {1: 0, 2: 0, 3: 0, 4: 0}
    club_counts: dict[int, int] = defaultdict(int)
    squad: list[dict] = []

    for p in player_pool:
        pos = p["pos_id"]
        tid = p["team_id"]
        if squad_counts.get(pos, 0) >= squad_limits.get(pos, 0):
            continue
        if club_counts[tid] >= 3:
            continue
        squad.append(p)
        squad_counts[pos] += 1
        club_counts[tid] += 1
        if len(squad) == 15:
            break

    if len(squad) < 15:
        return json.dumps({"error": "Not enough eligible players to form a full 15-man squad.",
                           "squad_size": len(squad)})

    # Select best XI from 15 with legal formation
    # At least 1 GKP, 3 DEF, 2 MID, 1 FWD, max 1 GKP, max 5 DEF/MID/FWD
    best_xi = _select_best_xi(squad)
    bench_ids = {p["id"] for p in squad} - {p["id"] for p in best_xi}
    bench = [p for p in squad if p["id"] in bench_ids]
    bench.sort(key=lambda p: (p["pos_id"] == 1, -p["points"]))  # GKP last on bench

    # Captain = highest scorer in XI
    captain = max(best_xi, key=lambda p: p["points"])
    total_xi_pts = sum(p["points"] for p in best_xi) + captain["points"]  # captain doubled

    # Format output
    def _fmt(p: dict, is_cap: bool = False, is_vc: bool = False) -> dict:
        return {
            "name": p["name"],
            "position": p["position"],
            "team": p["team"],
            "price": p["price"],
            "points": p["points"],
            "is_captain": is_cap,
            "is_vice_captain": is_vc,
        }

    # Vice captain = second highest scorer
    xi_sorted = sorted(best_xi, key=lambda p: p["points"], reverse=True)
    vc = xi_sorted[1] if len(xi_sorted) > 1 else xi_sorted[0]

    starters_out = []
    for p in best_xi:
        starters_out.append(_fmt(p, p["id"] == captain["id"], p["id"] == vc["id"]))

    bench_out = [_fmt(p) for p in bench]

    return json.dumps({
        "gameweek": gameweek,
        "starters": starters_out,
        "bench": bench_out,
        "captain": captain["name"],
        "vice_captain": vc["name"],
        "total_points": total_xi_pts,
        "bench_points": sum(p["points"] for p in bench),
    }, indent=2)


def _select_best_xi(squad: list[dict]) -> list[dict]:
    """Select the best 11 from a 15-man squad obeying formation rules.
    Legal: 1 GKP, 3-5 DEF, 2-5 MID, 1-3 FWD, total=11.
    Strategy: pick 1 GKP (highest pts), then greedily fill outfield
    respecting min/max per position.
    """
    by_pos: dict[int, list[dict]] = defaultdict(list)
    for p in squad:
        by_pos[p["pos_id"]].append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda p: p["points"], reverse=True)

    xi: list[dict] = []

    # 1 GKP (best)
    if by_pos[1]:
        xi.append(by_pos[1][0])

    # Minimums: 3 DEF, 2 MID, 1 FWD
    mins = {2: 3, 3: 2, 4: 1}
    remaining_pool: list[dict] = []

    for pos_id, min_count in mins.items():
        available = by_pos.get(pos_id, [])
        xi.extend(available[:min_count])
        remaining_pool.extend(available[min_count:])

    # Fill remaining 4 slots from remaining outfield, respecting max
    maxes = {2: 5, 3: 5, 4: 3}
    current_counts = defaultdict(int)
    for p in xi:
        current_counts[p["pos_id"]] += 1

    remaining_pool.sort(key=lambda p: p["points"], reverse=True)
    for p in remaining_pool:
        if len(xi) >= 11:
            break
        if current_counts[p["pos_id"]] < maxes.get(p["pos_id"], 5):
            xi.append(p)
            current_counts[p["pos_id"]] += 1

    return xi


@tool
def recommend_transfers(horizon: str = "1gw", risk: int = 50) -> str:
    """Recommend transfer plans for the user's team.

    Args:
        horizon: '1gw' for next gameweek focus, '5gw' for next 3-5 gameweeks.
        risk: 0-100 scale. 0 = conservative (free transfers only),
              100 = aggressive (allow hits, chase differentials).

    Returns JSON list of up to 3 transfer plans, each with:
    - transfers (in/out pairs with prices)
    - hit cost
    - rationale & expected upside score
    - confidence note + risk warning
    - data sources used
    """
    log.info("Tool called: recommend_transfers(horizon=%r, risk=%d)", horizon, risk)
    team_id = _get_team_id()
    if team_id is None:
        return json.dumps({"error": "FPL_TEAM_ID is not set. Link your team first."})

    gw = _current_gameweek()
    if gw is None:
        return json.dumps({"error": "Could not determine current GW. FPL may be off-season."})

    # Fetch current squad
    try:
        picks_data = _client.get_team_picks(team_id, gw)
    except Exception as e:
        return json.dumps({"error": f"Could not fetch team picks: {e}"})

    entry_history = picks_data.get("entry_history", {})
    bank = entry_history.get("bank", 0) / 10
    picks = picks_data.get("picks", [])

    all_players = {p["id"]: p for p in _client.get_all_players()}
    teams = _get_teams_map()

    # Build current squad info
    squad_ids = set()
    squad_info: list[dict] = []
    squad_club_counts: dict[int, int] = defaultdict(int)
    for pick in picks:
        pid = pick["element"]
        squad_ids.add(pid)
        player = all_players.get(pid, {})
        pos_id = player.get("element_type", 0)
        club_id = player.get("team", 0)
        squad_club_counts[club_id] += 1
        squad_info.append({
            "id": pid,
            "name": player.get("web_name", "?"),
            "position": _POSITION_MAP.get(pos_id, "?"),
            "pos_id": pos_id,
            "team_id": club_id,
            "team": teams.get(club_id, "?"),
            "price": player.get("now_cost", 0) / 10,
            "sell_price": player.get("now_cost", 0) / 10,  # approximate
            "form": float(player.get("form", 0)),
            "total_points": player.get("total_points", 0),
            "minutes": player.get("minutes", 0),
            "selected_by": float(player.get("selected_by_percent", 0)),
            "news": player.get("news", ""),
            "chance_playing": player.get("chance_of_playing_next_round"),
        })

    # Determine fixture window
    gw_start = gw + 1 if gw else 1
    gw_end = gw_start if horizon == "1gw" else min(gw_start + 4, 38)

    # Get fixture difficulty for all teams
    try:
        fix_map = _client.get_fixture_difficulty_map(gw_start, gw_end)
    except Exception:
        fix_map = {}

    def _fixture_str(team_id: int) -> str:
        fixtures = fix_map.get(team_id, [])
        if not fixtures:
            return "—"
        parts = []
        for f in fixtures[:5]:
            ha = "H" if f["is_home"] else "A"
            parts.append(f"{f['opponent']}({ha})")
        return ", ".join(parts)

    # Score OUT candidates (lower = more sell-worthy)
    # Factors: poor form, tough fixtures, injury doubts, low minutes
    out_candidates = []
    for p in squad_info:
        score = 0.0
        # Form (lower is worse) — weight heavily
        score += float(p["form"]) * 2
        # Fixture ease (lower avg difficulty = easier = better to keep)
        avg_diff = _avg_fixture_difficulty(fix_map, p["team_id"])
        score += (5 - avg_diff) * 1.5
        # Minutes risk
        if p["minutes"] < 200:
            score -= 2
        # Injury flag
        if p.get("chance_playing") is not None and p["chance_playing"] < 75:
            score -= 3
        if p.get("news"):
            score -= 1
        p["_keep_score"] = round(score, 2)
        out_candidates.append(p)

    out_candidates.sort(key=lambda p: p["_keep_score"])

    # Score IN candidates — top performers not in squad
    in_candidates_by_pos: dict[int, list] = defaultdict(list)
    for pid, player in all_players.items():
        if pid in squad_ids:
            continue
        if player.get("minutes", 0) < 90:
            continue  # skip zero-minute players
        pos_id = player.get("element_type", 0)
        club_id = player.get("team", 0)
        form = float(player.get("form", 0))
        price = player.get("now_cost", 0) / 10
        avg_diff = _avg_fixture_difficulty(fix_map, club_id)
        # Value score: form * fixture_ease * points_per_cost
        ppc = player.get("total_points", 0) / price if price > 0 else 0
        fixture_ease = (5 - avg_diff)
        in_score = form * 2 + fixture_ease * 1.5 + ppc * 0.5

        in_candidates_by_pos[pos_id].append({
            "id": pid,
            "name": player.get("web_name", "?"),
            "position": _POSITION_MAP.get(pos_id, "?"),
            "pos_id": pos_id,
            "team_id": club_id,
            "team": teams.get(club_id, "?"),
            "price": price,
            "form": form,
            "total_points": player.get("total_points", 0),
            "fixtures": _fixture_str(club_id),
            "selected_by": player.get("selected_by_percent", "0"),
            "_in_score": round(in_score, 2),
        })

    for pos in in_candidates_by_pos:
        in_candidates_by_pos[pos].sort(key=lambda p: p["_in_score"], reverse=True)

    # Generate transfer plans
    plans = []
    allow_hits = risk >= 50
    max_transfers = 1 if risk < 30 else (2 if risk < 70 else 3)

    for n_transfers in range(1, max_transfers + 1):
        hit_cost = max(0, (n_transfers - 1) * 4)  # 1 FT free, each extra = -4
        if hit_cost > 0 and not allow_hits:
            continue

        transfers = []
        budget_freed = 0.0
        used_in_ids: set[int] = set()
        plan_club_counts = dict(squad_club_counts)

        for i in range(n_transfers):
            if i >= len(out_candidates):
                break
            out_p = out_candidates[i]
            budget_avail = bank + out_p["price"] + budget_freed

            # Find best replacement at same position
            pos = out_p["pos_id"]
            best_in = None
            for candidate in in_candidates_by_pos.get(pos, []):
                if candidate["id"] in used_in_ids:
                    continue
                if candidate["price"] > budget_avail:
                    continue
                # Club limit: check if bringing this player in would exceed 3
                new_club_count = plan_club_counts.get(candidate["team_id"], 0)
                # Subtract 1 if out_p is from same club
                if out_p["team_id"] == candidate["team_id"]:
                    pass  # net zero change
                else:
                    if new_club_count >= 3:
                        continue
                best_in = candidate
                break

            if best_in is None:
                continue

            transfers.append({
                "out": out_p["name"],
                "out_team": out_p["team"],
                "out_price": out_p["price"],
                "out_form": out_p["form"],
                "in": best_in["name"],
                "in_team": best_in["team"],
                "in_price": best_in["price"],
                "in_form": best_in["form"],
                "in_fixtures": best_in["fixtures"],
            })
            used_in_ids.add(best_in["id"])
            budget_freed += out_p["price"] - best_in["price"]
            # Update club counts
            plan_club_counts[out_p["team_id"]] = plan_club_counts.get(out_p["team_id"], 0) - 1
            plan_club_counts[best_in["team_id"]] = plan_club_counts.get(best_in["team_id"], 0) + 1

        if not transfers:
            continue

        # Expected upside heuristic
        total_form_gain = sum(
            t["in_form"] - t["out_form"] for t in transfers
        )
        upside_score = round(max(0, total_form_gain * 3 - hit_cost), 1)

        confidence = "Medium" if n_transfers == 1 else ("Low-Medium" if hit_cost > 0 else "Medium")
        if horizon == "5gw":
            confidence += " (fixtures-weighted)"

        risk_warning = "Hits can backfire if new players blank." if hit_cost > 0 else \
                       "Single free transfer — low risk."

        plans.append({
            "plan_name": f"Plan {len(plans)+1}: {n_transfers} transfer{'s' if n_transfers > 1 else ''}",
            "transfers": transfers,
            "hit_cost": hit_cost,
            "expected_upside_score": upside_score,
            "net_cost": round(sum(t["in_price"] - t["out_price"] for t in transfers), 1),
            "remaining_bank": round(bank + budget_freed, 1),
            "rationale": f"{'Short-term' if horizon == '1gw' else 'Medium-term'} move targeting "
                         f"form + fixture advantage. "
                         f"Total form gain: {total_form_gain:+.1f}.",
            "data_used": f"Form, fixture difficulty (GW{gw_start}–{gw_end}), price, minutes",
            "confidence": confidence,
            "risk_warning": risk_warning,
        })

    if not plans:
        plans.append({
            "plan_name": "No transfers recommended",
            "rationale": "Your squad looks strong for the selected horizon. Roll the transfer.",
            "confidence": "High",
            "risk_warning": "None",
            "data_used": f"Form, fixture difficulty (GW{gw_start}–{gw_end})",
        })

    return json.dumps(plans, indent=2)


# ======================================================================
#  BEHAVIOUR & RISK TOOLS
# ======================================================================


@tool
def classify_player_archetype(player_name: str) -> str:
    """Classify a player's behavioural archetype (e.g. explosive talisman,
    consistent grinder, overperforming finisher) using underlying FPL stats.

    Returns archetype label, descriptive tags, and the full metrics
    (explosiveness, regression, talisman index, reliability).
    """
    log.info("Tool called: classify_player_archetype(player_name=%r)", player_name)
    teams = _get_teams_map()

    player = _resolve_player(player_name)
    if player is None:
        return json.dumps({"error": f"No player found matching '{player_name}'"})

    pid = player["id"]
    gw_stats = _compute_player_gw_stats(pid)
    explosiveness = _compute_explosiveness_and_consistency(gw_stats)
    regression = _compute_regression_metrics(gw_stats, player)
    talisman = _compute_talisman_index(pid)
    reliability = _compute_reliability_score(pid)
    archetype, tags = _classify_archetype(explosiveness, regression, talisman, reliability)

    # Build human-readable summary
    summary_parts = [f"{player['web_name']} is classified as **{archetype}**."]
    if "explosive" in tags:
        summary_parts.append(
            f"Haul rate {explosiveness['haul_rate']:.0%} with high variance (σ={explosiveness['std_points']:.1f})."
        )
    if "consistent" in tags:
        summary_parts.append(
            f"Averages {explosiveness['avg_points']:.1f} pts/GW with low variance (σ={explosiveness['std_points']:.1f})."
        )
    if "talisman" in tags:
        summary_parts.append(
            f"Talisman index {talisman['talisman_index']:.2f} — central to the team's attack."
        )
    if regression["regression_flag"] != "in_line":
        summary_parts.append(
            f"Currently {regression['regression_flag']} vs xG/xA "
            f"(ΔG={regression['delta_goals_vs_xg']:+.1f}, ΔA={regression['delta_assists_vs_xa']:+.1f})."
        )
    summary_parts.append(f"Reliability score: {reliability['reliability_score']}/100.")

    return json.dumps(
        {
            "player": {
                "id": pid,
                "name": player["web_name"],
                "team": teams.get(player["team"], "?"),
                "position": _POSITION_MAP.get(player["element_type"], "?"),
            },
            "archetype": archetype,
            "tags": tags,
            "metrics": {
                "explosiveness": explosiveness,
                "regression": regression,
                "talisman": talisman,
                "reliability": reliability,
            },
            "summary": " ".join(summary_parts),
        },
        indent=2,
    )


@tool
def get_player_volatility_profile(
    player_name: str, window_gws: int = 8
) -> str:
    """Return a player's recent-window volatility profile: per-GW points,
    average, standard deviation, haul/blank rates, and a 0-100 volatility score.
    """
    log.info(
        "Tool called: get_player_volatility_profile(player_name=%r, window=%d)",
        player_name, window_gws,
    )
    teams = _get_teams_map()

    player = _resolve_player(player_name)
    if player is None:
        return json.dumps({"error": f"No player found matching '{player_name}'"})

    pid = player["id"]
    gw_stats = _compute_player_gw_stats(pid)
    played = [h for h in gw_stats if h.get("minutes", 0) > 0]
    recent = played[-window_gws:] if len(played) >= window_gws else played

    pts_list = [h["total_points"] for h in recent]
    expl = _compute_explosiveness_and_consistency(gw_stats, window=window_gws)

    # Volatility score 0-100  (higher = more volatile)
    # Normalise std relative to avg — coefficient of variation style
    avg = expl["avg_points"] if expl["avg_points"] > 0 else 1
    cv = expl["std_points"] / avg
    volatility_score = min(100, round(cv * 50 + expl["haul_rate"] * 30 + expl["blank_rate"] * 20))

    return json.dumps(
        {
            "player": {
                "id": pid,
                "name": player["web_name"],
                "team": teams.get(player["team"], "?"),
                "position": _POSITION_MAP.get(player["element_type"], "?"),
            },
            "window_gws": window_gws,
            "points_per_gw": pts_list,
            "stats": {
                "avg_points": expl["avg_points"],
                "std_points": expl["std_points"],
                "max_points": expl["max_points"],
                "haul_rate": expl["haul_rate"],
                "blank_rate": expl["blank_rate"],
                "volatility_score": volatility_score,
            },
        },
        indent=2,
    )


@tool
def find_talisman_players(limit: int = 15, min_minutes: int = 900) -> str:
    """Return the most *talismanic* players across the league — those who
    account for the largest share of their club's attacking output.

    Filtered by a minimum minutes threshold to exclude bit-part players.
    """
    log.info(
        "Tool called: find_talisman_players(limit=%d, min_minutes=%d)",
        limit, min_minutes,
    )
    all_players = _client.get_all_players()
    teams = _get_teams_map()

    # Pre-compute per-club totals once for efficiency
    club_totals: dict[int, dict] = defaultdict(lambda: {"goals": 0, "xgi": 0.0})
    for p in all_players:
        cid = p["team"]
        club_totals[cid]["goals"] += int(p.get("goals_scored", 0)) + int(p.get("assists", 0))
        club_totals[cid]["xgi"] += float(p.get("expected_goal_involvements", 0) or 0)

    candidates = []
    for p in all_players:
        if p.get("minutes", 0) < min_minutes:
            continue
        cid = p["team"]
        ct = club_totals[cid]
        player_gi = int(p.get("goals_scored", 0)) + int(p.get("assists", 0))
        player_xgi = float(p.get("expected_goal_involvements", 0) or 0)
        gi_share = player_gi / ct["goals"] if ct["goals"] > 0 else 0
        xgi_share = player_xgi / ct["xgi"] if ct["xgi"] > 0 else 0
        ti = round((gi_share + xgi_share) / 2, 3)

        notes = ""
        if gi_share > xgi_share + 0.05:
            notes = "outperforming xGI share — slight regression risk"
        elif xgi_share > gi_share + 0.05:
            notes = "underlying xGI share higher — upside potential"

        candidates.append({
            "name": p["web_name"],
            "team": teams.get(cid, "?"),
            "position": _POSITION_MAP.get(p["element_type"], "?"),
            "talisman_index": ti,
            "attacking_share": round(gi_share, 3),
            "xgi_share": round(xgi_share, 3),
            "goals": p.get("goals_scored", 0),
            "assists": p.get("assists", 0),
            "minutes": p.get("minutes", 0),
            "notes": notes,
        })

    candidates.sort(key=lambda c: c["talisman_index"], reverse=True)
    return json.dumps(candidates[:limit], indent=2)


@tool
def analyze_squad_risk_profile(gameweek: int | None = None) -> str:
    """Analyse your current 15-man squad for behavioural risk:
    volatility, talisman concentration, reliability, club exposure.

    Returns per-player tags and a squad-level summary with actionable notes.
    """
    log.info("Tool called: analyze_squad_risk_profile(gameweek=%s)", gameweek)
    team_id = _get_team_id()
    if team_id is None:
        return json.dumps({"error": "FPL_TEAM_ID is not set. Link your team first."})

    gw = gameweek or _current_gameweek()
    if gw is None:
        return json.dumps({"error": "Could not determine the current gameweek."})

    try:
        picks_data = _client.get_team_picks(team_id, gw)
    except Exception as e:
        return json.dumps({"error": f"Could not fetch team picks: {e}"})

    all_players = {p["id"]: p for p in _client.get_all_players()}
    teams = _get_teams_map()

    picks = picks_data.get("picks", [])

    per_player = []
    club_counter: dict[str, int] = defaultdict(int)
    explosive_count = 0
    consistent_count = 0
    high_risk_players = []

    for pick in picks:
        pid = pick["element"]
        player = all_players.get(pid, {})
        club_short = teams.get(player.get("team"), "?")
        club_counter[club_short] += 1

        gw_stats = _compute_player_gw_stats(pid)
        expl = _compute_explosiveness_and_consistency(gw_stats)
        talisman = _compute_talisman_index(pid)
        reliability = _compute_reliability_score(pid)
        regression = _compute_regression_metrics(gw_stats, player)
        archetype, tags = _classify_archetype(expl, regression, talisman, reliability)

        if "explosive" in tags or "semi-explosive" in tags:
            explosive_count += 1
        if "consistent" in tags or "steady" in tags:
            consistent_count += 1
        if reliability["reliability_score"] < 50 or reliability["injury_risk"] == "high":
            high_risk_players.append(player.get("web_name", "?"))

        per_player.append({
            "name": player.get("web_name", "?"),
            "position": _POSITION_MAP.get(player.get("element_type"), "?"),
            "team": club_short,
            "archetype": archetype,
            "tags": tags,
            "reliability_score": reliability["reliability_score"],
            "talisman_index": talisman.get("talisman_index", 0),
            "avg_points": expl["avg_points"],
            "regression_flag": regression["regression_flag"],
            "on_bench": pick.get("position", 0) > 11,
        })

    # Club concentration
    club_concentration = sorted(
        [{"team": t, "count": c} for t, c in club_counter.items()],
        key=lambda x: x["count"],
        reverse=True,
    )

    # Portfolio notes
    notes_parts: list[str] = []
    if explosive_count >= 8:
        notes_parts.append("Very aggressive squad — high ceiling but volatile week-to-week.")
    elif consistent_count >= 8:
        notes_parts.append("Very conservative squad — steady floor but limited ceiling.")
    else:
        notes_parts.append(f"Balanced mix: {explosive_count} explosive, {consistent_count} consistent picks.")

    heavy_clubs = [c for c in club_concentration if c["count"] >= 3]
    if heavy_clubs:
        clubs_str = ", ".join(f"{c['team']}({c['count']})" for c in heavy_clubs)
        notes_parts.append(f"Heavy exposure to: {clubs_str}. A bad GW for those clubs hurts.")

    if high_risk_players:
        notes_parts.append(
            f"High-risk players: {', '.join(high_risk_players)}. "
            "Consider bench cover or replacements."
        )

    overperformers = [p["name"] for p in per_player if p["regression_flag"] == "overperforming"]
    if overperformers:
        notes_parts.append(
            f"Regression watch: {', '.join(overperformers)} overperforming vs xG/xA."
        )

    return json.dumps(
        {
            "gameweek": gw,
            "per_player": per_player,
            "summary": {
                "explosive_count": explosive_count,
                "consistent_count": consistent_count,
                "high_risk_players": high_risk_players,
                "club_concentration": club_concentration,
                "portfolio_notes": " ".join(notes_parts),
            },
        },
        indent=2,
    )


# ======================================================================
#  DRAFT BUILDER TOOLS
# ======================================================================


def _build_player_behaviour(player: dict, pid: int) -> dict:
    """Compute behaviour metrics for a single player element dict.
    Returns a compact dict with archetype, tags, and key scores."""
    gw_stats = _compute_player_gw_stats(pid)
    explosiveness = _compute_explosiveness_and_consistency(gw_stats)
    regression = _compute_regression_metrics(gw_stats, player)
    talisman = _compute_talisman_index(pid)
    reliability = _compute_reliability_score(pid)
    archetype, tags = _classify_archetype(explosiveness, regression, talisman, reliability)
    return {
        "archetype": archetype,
        "tags": tags,
        "volatility_std": explosiveness.get("std_points", 0),
        "haul_rate": explosiveness.get("haul_rate", 0),
        "blank_rate": explosiveness.get("blank_rate", 0),
        "regression_flag": regression.get("regression_flag", "in_line"),
        "reliability_score": reliability.get("reliability_score", 0),
        "talisman_index": talisman.get("talisman_index", 0),
    }


@tool
def suggest_replacements_for_player(
    player_name: str,
    max_price: float,
    horizon_gws: int = 3,
    risk_level: str = "medium",
    limit: int = 10,
) -> str:
    """Suggest replacement options for a given player within a max price.

    Args:
        player_name: Name of the current player in the user's squad (fuzzy match ok).
        max_price: Maximum allowed price in £m for replacement.
        horizon_gws: How many upcoming gameweeks to consider (1–5 typical).
        risk_level: "low", "medium", or "high" to bias towards safer vs explosive picks.
        limit: Max number of candidate replacements to return.

    Returns JSON with out_player info and ranked candidate replacements
    including stats, fixtures, behaviour tags, scores, and deltas.
    """
    log.info(
        "Tool called: suggest_replacements_for_player(player=%r, max_price=%.1f, "
        "horizon=%d, risk=%s, limit=%d)",
        player_name, max_price, horizon_gws, risk_level, limit,
    )

    # ── Resolve the outgoing player ──────────────────────────────────
    player = _resolve_player(player_name)
    if player is None:
        return json.dumps({"error": f"No player found matching '{player_name}'"})

    pid = player["id"]
    pos_id = player["element_type"]
    all_players = _client.get_all_players()
    teams = _get_teams_map()

    # Current squad IDs (if team is linked)
    squad_ids: set[int] = set()
    team_id = _get_team_id()
    gw = _current_gameweek()
    if team_id and gw:
        try:
            picks_data = _client.get_team_picks(team_id, gw)
            squad_ids = {p["element"] for p in picks_data.get("picks", [])}
        except Exception:
            pass

    # ── Fixture difficulty ───────────────────────────────────────────
    gw_start = (gw + 1) if gw else 1
    gw_end = min(gw_start + horizon_gws - 1, 38)
    try:
        fix_map = _client.get_fixture_difficulty_map(gw_start, gw_end)
    except Exception:
        fix_map = {}

    def _fixture_summary(tid: int) -> list[dict]:
        fixes = fix_map.get(tid, [])
        return [
            {"gw": f["gw"], "opponent": f["opponent"],
             "difficulty": f["difficulty"], "is_home": f["is_home"]}
            for f in fixes[:horizon_gws]
        ]

    # ── Out-player metadata + behaviour ──────────────────────────────
    out_price = player["now_cost"] / 10
    out_form = float(player.get("form", 0))
    out_fix_avg = _avg_fixture_difficulty(fix_map, player["team"])
    out_behaviour = _build_player_behaviour(player, pid)

    out_player = {
        "id": pid,
        "name": player["web_name"],
        "team": teams.get(player["team"], "?"),
        "position": _POSITION_MAP.get(pos_id, "?"),
        "price": out_price,
        "form": out_form,
        "total_points": player.get("total_points", 0),
        "minutes": player.get("minutes", 0),
        "xG": player.get("expected_goals"),
        "xA": player.get("expected_assists"),
        "fixtures": _fixture_summary(player["team"]),
        "fixture_avg_difficulty": round(out_fix_avg, 2),
        "behaviour": out_behaviour,
    }

    # ── Scoring weights by risk_level ────────────────────────────────
    #    low  → favour safety; high → favour upside
    weights = {
        "low":    {"form": 1.5, "fixture": 2.0, "reliability": 3.0, "upside": 0.5},
        "medium": {"form": 2.0, "fixture": 1.5, "reliability": 1.5, "upside": 1.5},
        "high":   {"form": 2.5, "fixture": 1.0, "reliability": 0.5, "upside": 3.0},
    }.get(risk_level, {"form": 2.0, "fixture": 1.5, "reliability": 1.5, "upside": 1.5})

    # ── Scan candidates ──────────────────────────────────────────────
    raw_candidates: list[dict] = []

    for p in all_players:
        # Same position, not already in squad, affordable, has minutes
        if p["element_type"] != pos_id:
            continue
        if p["id"] == pid:
            continue
        if p["id"] in squad_ids:
            continue
        c_price = p["now_cost"] / 10
        if c_price > max_price:
            continue
        if p.get("minutes", 0) < 90:
            continue

        c_form = float(p.get("form", 0))
        c_tid = p["team"]
        c_fix_avg = _avg_fixture_difficulty(fix_map, c_tid)
        fixture_ease = max(0, 5 - c_fix_avg)  # 0-5 scale

        # Points-per-cost efficiency
        ppc = p.get("total_points", 0) / c_price if c_price > 0 else 0

        # Quick reliability proxy (chance_of_playing + minutes ratio)
        cop = p.get("chance_of_playing_next_round")
        avail_penalty = 0
        if cop is not None and cop < 75:
            avail_penalty = 2

        # Scores
        safety_score = (
            fixture_ease * weights["fixture"]
            + min(ppc, 10) * 0.3
            - avail_penalty
        )
        upside_score = (
            c_form * weights["form"]
            + fixture_ease * weights["fixture"] * 0.5
            + ppc * 0.5
        )
        overall_score = (
            c_form * weights["form"]
            + fixture_ease * weights["fixture"]
            + min(ppc, 10) * 0.5
            + (0 if avail_penalty else weights["reliability"])
            + (c_form * weights["upside"] * 0.3)
        )

        raw_candidates.append({
            "id": p["id"],
            "name": p["web_name"],
            "team": teams.get(c_tid, "?"),
            "team_id": c_tid,
            "position": _POSITION_MAP.get(pos_id, "?"),
            "price": c_price,
            "stats": {
                "form": c_form,
                "total_points": p.get("total_points", 0),
                "minutes": p.get("minutes", 0),
                "xG": p.get("expected_goals"),
                "xA": p.get("expected_assists"),
                "selected_by": p.get("selected_by_percent"),
            },
            "fixtures": _fixture_summary(c_tid),
            "fixture_avg_difficulty": round(c_fix_avg, 2),
            "scores": {
                "overall_score": round(overall_score, 2),
                "upside_score": round(upside_score, 2),
                "safety_score": round(safety_score, 2),
            },
            "deltas_vs_out": {
                "form_delta": round(c_form - out_form, 2),
                "fixture_difficulty_delta": round(out_fix_avg - c_fix_avg, 2),
                "price_delta": round(c_price - out_price, 1),
            },
            # Placeholder for behaviour — filled below for top candidates only
            "_overall": overall_score,
        })

    # Sort and take top N
    raw_candidates.sort(key=lambda c: c["_overall"], reverse=True)
    top = raw_candidates[:limit]

    # Enrich top candidates with full behaviour metrics (expensive)
    for c in top:
        c_player = next((p for p in all_players if p["id"] == c["id"]), None)
        if c_player:
            try:
                c["behaviour"] = _build_player_behaviour(c_player, c["id"])
            except Exception:
                c["behaviour"] = {"archetype": "unknown", "tags": []}
        else:
            c["behaviour"] = {"archetype": "unknown", "tags": []}
        del c["_overall"]
        del c["team_id"]

    return json.dumps({"out_player": out_player, "candidates": top}, indent=2)


# ======================================================================
#  ALL_TOOLS EXPORT — ordered by section
# ======================================================================
ALL_TOOLS = [
    # General FPL Tools
    get_top_players_by_form,
    get_player_details,
    get_current_gameweek_info,
    get_fixtures_for_gameweek,
    get_best_value_players,
    # My Team / Manager Tools
    get_my_team,
    get_my_season_history,
    get_my_transfers,
    get_my_team_structured,
    # Planning & Transfer Tools
    get_dream_team_full15,
    recommend_transfers,
    # Behaviour & Risk Tools
    classify_player_archetype,
    get_player_volatility_profile,
    find_talisman_players,
    analyze_squad_risk_profile,
    # Draft Builder Tools
    suggest_replacements_for_player,
]
