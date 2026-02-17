# FPL Agent

An AI-powered Fantasy Premier League assistant built with **LangChain** + **LangGraph**.

## What is this?

This is an **AI agent** — a program where an LLM (like GPT-4o) is connected to **tools** it can call on its own to answer your questions. Instead of just chatting, it can *act*: fetch live FPL data, compare players, and reason about your squad.

### Agent Architecture

```
You ──► LangGraph ReAct Loop ──► LLM decides what to do
                │                        │
                │            ┌───────────┴───────────┐
                │            │ "I need fixture data" │
                │            └───────────┬───────────┘
                │                        │
                │                  Calls a Tool
                │                        │
                │            ┌───────────┴───────────┐
                │            │  FPL API (live data)   │
                │            └───────────┬───────────┘
                │                        │
                │              Tool returns data
                │                        │
                │            ┌───────────┴───────────┐
                │            │ LLM reasons & answers │
                │            └───────────────────────┘
                │
           Final answer ◄──────────────────────┘
```

### Key Concepts

| Concept | What it means here |
|---|---|
| **LangChain** | Framework that standardises LLM calls, prompts, and tools |
| **LangGraph** | Adds a *graph* (state machine) so the agent can loop: think → act → observe → think … |
| **Tool** | A Python function the LLM can call (e.g. `get_top_players_by_form`) |
| **ReAct** | "Reason + Act" — the agent pattern where the LLM alternates between reasoning and calling tools |

---

## Project Structure

```
fpl_agent/
├── app.py               ← Streamlit web UI (recommended)
├── main.py              ← CLI entry point (REPL or one-shot)
├── requirements.txt     ← Python dependencies
├── supabase_schema.sql  ← SQL to create tables in Supabase
├── .env.example         ← template for API keys
├── .gitignore
├── README.md            ← you are here
├── .streamlit/
│   └── config.toml      ← FPL-themed dark mode for Streamlit
└── fpl/
    ├── __init__.py
    ├── api_client.py    ← thin wrapper around the FPL REST API
    ├── tools.py         ← LangChain @tool functions the agent can use
    ├── agent.py         ← LangGraph ReAct agent definition
    ├── db.py            ← Supabase (PostgreSQL) persistence layer
    └── login.py         ← one-time login helper to fetch your Team ID
```

---

## FPL API Endpoints Used

All data comes from the **public** Fantasy Premier League API (no auth needed for reads):

| Endpoint | Returns |
|---|---|
| `/bootstrap-static/` | All players, teams, gameweeks, positions |
| `/element-summary/{id}/` | Single player's history + upcoming fixtures |
| `/fixtures/?event={gw}` | Fixtures for a gameweek |
| `/event/{gw}/live/` | Live points during a gameweek |
| `/entry/{team_id}/` | Manager's team info, rank, points |
| `/entry/{team_id}/history/` | GW-by-GW season history + past seasons |
| `/entry/{team_id}/event/{gw}/picks/` | Squad picks, captain, bench, chip |
| `/entry/{team_id}/transfers/` | Full transfer history |

Base URL: `https://fantasy.premierleague.com/api`

---

## Tools Available to the Agent

### General FPL Tools

| Tool | What it does |
|---|---|
| `get_top_players_by_form` | Top N players sorted by recent form |
| `get_player_details` | Deep-dive on a single player (stats, xG, fixtures) |
| `get_current_gameweek_info` | Current & next gameweek metadata |
| `get_fixtures_for_gameweek` | All matches + difficulty ratings for a GW |
| `get_best_value_players` | Best points-per-£m for a position |

### Your Team Tools (requires `FPL_TEAM_ID` in `.env`)

| Tool | What it does |
|---|---|
| `get_my_team` | Your current squad — 15 players, captain, bench, budget, chip |
| `get_my_season_history` | GW-by-GW points, rank trajectory, squad value over time |
| `get_my_transfers` | Every transfer you've made — who in/out, prices, when |

---

## Setting Up Your FPL Team ID

You need your Team ID for "my team" / "my squad" queries. Two options:

