"""
PostgreSQL persistence layer for FPL Agent.

Connects directly via DATABASE_URL (standard Postgres connection string).
Works with Supabase, Neon, Railway, Render, or any hosted Postgres.

Tables
------
managers     — app users (username + hashed password + linked FPL team ID)
chat_history — every prompt/response pair, per manager

Setup
-----
1. Get a free Postgres database (e.g. Supabase, Neon, Railway)
2. Run the SQL in `schema.sql` to create the tables
3. Set DATABASE_URL in your .env or Streamlit secrets
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras  # for RealDictCursor

log = logging.getLogger(__name__)


# ── Connection helper ────────────────────────────────────────────────
@contextmanager
def _get_conn():
    """Yield a connection from DATABASE_URL.  Auto-commits on success."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        log.error("DATABASE_URL is not set")
        raise RuntimeError(
            "DATABASE_URL is not set. "
            "Add it to .env or Streamlit secrets."
        )
    log.debug("Opening DB connection")
    conn = psycopg2.connect(url)
    try:
        yield conn
        conn.commit()
    except Exception:
        log.exception("DB operation failed — rolling back")
        conn.rollback()
        raise
    finally:
        conn.close()
        log.debug("DB connection closed")


def _fetch_one(query: str, params: tuple = ()) -> dict | None:
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            return dict(row) if row else None


def _fetch_all(query: str, params: tuple = ()) -> list[dict]:
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return [dict(r) for r in cur.fetchall()]


def _execute(query: str, params: tuple = ()):
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)


# ── Schema bootstrap ────────────────────────────────────────────────
def init_db():
    """Create tables if they don't exist (safe to call on every start)."""
    log.info("Running DB migrations (CREATE TABLE IF NOT EXISTS)…")
    ddl = """
    CREATE TABLE IF NOT EXISTS managers (
        id              BIGSERIAL PRIMARY KEY,
        username        TEXT        NOT NULL UNIQUE,
        password_hash   TEXT        NOT NULL,
        salt            TEXT        NOT NULL,
        fpl_team_id     INTEGER,
        fpl_team_name   TEXT,
        manager_name    TEXT,
        overall_points  INTEGER,
        overall_rank    INTEGER,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS chat_history (
        id          BIGSERIAL PRIMARY KEY,
        manager_id  BIGINT      NOT NULL REFERENCES managers(id) ON DELETE CASCADE,
        role        TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
        content     TEXT        NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX IF NOT EXISTS idx_chat_manager
        ON chat_history (manager_id, created_at);
    """
    _execute(ddl)
    log.info("DB migrations complete ✓")


# ── Password hashing (stdlib — no extra deps) ───────────────────────
def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), iterations=260_000
    ).hex()


# ── Manager CRUD ─────────────────────────────────────────────────────
def create_manager(username: str, password: str) -> dict | None:
    """Register a new manager.  Returns the row dict or None if username taken."""
    log.info("Creating manager: %s", username)
    salt = secrets.token_hex(16)
    pw_hash = _hash_password(password, salt)
    now = datetime.now(timezone.utc).isoformat()

    try:
        with _get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """INSERT INTO managers
                       (username, password_hash, salt, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s)
                       RETURNING *""",
                    (username, pw_hash, salt, now, now),
                )
                row = cur.fetchone()
                log.info("Manager created: %s (id=%s)", username, row["id"] if row else "?")
                return dict(row) if row else None
    except psycopg2.IntegrityError:
        log.warning("Username already taken: %s", username)
        return None


def verify_manager(username: str, password: str) -> dict | None:
    """Check credentials.  Returns row dict on success, None on failure."""
    log.debug("Verifying credentials for: %s", username)
    mgr = get_manager_by_username(username)
    if mgr is None:
        log.warning("Login failed — user not found: %s", username)
        return None
    if _hash_password(password, mgr["salt"]) != mgr["password_hash"]:
        log.warning("Login failed — wrong password for: %s", username)
        return None
    log.info("Login successful: %s (id=%s)", username, mgr["id"])
    return mgr


def get_manager_by_username(username: str) -> dict | None:
    return _fetch_one(
        "SELECT * FROM managers WHERE LOWER(username) = LOWER(%s)",
        (username,),
    )


def link_fpl_team(
    manager_id: int,
    fpl_team_id: int,
    fpl_team_name: str = "",
    manager_name: str = "",
    overall_points: int | None = None,
    overall_rank: int | None = None,
):
    """Attach (or update) an FPL team to a manager account."""
    log.info("Linking FPL team %s to manager_id=%s", fpl_team_id, manager_id)
    now = datetime.now(timezone.utc).isoformat()
    _execute(
        """UPDATE managers
           SET fpl_team_id = %s, fpl_team_name = %s, manager_name = %s,
               overall_points = %s, overall_rank = %s, updated_at = %s
           WHERE id = %s""",
        (fpl_team_id, fpl_team_name, manager_name,
         overall_points, overall_rank, now, manager_id),
    )


def unlink_fpl_team(manager_id: int):
    """Remove the linked FPL team from a manager."""
    log.info("Unlinking FPL team from manager_id=%s", manager_id)
    now = datetime.now(timezone.utc).isoformat()
    _execute(
        """UPDATE managers
           SET fpl_team_id = NULL, fpl_team_name = NULL, manager_name = NULL,
               overall_points = NULL, overall_rank = NULL, updated_at = %s
           WHERE id = %s""",
        (now, manager_id),
    )


# ── Chat history ─────────────────────────────────────────────────────
def save_message(manager_id: int, role: str, content: str):
    """Persist a single chat message."""
    log.debug("Saving %s message for manager_id=%s (%d chars)", role, manager_id, len(content))
    now = datetime.now(timezone.utc).isoformat()
    _execute(
        """INSERT INTO chat_history (manager_id, role, content, created_at)
           VALUES (%s, %s, %s, %s)""",
        (manager_id, role, content, now),
    )


def get_chat_history(manager_id: int, limit: int = 200) -> list[dict]:
    """Return recent messages for a manager, oldest first."""
    log.debug("Loading chat history for manager_id=%s (limit=%d)", manager_id, limit)
    return _fetch_all(
        """SELECT role, content, created_at FROM chat_history
           WHERE manager_id = %s
           ORDER BY created_at DESC, id DESC
           LIMIT %s""",
        (manager_id, limit),
    )[::-1]  # reverse so oldest first


def clear_chat_history(manager_id: int):
    """Delete all chat history for a manager."""
    log.info("Clearing chat history for manager_id=%s", manager_id)
    _execute("DELETE FROM chat_history WHERE manager_id = %s", (manager_id,))


def get_all_prompts(limit: int = 500) -> list[dict]:
    """Return all user prompts across managers (for analysis)."""
    return _fetch_all(
        """SELECT ch.content, ch.created_at, m.username
           FROM chat_history ch
           JOIN managers m ON m.id = ch.manager_id
           WHERE ch.role = 'user'
           ORDER BY ch.created_at DESC
           LIMIT %s""",
        (limit,),
    )
