"""
Supabase (PostgreSQL) persistence layer for FPL Agent.

Tables (create these in your Supabase dashboard → SQL Editor)
------
managers     — app users (username + hashed password + linked FPL team ID)
chat_history — every prompt/response pair, per manager

Setup
-----
1. Create a free project at https://supabase.com
2. Run the SQL in `supabase_schema.sql` in the SQL Editor
3. Copy the project URL + anon key into your .env / Streamlit secrets
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timezone

from supabase import create_client, Client

# ── Supabase client (initialised lazily) ─────────────────────────────
_client: Client | None = None


def _sb() -> Client:
    """Return a cached Supabase client, creating it on first call."""
    global _client
    if _client is None:
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_KEY", "")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_KEY must be set. "
                "Add them to .env or Streamlit secrets."
            )
        _client = create_client(url, key)
    return _client


# ── Password hashing (stdlib — no extra deps) ───────────────────────
def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), iterations=260_000
    ).hex()


# ── Manager CRUD ─────────────────────────────────────────────────────
def create_manager(username: str, password: str) -> dict | None:
    """Register a new manager.  Returns the row dict or None if username taken."""
    salt = secrets.token_hex(16)
    pw_hash = _hash_password(password, salt)
    now = datetime.now(timezone.utc).isoformat()

    try:
        result = (
            _sb()
            .table("managers")
            .insert(
                {
                    "username": username,
                    "password_hash": pw_hash,
                    "salt": salt,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            .execute()
        )
        if result.data:
            return result.data[0]
        return None
    except Exception:
        # Unique-constraint violation → username taken
        return None


def verify_manager(username: str, password: str) -> dict | None:
    """Check credentials.  Returns row dict on success, None on failure."""
    mgr = get_manager_by_username(username)
    if mgr is None:
        return None
    if _hash_password(password, mgr["salt"]) != mgr["password_hash"]:
        return None
    return mgr


def get_manager_by_username(username: str) -> dict | None:
    result = (
        _sb()
        .table("managers")
        .select("*")
        .ilike("username", username)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def link_fpl_team(
    manager_id: int,
    fpl_team_id: int,
    fpl_team_name: str = "",
    manager_name: str = "",
    overall_points: int | None = None,
    overall_rank: int | None = None,
):
    """Attach (or update) an FPL team to a manager account."""
    now = datetime.now(timezone.utc).isoformat()
    _sb().table("managers").update(
        {
            "fpl_team_id": fpl_team_id,
            "fpl_team_name": fpl_team_name,
            "manager_name": manager_name,
            "overall_points": overall_points,
            "overall_rank": overall_rank,
            "updated_at": now,
        }
    ).eq("id", manager_id).execute()


def unlink_fpl_team(manager_id: int):
    """Remove the linked FPL team from a manager."""
    now = datetime.now(timezone.utc).isoformat()
    _sb().table("managers").update(
        {
            "fpl_team_id": None,
            "fpl_team_name": None,
            "manager_name": None,
            "overall_points": None,
            "overall_rank": None,
            "updated_at": now,
        }
    ).eq("id", manager_id).execute()


# ── Chat history ─────────────────────────────────────────────────────
def save_message(manager_id: int, role: str, content: str):
    """Persist a single chat message."""
    now = datetime.now(timezone.utc).isoformat()
    _sb().table("chat_history").insert(
        {
            "manager_id": manager_id,
            "role": role,
            "content": content,
            "created_at": now,
        }
    ).execute()


def get_chat_history(manager_id: int, limit: int = 200) -> list[dict]:
    """Return recent messages for a manager, oldest first."""
    result = (
        _sb()
        .table("chat_history")
        .select("role, content, created_at")
        .eq("manager_id", manager_id)
        .order("created_at", desc=True)
        .order("id", desc=True)
        .limit(limit)
        .execute()
    )
    return list(reversed(result.data)) if result.data else []


def clear_chat_history(manager_id: int):
    """Delete all chat history for a manager."""
    _sb().table("chat_history").delete().eq("manager_id", manager_id).execute()


def get_all_prompts(limit: int = 500) -> list[dict]:
    """Return all user prompts across managers (for analysis).

    NOTE: For heavier analytics, use the Supabase dashboard directly —
    you can run arbitrary SQL there.
    """
    result = (
        _sb()
        .table("chat_history")
        .select("content, created_at, manager_id")
        .eq("role", "user")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data if result.data else []
