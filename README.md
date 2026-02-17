# FPL Agent 🏈

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
├── .env.example         ← template for API keys
├── .gitignore
├── README.md            ← you are here
└── fpl/
    ├── __init__.py
    ├── api_client.py    ← thin wrapper around the FPL REST API
    ├── tools.py         ← LangChain @tool functions the agent can use
    ├── agent.py         ← LangGraph ReAct agent definition
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

The Streamlit web UI provides a chat interface with **session-based authentication**:

```bash
streamlit run app.py
```

### Features

- **Sidebar login** — enter your FPL email & password to connect your account
- **Session-only credentials** — your password is used once to fetch your Team ID, then discarded. Your Team ID lives in memory only — it's wiped when you log out or close the tab
- **Chat interface** — ask the agent anything, with full conversation history
- **No-login mode** — general FPL queries work without connecting an account

> 🔒 **Privacy**: Credentials are **never** written to disk through the UI. They exist only in your browser session's memory.

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

# 4. Set your OpenAI key
copy .env.example .env
# Edit .env → paste your OPENAI_API_KEY

# 5. Run the web UI (recommended)
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
