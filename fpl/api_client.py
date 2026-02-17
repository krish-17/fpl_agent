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
import re
import secrets
import uuid

import httpx

BASE_URL = "https://fantasy.premierleague.com/api"

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
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"User-Agent": "FPL-Agent/0.1"},
        )

    # ------------------------------------------------------------------
    # Bootstrap (the "everything" endpoint)
    # ------------------------------------------------------------------
    def get_bootstrap(self) -> dict:
        """Return the full bootstrap-static payload (players, teams, events, …)."""
        return self._get("/bootstrap-static/")

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

        session = cffi_requests.Session(impersonate="chrome")

        # --- Step 0: Generate PKCE verifier + challenge ---
        code_verifier = _generate_code_verifier()
        code_challenge = _generate_code_challenge(code_verifier)
        initial_state = uuid.uuid4().hex

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
                raise ValueError(
                    f"Login failed: {err_msg or 'invalid email or password'}"
                )

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

            # --- Step 6: GET /api/me/ with the bearer token ---
            me_resp = session.get(
                _LOGIN_URLS["me"],
                headers={"X-API-Authorization": f"Bearer {access_token}"},
            )
            me_resp.raise_for_status()
            return me_resp.json()

        except (ValueError, RuntimeError):
            raise
        except Exception as e:
            raise RuntimeError(
                f"FPL login failed at an unexpected step: {e}\n"
                "This may be due to off-season maintenance or API changes. "
                "Try using your Team ID directly instead."
            ) from e

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _get(self, path: str) -> dict | list:
        resp = self._client.get(path)
        resp.raise_for_status()
        return resp.json()

    def close(self):
        self._client.close()

    # context-manager support
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
