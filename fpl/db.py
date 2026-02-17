"""
SQLite database layer for FPL Agent.

Tables
------
managers     — app users (username + hashed password + linked FPL team ID)
chat_history — every prompt/response pair, per manager
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "fpl_agent.db"


# ── Connection helper ────────────────────────────────────────────────
@contextmanager
def _get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Schema bootstrap ────────────────────────────────────────────────
def init_db():
    """Create tables if they don't exist."""
    with _get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS managers (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT    NOT NULL,
                salt          TEXT    NOT NULL,
                fpl_team_id   INTEGER,
                fpl_team_name TEXT,
                manager_name  TEXT,
                overall_points INTEGER,
                overall_rank  INTEGER,
                created_at    TEXT    NOT NULL,
                updated_at    TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                manager_id  INTEGER NOT NULL REFERENCES managers(id),
                role        TEXT    NOT NULL CHECK(role IN ('user','assistant')),
                content     TEXT    NOT NULL,
                created_at  TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_chat_manager
                ON chat_history(manager_id, created_at);
            """
        )


# ── Password hashing ────────────────────────────────────────────────
def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), iterations=260_000
    ).hex()


# ── Manager CRUD ─────────────────────────────────────────────────────
def create_manager(username: str, password: str) -> dict | None:
    """Register a new manager. Returns the row dict or None if username taken."""
    salt = secrets.token_hex(16)
    pw_hash = _hash_password(password, salt)
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _get_conn() as conn:
            conn.execute(
                """INSERT INTO managers
                   (username, password_hash, salt, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (username, pw_hash, salt, now, now),
            )
            return get_manager_by_username(username)
    except sqlite3.IntegrityError:
        return None


def verify_manager(username: str, password: str) -> dict | None:
    """Check credentials. Returns row dict on success, None on failure."""
    mgr = get_manager_by_username(username)
    if mgr is None:
        return None
    if _hash_password(password, mgr["salt"]) != mgr["password_hash"]:
        return None
    return mgr


def get_manager_by_username(username: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM managers WHERE username = ?", (username,)
        ).fetchone()
        return dict(row) if row else None


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
    with _get_conn() as conn:
        conn.execute(
            """UPDATE managers
               SET fpl_team_id = ?, fpl_team_name = ?, manager_name = ?,
                   overall_points = ?, overall_rank = ?, updated_at = ?
               WHERE id = ?""",
            (fpl_team_id, fpl_team_name, manager_name,
             overall_points, overall_rank, now, manager_id),
        )


def unlink_fpl_team(manager_id: int):
    """Remove the linked FPL team from a manager."""
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            """UPDATE managers
               SET fpl_team_id = NULL, fpl_team_name = NULL, manager_name = NULL,
                   overall_points = NULL, overall_rank = NULL, updated_at = ?
               WHERE id = ?""",
            (now, manager_id),
        )


# ── Chat history ─────────────────────────────────────────────────────
def save_message(manager_id: int, role: str, content: str):
    """Persist a single chat message."""
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO chat_history (manager_id, role, content, created_at)
               VALUES (?, ?, ?, ?)""",
            (manager_id, role, content, now),
        )


def get_chat_history(manager_id: int, limit: int = 200) -> list[dict]:
    """Return recent messages for a manager, oldest first."""
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT role, content, created_at FROM chat_history
               WHERE manager_id = ?
               ORDER BY created_at DESC, id DESC
               LIMIT ?""",
            (manager_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


def clear_chat_history(manager_id: int):
    """Delete all chat history for a manager."""
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM chat_history WHERE manager_id = ?", (manager_id,)
        )


def get_all_prompts(limit: int = 500) -> list[dict]:
    """Return all user prompts across managers (for analysis)."""
    with _get_conn() as conn:
        rows = conn.execute(
            """SELECT ch.content, ch.created_at, m.username
               FROM chat_history ch
               JOIN managers m ON m.id = ch.manager_id
               WHERE ch.role = 'user'
               ORDER BY ch.created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# Auto-create tables on import
init_db()
