"""
FPL Agent — Streamlit Web UI

Run:  streamlit run app.py

Features:
  • App-level sign up / sign in (username + password, stored in PostgreSQL)
  • Link your FPL team via Team ID or FPL email login
  • Persistent chat history per user (PostgreSQL)
  • All user prompts saved for requirement analysis
"""

from __future__ import annotations

import os

import streamlit as st
from dotenv import load_dotenv

# Load .env locally; on Streamlit Cloud the secrets come from the dashboard
load_dotenv()

# Hydrate env vars from Streamlit secrets (for Cloud deployment)
# This bridges st.secrets → os.environ so fpl/db.py and tools pick them up.
for _key in ("OPENAI_API_KEY", "DATABASE_URL"):
    if _key not in os.environ:
        _val = st.secrets.get(_key, "")
        if _val:
            os.environ[_key] = _val

from fpl import db  # noqa: E402  (import after env setup)

# Ensure tables exist on startup
db.init_db()

# ── Page config ──────────────────────────────────────────────────────
st.set_page_config(
    page_title="FPL Agent",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS — FPL green accent ────────────────────────────────────
st.markdown(
    """
    <style>
    [data-testid="stSidebar"] { background: linear-gradient(180deg, #07332f 0%, #0b4a40 100%); }
    [data-testid="stSidebar"] * { color: #e8e8e8 !important; }
    [data-testid="stSidebar"] .stMetric label { color: #8bccbe !important; }
    [data-testid="stSidebar"] .stMetric [data-testid="stMetricValue"] { color: #fff !important; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ── Session state defaults ───────────────────────────────────────────
def _init_session():
    defaults = {
        "user": None,           # dict from managers table (logged-in user)
        "messages": [],         # in-memory chat messages for display
        "agent": None,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


_init_session()


# ── Convenience ──────────────────────────────────────────────────────
def _logged_in() -> bool:
    return st.session_state.user is not None


def _fpl_linked() -> bool:
    return _logged_in() and st.session_state.user.get("fpl_team_id") is not None


def _refresh_user():
    """Re-read user from DB (e.g. after linking FPL)."""
    if _logged_in():
        st.session_state.user = db.get_manager_by_username(
            st.session_state.user["username"]
        )


# ── FPL linking helpers ──────────────────────────────────────────────
def _link_with_team_id(team_id: int):
    """Fetch FPL team info and link to the logged-in user's profile."""
    from fpl.api_client import FPLClient

    client = FPLClient()
    info = client.get_team_info(team_id)

    if not info or "detail" in info:
        raise ValueError(f"Team ID {team_id} not found — double-check and try again.")

    team_name = info.get("name", "My Team")
    mgr_name = (
        f"{info.get('player_first_name', '')} "
        f"{info.get('player_last_name', '')}".strip()
    )
    points = info.get("summary_overall_points")
    rank = info.get("summary_overall_rank")

    db.link_fpl_team(
        manager_id=st.session_state.user["id"],
        fpl_team_id=team_id,
        fpl_team_name=team_name,
        manager_name=mgr_name,
        overall_points=points,
        overall_rank=rank,
    )
    os.environ["FPL_TEAM_ID"] = str(team_id)
    st.session_state.agent = None  # rebuild with new team id
    _refresh_user()


def _link_with_login(email: str, password: str):
    """FPL email login → extract team ID → link."""
    from fpl.api_client import FPLClient

    profile = FPLClient.login(email, password)

    team_id = None
    player_info = profile.get("player", {})
    if isinstance(player_info, dict):
        team_id = player_info.get("entry")
    if team_id is None:
        team_id = profile.get("entry")

    if team_id is None:
        raise ValueError(
            "Login succeeded but no FPL team was found on this account. "
            "You may not have registered a team this season."
        )

    _link_with_team_id(team_id)


def _unlink_fpl():
    db.unlink_fpl_team(st.session_state.user["id"])
    os.environ.pop("FPL_TEAM_ID", None)
    st.session_state.agent = None
    _refresh_user()


# ── Agent helpers ────────────────────────────────────────────────────
def _get_agent():
    if st.session_state.agent is None:
        from fpl.agent import build_agent

        st.session_state.agent = build_agent()
    return st.session_state.agent


def _run_agent(query: str) -> str:
    agent = _get_agent()
    result = agent.invoke({"messages": [{"role": "user", "content": query}]})
    return result["messages"][-1].content


# ── Auth helpers ─────────────────────────────────────────────────────
def _do_login(username: str, password: str) -> bool:
    user = db.verify_manager(username, password)
    if user is None:
        return False
    st.session_state.user = user
    # Hydrate env so tools can see the FPL team ID
    if user.get("fpl_team_id"):
        os.environ["FPL_TEAM_ID"] = str(user["fpl_team_id"])
    # Load persisted chat history
    history = db.get_chat_history(user["id"])
    st.session_state.messages = [
        {"role": m["role"], "content": m["content"]} for m in history
    ]
    st.session_state.agent = None
    return True


def _do_signup(username: str, password: str) -> bool:
    user = db.create_manager(username, password)
    if user is None:
        return False
    st.session_state.user = user
    st.session_state.messages = []
    st.session_state.agent = None
    return True


def _do_logout():
    os.environ.pop("FPL_TEAM_ID", None)
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    _init_session()


# =====================================================================
#  AUTH SCREEN  (shown when no user is logged in)
# =====================================================================
if not _logged_in():
    st.markdown(
        "<h1 style='text-align:center'>⚽ FPL Agent</h1>"
        "<p style='text-align:center;color:grey'>"
        "Your AI-powered Fantasy Premier League assistant</p>",
        unsafe_allow_html=True,
    )
    st.write("")

    # Centre the form with columns
    _, col, _ = st.columns([1, 2, 1])
    with col:
        tab_signin, tab_signup = st.tabs(["🔑 Sign In", "📝 Sign Up"])

        with tab_signin:
            with st.form("signin_form"):
                si_user = st.text_input("Username")
                si_pass = st.text_input("Password", type="password")
                si_btn = st.form_submit_button(
                    "Sign In", use_container_width=True
                )
            if si_btn:
                if not si_user or not si_pass:
                    st.warning("Fill in both fields, gaffer.")
                elif not _do_login(si_user.strip(), si_pass):
                    st.error("Wrong username or password. Try again!")
                else:
                    st.rerun()

        with tab_signup:
            with st.form("signup_form"):
                su_user = st.text_input("Choose a username")
                su_pass = st.text_input("Choose a password", type="password")
                su_pass2 = st.text_input("Confirm password", type="password")
                su_btn = st.form_submit_button(
                    "Create Account", use_container_width=True
                )
            if su_btn:
                if not su_user or not su_pass:
                    st.warning("All fields are required.")
                elif len(su_pass) < 4:
                    st.warning("Password must be at least 4 characters.")
                elif su_pass != su_pass2:
                    st.error("Passwords don't match.")
                elif not _do_signup(su_user.strip(), su_pass):
                    st.error(
                        f"Username **{su_user.strip()}** is already taken. "
                        "Pick another one!"
                    )
                else:
                    st.success("Account created! Redirecting…")
                    st.rerun()

    st.stop()  # nothing else renders until signed in


# =====================================================================
#  SIDEBAR  (user is logged in)
# =====================================================================
user = st.session_state.user

with st.sidebar:
    st.title("⚽ FPL Agent")
    st.caption("AI-powered Fantasy Premier League assistant")
    st.divider()

    # ── User info ────────────────────────────────────────────────────
    st.subheader(f"👤 {user['username']}")

    if _fpl_linked():
        st.markdown("**🟢 FPL Team Linked**")

        col1, col2 = st.columns(2)
        col1.metric("Manager", user.get("manager_name") or "—")
        col2.metric("Team ID", user["fpl_team_id"])

        st.markdown(f"**{user.get('fpl_team_name', '')}**")

        col3, col4 = st.columns(2)
        col3.metric(
            "Points",
            f"{user['overall_points']:,}" if user.get("overall_points") else "—",
        )
        col4.metric(
            "Rank",
            f"{user['overall_rank']:,}" if user.get("overall_rank") else "—",
        )

        st.divider()
        if st.button("🔗 Unlink FPL Team", use_container_width=True):
            _unlink_fpl()
            st.rerun()

    else:
        # ── Link FPL account ─────────────────────────────────────────
        st.markdown("**Link your FPL team** for personalised advice.")
        tab_id, tab_login = st.tabs(["🔢 Team ID", "🔐 FPL Login"])

        with tab_id:
            st.caption(
                "Your Team ID is in the URL when you view your team: "
                "`…/entry/`**`1234567`**`/event/…`"
            )
            with st.form("link_team_id"):
                tid = st.text_input("Team ID", placeholder="e.g. 1234567")
                tid_btn = st.form_submit_button("Link", use_container_width=True)
            if tid_btn:
                if not tid or not tid.strip().isdigit():
                    st.warning("Enter a valid numeric Team ID.")
                else:
                    with st.spinner("Fetching team info…"):
                        try:
                            _link_with_team_id(int(tid.strip()))
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))

        with tab_login:
            st.caption(
                "Your FPL credentials are used **once** to find your Team ID "
                "and are **never stored**."
            )
            with st.form("link_fpl_login"):
                fpl_email = st.text_input("FPL Email", placeholder="you@example.com")
                fpl_pass = st.text_input("FPL Password", type="password")
                fpl_btn = st.form_submit_button(
                    "Login & Link", use_container_width=True
                )
            if fpl_btn:
                if not fpl_email or not fpl_pass:
                    st.warning("Enter both email and password.")
                else:
                    with st.spinner("Logging in to FPL…"):
                        try:
                            _link_with_login(fpl_email, fpl_pass)
                            st.rerun()
                        except Exception as e:
                            st.error(
                                f"Could not link: {e}\n\n"
                                "**Tip:** If FPL is in off-season, use the Team ID tab."
                            )

        # Auto-detect from .env
        env_tid = os.getenv("FPL_TEAM_ID", "").strip()
        if env_tid:
            st.divider()
            st.info(f"Team ID **{env_tid}** found in `.env`")
            if st.button("Use this Team ID", use_container_width=True):
                with st.spinner("Fetching…"):
                    try:
                        _link_with_team_id(int(env_tid))
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

    st.divider()

    # ── Chat controls ────────────────────────────────────────────────
    if st.button("🗑️ Clear Chat History", use_container_width=True):
        db.clear_chat_history(user["id"])
        st.session_state.messages = []
        st.rerun()

    if st.button("🚪 Sign Out", use_container_width=True):
        _do_logout()
        st.rerun()

    st.caption(
        "Chat history is saved to your account and "
        "will be here when you come back. ✨"
    )


