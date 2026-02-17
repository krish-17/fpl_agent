-- ============================================================
--  FPL Agent — PostgreSQL Schema
--  Works with Supabase, Neon, Railway, Render, or any Postgres.
--
--  Supabase:  Dashboard → SQL Editor → New Query → paste & Run
--  Others:    psql $DATABASE_URL -f schema.sql
-- ============================================================

-- 1. Managers (app-level user accounts)
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

-- Case-insensitive username lookups
CREATE UNIQUE INDEX IF NOT EXISTS idx_managers_username_lower
    ON managers (LOWER(username));

-- 2. Chat history (every prompt & response, per manager)
CREATE TABLE IF NOT EXISTS chat_history (
    id          BIGSERIAL PRIMARY KEY,
    manager_id  BIGINT      NOT NULL REFERENCES managers(id) ON DELETE CASCADE,
    role        TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chat_manager
    ON chat_history (manager_id, created_at);

-- ============================================================
--  Supabase-only: enable RLS with a permissive policy.
--  Skip these if you're on a different Postgres host.
-- ============================================================
-- ALTER TABLE managers      ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE chat_history  ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY "Allow all" ON managers     FOR ALL USING (true) WITH CHECK (true);
-- CREATE POLICY "Allow all" ON chat_history FOR ALL USING (true) WITH CHECK (true);

-- 3. Draft squads (saved gameweek plans per manager)
CREATE TABLE IF NOT EXISTS draft_squads (
    id              BIGSERIAL PRIMARY KEY,
    manager_id      BIGINT      NOT NULL REFERENCES managers(id) ON DELETE CASCADE,
    gameweek        INTEGER     NOT NULL,
    squad_json      JSONB       NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (manager_id, gameweek)
);

CREATE INDEX IF NOT EXISTS idx_draft_squads_manager_gw
    ON draft_squads (manager_id, gameweek);

-- ============================================================
--  Useful analytics queries you can run anytime:
-- ============================================================
--
--  All user prompts (newest first):
--    SELECT ch.content, ch.created_at, m.username
--    FROM chat_history ch
--    JOIN managers m ON m.id = ch.manager_id
--    WHERE ch.role = 'user'
--    ORDER BY ch.created_at DESC;
--
--  Prompt count per user:
--    SELECT m.username, COUNT(*) AS prompt_count
--    FROM chat_history ch
--    JOIN managers m ON m.id = ch.manager_id
--    WHERE ch.role = 'user'
--    GROUP BY m.username
--    ORDER BY prompt_count DESC;
--
--  Most active day:
--    SELECT DATE(created_at) AS day, COUNT(*) AS prompts
--    FROM chat_history WHERE role = 'user'
--    GROUP BY day ORDER BY prompts DESC LIMIT 10;