### Option A — Auto-fetch via login (easiest, great for mobile users)

```bash
python -m fpl.login
```

This will:
1. Prompt for your **FPL email** and **password**
2. Log in to the FPL API and fetch your Team ID
3. **Save it to `.env`** automatically

> ⚠️ Your password is **never stored** — it's used once to call the FPL API and then discarded.

### Option B — Find it manually (desktop browser)

1. Log in to [fantasy.premierleague.com](https://fantasy.premierleague.com)
2. Click **Points** (or **My Team**)
3. Look at the URL: `https://fantasy.premierleague.com/entry/1234567/event/26`
4. The number **1234567** is your Team ID
5. Add to `.env`: `FPL_TEAM_ID=1234567`

---

## Web UI (Recommended)

The Streamlit web UI provides a chat interface with **user accounts + persistent chat history**:

```bash
streamlit run app.py
```

### Features

- **Sign up / sign in** — app-level accounts (username + password) stored in Supabase
- **Link your FPL team** — enter Team ID or log in with FPL email to auto-detect it
- **Persistent chat** — your conversations are saved in PostgreSQL and reload when you sign back in
- **Prompt analytics** — all user prompts are stored; run SQL in the Supabase dashboard to analyse them
- **No-link mode** — general FPL queries work without linking an FPL team

> 🔒 **Privacy**: FPL credentials are **never** stored — used once to look up your Team ID and discarded.

---

## Database Setup (Supabase — free PostgreSQL)

1. **Create a free project** at [supabase.com](https://supabase.com)
2. Go to **SQL Editor** → **New Query** → paste the contents of `supabase_schema.sql` → **Run**
3. Go to **Settings → API** → copy **Project URL** and **anon/public key**
4. Add them to your `.env`:
   ```
   SUPABASE_URL=https://your-project.supabase.co
   SUPABASE_KEY=eyJ...your-anon-key...
   ```

### Digging into the data

The Supabase dashboard gives you a full PostgreSQL playground:

- **Table Editor** — browse managers & chat_history visually
- **SQL Editor** — run any query you want. Some starters are in `supabase_schema.sql`
- **Logs** — see every API call to your database
- **Realtime** — watch inserts live if you want

---

## Deploy to Streamlit Community Cloud (free)

1. Push this repo to **GitHub** (public or private)
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app**
3. Select your repo, branch, and set **Main file path** = `app.py`
4. Click **Advanced settings → Secrets** and paste:
   ```toml
   OPENAI_API_KEY = "sk-..."
   SUPABASE_URL   = "https://your-project.supabase.co"
   SUPABASE_KEY   = "eyJ...your-anon-key..."
   ```
5. Click **Deploy** — done! 🎉

---

## Quick Start

```bash
# 1. Clone & enter the project
cd fpl_agent

# 2. Create a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment
copy .env.example .env
# Edit .env → paste your OPENAI_API_KEY, SUPABASE_URL, SUPABASE_KEY

# 5. Create database tables
#    Go to supabase.com → your project → SQL Editor → paste supabase_schema.sql → Run

# 6. Run the web UI (recommended)
streamlit run app.py

# — OR — run the CLI
python main.py
```

### Example Queries

```
You: Show me my team
You: Who should I captain this week based on my squad?
You: Which of my players should I transfer out?
You: What's my rank trend this season?
You: Who are the best value midfielders right now?
You: Should I captain Salah or Palmer this week?
You: What are the fixtures for gameweek 26?
You: Tell me about Gyökeres — is he worth the price?
```

---

## What's Next? (ideas to build on)

1. **Transfer recommender** — add your team ID, let the agent suggest transfers
2. **Squad optimizer** — use linear programming to pick the best 15 under budget
3. **Chip advisor** — analyse when to play Wildcard / Triple Captain / Bench Boost
4. **Memory** — add conversation memory so the agent remembers your squad
5. **Notifications** — schedule the agent to alert you before deadlines
6. **Backtest** — replay past seasons to evaluate strategy quality