# =====================================================================
#  MAIN CHAT AREA
# =====================================================================
if not os.getenv("OPENAI_API_KEY", "").strip():
    st.warning(
        "⚠️ `OPENAI_API_KEY` not found. Add it to your `.env` file and restart.",
        icon="🔑",
    )
    st.stop()

# Header
st.title("⚽ FPL Agent")
if _fpl_linked():
    st.caption(
        f"Signed in as **{user['username']}** · "
        f"FPL team: **{user.get('fpl_team_name', '')}** · "
        f"Ask about your team, transfers, captain picks and more"
    )
else:
    st.caption(
        f"Signed in as **{user['username']}** · "
        "Link your FPL team in the sidebar for personalised advice, "
        "or ask general FPL questions"
    )

# Display chat history
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# Chat input
if prompt := st.chat_input("Ask anything about FPL…"):
    # Show & persist user message
    st.session_state.messages.append({"role": "user", "content": prompt})
    db.save_message(user["id"], "user", prompt)
    with st.chat_message("user"):
        st.markdown(prompt)

    # Get agent response
    with st.chat_message("assistant"):
        with st.spinner("Thinking…"):
            try:
                response = _run_agent(prompt)
            except Exception as e:
                response = f"❌ Error: {e}"
        st.markdown(response)

    # Persist assistant response
    st.session_state.messages.append({"role": "assistant", "content": response})
    db.save_message(user["id"], "assistant", response)
