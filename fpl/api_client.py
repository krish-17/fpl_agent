"""
FPL API client — thin wrapper around the public Fantasy Premier League API.

Base URL : https://fantasy.premierleague.com/api
No auth is needed for read-only endpoints.

Key endpoints used
------------------
/bootstrap-static/          → all players, teams, gameweeks, element types
/element-summary/{player_id}/ → per-player history & fixtures
/fixtures/                   → all fixtures (optionally ?event={gw})
/event/{gw}/live/            → live gameweek scores
/entry/{team_id}/            → manager info (public, no auth)
/entry/{team_id}/history/    → season history
/entry/{team_id}/event/{gw}/picks/ → squad picks for a GW

Authentication (login)
----------------------
Uses the new PingOne DaVinci OAuth2 + PKCE flow via account.premierleague.com.
Credits: @Moose on FPLDev Discord / AIrsenal project.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import time
import uuid

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://fantasy.premierleague.com/api"

# ── Module-level bootstrap cache (avoids repeated heavy fetches) ─────
_bootstrap_cache: dict | None = None
_bootstrap_ts: float = 0.0
_BOOTSTRAP_TTL = 300  # seconds (5 min)

# --- OAuth / DaVinci login constants ---
_LOGIN_BASE = "https://account.premierleague.com"
_LOGIN_URLS = {
    "auth": f"{_LOGIN_BASE}/as/authorize",
    "start": f"{_LOGIN_BASE}/davinci/policy/262ce4b01d19dd9d385d26bddb4297b6/start",
    "login": f"{_LOGIN_BASE}/davinci/connections/{{}}/capabilities/customHTMLTemplate",
    "resume": f"{_LOGIN_BASE}/as/resume",
    "token": f"{_LOGIN_BASE}/as/token",
    "me": f"{BASE_URL}/me/",
}
_CLIENT_ID = "bfcbaf69-aade-4c1b-8f00-c1cb8a193030"
_STANDARD_CONNECTION_ID = "867ed4363b2bc21c860085ad2baa817d"


def _generate_code_verifier() -> str:
    return secrets.token_urlsafe(64)[:128]


def _generate_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


class FPLClient:
    """Lightweight synchronous client for the public FPL API."""

    def __init__(self, timeout: float = 30.0):
        log.debug("Initialising FPLClient (timeout=%.1fs)", timeout)
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"User-Agent": "FPL-Agent/0.1"},
        )

    # ------------------------------------------------------------------
    # Bootstrap (the "everything" endpoint) — cached at module level
    # ------------------------------------------------------------------
    def get_bootstrap(self) -> dict:
        """Return the full bootstrap-static payload (players, teams, events, …).
        Cached for _BOOTSTRAP_TTL seconds to avoid repeated heavy fetches."""
        global _bootstrap_cache, _bootstrap_ts
        now = time.time()
        if _bootstrap_cache is None or (now - _bootstrap_ts) > _BOOTSTRAP_TTL:
            _bootstrap_cache = self._get("/bootstrap-static/")
            _bootstrap_ts = now
            log.debug("Bootstrap cache refreshed")
        return _bootstrap_cache

    def get_all_players(self) -> list[dict]:
        """Return the 'elements' list — every registered player."""
        return self.get_bootstrap()["elements"]

    def get_all_teams(self) -> list[dict]:
        """Return the 'teams' list — every Premier League club."""
        return self.get_bootstrap()["teams"]

    def get_gameweeks(self) -> list[dict]:
        """Return the 'events' list — metadata for every gameweek."""
        return self.get_bootstrap()["events"]

    def get_element_types(self) -> list[dict]:
        """Return position types (GKP, DEF, MID, FWD)."""
        return self.get_bootstrap()["element_types"]

    def get_current_gameweek(self) -> int | None:
        """Return the current gameweek id, or None if off-season."""
        events = self.get_gameweeks()
        current = next((e for e in events if e.get("is_current")), None)
        if current:
            return current["id"]
        # fallback: last finished GW
        finished = [e for e in events if e.get("finished")]
        return finished[-1]["id"] if finished else None

    def get_next_gameweek(self) -> int | None:
        """Return the next gameweek id, or None."""
        events = self.get_gameweeks()
        nxt = next((e for e in events if e.get("is_next")), None)
        return nxt["id"] if nxt else None

    def get_fixture_difficulty_map(self, gw_start: int, gw_end: int) -> dict[int, list]:
        """Return {team_id: [{gw, opponent_short, difficulty, is_home}, ...]} for the
        given gameweek range (inclusive).  Useful for transfer planning."""
        teams = {t["id"]: t["short_name"] for t in self.get_all_teams()}
        fixtures = self.get_fixtures()  # all fixtures
        result: dict[int, list] = {}
        for f in fixtures:
            gw = f.get("event")
            if gw is None or gw < gw_start or gw > gw_end:
                continue
            for side, opp_side, home_flag in [
                ("team_h", "team_a", True),
                ("team_a", "team_h", False),
            ]:
                tid = f[side]
                diff_key = "team_h_difficulty" if home_flag else "team_a_difficulty"
                diff = f.get(diff_key, 3)
                result.setdefault(tid, []).append({
                    "gw": gw,
                    "opponent": teams.get(f[opp_side], "?"),
                    "difficulty": diff,
                    "is_home": home_flag,
                })
        return result

    # ------------------------------------------------------------------
    # Player detail
    # ------------------------------------------------------------------
    def get_player_summary(self, player_id: int) -> dict:
        """Per-player past-season history + remaining fixtures."""
        return self._get(f"/element-summary/{player_id}/")

    # ------------------------------------------------------------------
    # Fixtures
    # ------------------------------------------------------------------
    def get_fixtures(self, gameweek: int | None = None) -> list[dict]:
        """All fixtures, optionally filtered to a single gameweek."""
        url = "/fixtures/"
        if gameweek is not None:
            url += f"?event={gameweek}"
        return self._get(url)

    # ------------------------------------------------------------------
    # Live gameweek data
    # ------------------------------------------------------------------
    def get_live_gameweek(self, gameweek: int) -> dict:
        """Live points data for a specific gameweek."""
        return self._get(f"/event/{gameweek}/live/")

    # ------------------------------------------------------------------
    # User / Manager team  (all public — no auth needed, just a team ID)
    # ------------------------------------------------------------------
    def get_team_info(self, team_id: int) -> dict:
        """Basic info about a manager's team — name, overall rank, points, etc."""
        return self._get(f"/entry/{team_id}/")

    def get_team_history(self, team_id: int) -> dict:
        """Season history — GW-by-GW points, bank, value, transfers, plus past seasons."""
        return self._get(f"/entry/{team_id}/history/")

    def get_team_picks(self, team_id: int, gameweek: int) -> dict:
        """Squad picks for a specific gameweek — the 15 players, captain, vice-captain,
        formation, active chip, auto-subs, etc."""
        return self._get(f"/entry/{team_id}/event/{gameweek}/picks/")

    def get_team_transfers(self, team_id: int) -> list[dict]:
        """Full transfer history for a manager (every transfer they've ever made)."""
        return self._get(f"/entry/{team_id}/transfers/")

    # ------------------------------------------------------------------
    # Authentication — OAuth2 + PKCE via PingOne DaVinci
    # ------------------------------------------------------------------
    @staticmethod
    def login(email: str, password: str) -> dict:
        """Log in to FPL using the new PingOne DaVinci OAuth2 + PKCE flow.

        Returns the /api/me/ profile dict.  The ``player.entry`` field
        inside it is the Team ID.

        Raises ``ValueError`` on bad credentials and ``RuntimeError`` on
        unexpected failures.

        Uses ``curl_cffi`` instead of httpx/requests because the FPL
        auth endpoints use TLS fingerprint detection (DataDome) that
        blocks standard Python HTTP clients.
        """
        from curl_cffi import requests as cffi_requests

        log.info("FPL login starting for: %s", email)
        session = cffi_requests.Session(impersonate="chrome")

        # --- Step 0: Generate PKCE verifier + challenge ---
        code_verifier = _generate_code_verifier()
        code_challenge = _generate_code_challenge(code_verifier)
        initial_state = uuid.uuid4().hex
        log.debug("PKCE challenge generated")

        try:
            # --- Step 1: GET the authorization page ---
            params = {
                "client_id": _CLIENT_ID,
                "redirect_uri": "https://fantasy.premierleague.com/",
                "response_type": "code",
                "scope": "openid profile email offline_access",
                "state": initial_state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
            auth_resp = session.get(_LOGIN_URLS["auth"], params=params)
            html = auth_resp.text

            # Extract accessToken from the HTML
            m = re.search(r'"accessToken":"([^"]+)"', html)
            if not m:
                raise RuntimeError(
                    "Failed to extract access token from auth page. "
                    "FPL may be in off-season / maintenance."
                )
            access_token = m.group(1)
            log.debug("Step 1 ✓ — got access token from auth page")

            # Extract hidden state value
            m = re.search(
                r'<input[^>]+name="state"[^>]+value="([^"]+)"', html
            )
            if not m:
                raise RuntimeError("Failed to extract OAuth state from auth page.")
            new_state = m.group(1)

            # --- Step 2: Start DaVinci interaction ---
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
            resp = session.post(_LOGIN_URLS["start"], headers=headers)
            r_json = resp.json()
            interaction_id = r_json["interactionId"]
            response_id = r_json["id"]
            log.debug("Step 2 ✓ — DaVinci interaction started (id=%s)", interaction_id[:12])

            # --- Step 3a: Polling init ---
            login_url = _LOGIN_URLS["login"].format(_STANDARD_CONNECTION_ID)
            resp = session.post(
                login_url,
                headers={"interactionId": interaction_id},
                json={
                    "id": response_id,
                    "eventName": "continue",
                    "parameters": {"eventType": "polling"},
                    "pollProps": {
                        "status": "continue",
                        "delayInMs": 10,
                        "retriesAllowed": 1,
                        "pollChallengeStatus": False,
                    },
                },
            )
            response_id = resp.json()["id"]
            log.debug("Step 3a ✓ — polling init")

            # --- Step 3b: Submit email + password ---
            resp = session.post(
                login_url,
                headers={"interactionId": interaction_id},
                json={
                    "id": response_id,
                    "nextEvent": {
                        "constructType": "skEvent",
                        "eventName": "continue",
                        "params": [],
                        "eventType": "post",
                        "postProcess": {},
                    },
                    "parameters": {
                        "buttonType": "form-submit",
                        "buttonValue": "SIGNON",
                        "username": email,
                        "password": password,
                    },
                    "eventName": "continue",
                },
            )
            r_json = resp.json()

            # Check for login errors (bad credentials)
            if "error" in r_json or "errorMessage" in r_json:
                err_msg = r_json.get("errorMessage") or r_json.get("error", "")
                log.warning("Step 3b ✗ — bad credentials for %s: %s", email, err_msg)
                raise ValueError(
                    f"Login failed: {err_msg or 'invalid email or password'}"
                )

            log.debug("Step 3b ✓ — credentials accepted")
            response_id = r_json["id"]
            connection_id = r_json.get("connectionId", _STANDARD_CONNECTION_ID)

            # --- Step 3c: Finalize DaVinci interaction ---
            resp = session.post(
                _LOGIN_URLS["login"].format(connection_id),
                headers={"interactionId": interaction_id},
                json={
                    "id": response_id,
                    "nextEvent": {
                        "constructType": "skEvent",
                        "eventName": "continue",
                        "params": [],
                        "eventType": "post",
                        "postProcess": {},
                    },
                    "parameters": {
                        "buttonType": "form-submit",
                        "buttonValue": "SIGNON",
                    },
                    "eventName": "continue",
                },
            )
            dv_response = resp.json()["dvResponse"]

            # --- Step 4: Resume OAuth flow → get auth code ---
            resp = session.post(
                _LOGIN_URLS["resume"],
                data={"dvResponse": dv_response, "state": new_state},
                allow_redirects=False,
            )
            location = resp.headers.get("Location", "")
            m = re.search(r"[?&]code=([^&]+)", location)
            if not m:
                raise RuntimeError("Failed to extract authorization code from redirect.")
            auth_code = m.group(1)
            log.debug("Step 4 ✓ — got authorization code")

            # --- Step 5: Exchange auth code for access token ---
            resp = session.post(
                _LOGIN_URLS["token"],
                data={
                    "grant_type": "authorization_code",
                    "redirect_uri": "https://fantasy.premierleague.com/",
                    "code": auth_code,
                    "code_verifier": code_verifier,
                    "client_id": _CLIENT_ID,
                },
            )
            access_token = resp.json()["access_token"]
            log.debug("Step 5 ✓ — got bearer token")

            # --- Step 6: GET /api/me/ with the bearer token ---
            me_resp = session.get(
                _LOGIN_URLS["me"],
                headers={"X-API-Authorization": f"Bearer {access_token}"},
            )
            me_resp.raise_for_status()
            profile = me_resp.json()
            log.info("Step 6 ✓ — FPL login complete for %s (entry=%s)",
                     email, profile.get("player", {}).get("entry"))
            return profile

        except (ValueError, RuntimeError):
            raise
        except Exception as e:
            log.exception("FPL login failed at an unexpected step")
            raise RuntimeError(
                f"FPL login failed at an unexpected step: {e}\n"
                "This may be due to off-season maintenance or API changes. "
                "Try using your Team ID directly instead."
            ) from e

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _get(self, path: str) -> dict | list:
        log.debug("GET %s%s", BASE_URL, path)
        resp = self._client.get(path)
        log.debug("  → %s (%d bytes)", resp.status_code, len(resp.content))
        resp.raise_for_status()
        return resp.json()

    def close(self):
        self._client.close()

    # context-manager support
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
