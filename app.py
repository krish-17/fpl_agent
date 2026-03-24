"""
FPL Agent — Streamlit Web UI

Run:  streamlit run app.py

Features:
  • App-level sign up / sign in (username + password, stored in PostgreSQL)
  • Link your FPL team via Team ID or FPL email login
  • Sidebar navigation: Chat, My Team, Transfer Hub, Mini-Leagues, Rival Analysis, Gameweek Prep, Dream Team
  • Mini-league standings and rival comparison tools
  • Persistent chat history per user (PostgreSQL)
  • Draft Builder with saved gameweek plans
  • All user prompts saved for requirement analysis
"""

from __future__ import annotations

import json
import logging
import os

import streamlit as st
from dotenv import load_dotenv

# ── Logging setup ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

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
log.info("App startup — DB ready, page rendering")

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

    /* ── Mobile-friendly overrides ── */
    @media (max-width: 640px) {
        .block-container { padding-left: 0.5rem !important; padding-right: 0.5rem !important; }
        [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; gap: 0.25rem !important; }
        [data-testid="stHorizontalBlock"] > div { min-width: 45% !important; }
        [data-testid="stMetric"] { padding: 0.25rem !important; }
        [data-testid="stMetricValue"] { font-size: 1.1rem !important; }
        [data-testid="stMetricLabel"] { font-size: 0.75rem !important; }
    }
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
        "nav_page": "chat",     # current page for sidebar navigation
        "selected_rival_id": None,  # for rival analysis page
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
    log.info("Linking Team ID %s for user %s", team_id, st.session_state.user["username"])
    from fpl.api_client import FPLClient

    client = FPLClient()
    info = client.get_team_info(team_id)

    if not info or "detail" in info:
        log.warning("Team ID %s not found in FPL API", team_id)
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
    log.info("FPL login-link attempt for email: %s", email)
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
    log.info("Agent query: %s", query[:120])
    agent = _get_agent()
    result = agent.invoke({"messages": [{"role": "user", "content": query}]})
    response = result["messages"][-1].content
    log.info("Agent response: %d chars", len(response))
    return response


# ── Auth helpers ─────────────────────────────────────────────────────
def _do_login(username: str, password: str) -> bool:
    log.info("Sign-in attempt: %s", username)
    user = db.verify_manager(username, password)
    if user is None:
        log.warning("Sign-in failed: %s", username)
        return False
    log.info("Sign-in success: %s (id=%s)", username, user["id"])
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
    log.info("Sign-up attempt: %s", username)
    user = db.create_manager(username, password)
    if user is None:
        log.warning("Sign-up failed (username taken): %s", username)
        return False
    log.info("Sign-up success: %s (id=%s)", username, user["id"])
    st.session_state.user = user
    st.session_state.messages = []
    st.session_state.agent = None
    return True


def _do_logout():
    log.info("User signed out")
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

    # ── Navigation ────────────────────────────────────────────────────
    st.markdown("### Navigation")

    nav_options = [
        ("chat", "💬", "Chat"),
        ("my_team", "👕", "My Team"),
        ("transfers", "🔄", "Transfer Hub"),
        ("leagues", "🏆", "Mini-Leagues"),
        ("rivals", "⚔️", "Rival Analysis"),
        ("prep", "📋", "Gameweek Prep"),
        ("dream", "🌟", "Dream Team"),
    ]

    for key, icon, label in nav_options:
        is_current = st.session_state.nav_page == key
        btn_type = "primary" if is_current else "secondary"
        if st.button(f"{icon} {label}", key=f"nav_{key}", use_container_width=True, type=btn_type):
            st.session_state.nav_page = key
            st.rerun()

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
#  PITCH RENDERING HELPERS (shared by My Team, Gameweek Prep, Dream Team)
# =====================================================================
_POS_COLORS = {
    "GKP": "#ebff00",  # yellow
    "DEF": "#00ff87",  # green
    "MID": "#05f0ff",  # cyan
    "FWD": "#e90052",  # red/pink
}


def _render_player_card(p: dict, show_pts: bool = False):
    """Render a single player as a compact card using st.markdown."""
    pos = p.get("position", "?")
    name = p.get("name", "?")
    team = p.get("team", "")
    price = p.get("price", 0)
    form = p.get("form", "—")
    badges = ""
    if p.get("is_captain"):
        badges = " ⓒ"
    elif p.get("is_vice_captain"):
        badges = " Ⓥ"

    color = _POS_COLORS.get(pos, "#aaa")
    pts_line = ""
    if show_pts:
        pts = p.get("points", 0)
        pts_line = f"<div style='font-size:0.75rem;color:#fff;'>⭐ {pts} pts</div>"
    else:
        nf = p.get("next_fixture", "")
        pts_line = f"<div style='font-size:0.7rem;color:#ccc;'>📅 {nf}</div>" if nf and nf != "—" else ""

    st.markdown(
        f"""<div style='text-align:center;background:#1a1a2e;border-radius:8px;
            padding:4px 2px;margin:2px 0;border-left:3px solid {color};min-width:60px;'>
            <div style='font-weight:700;font-size:0.8rem;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'>{name}{badges}</div>
            <div style='font-size:0.65rem;color:{color};'>{pos} · {team}</div>
            <div style='font-size:0.65rem;color:#aaa;'>£{price:.1f}m · F {form}</div>
            {pts_line}
        </div>""",
        unsafe_allow_html=True,
    )


def _render_pitch(starters: list[dict], bench: list[dict],
                  show_pts: bool = False, title: str = ""):
    """Render a full pitch layout with starters grouped by position + bench row."""
    if title:
        st.markdown(f"### {title}")

    pos_order = ["GKP", "DEF", "MID", "FWD"]
    grouped: dict[str, list] = {p: [] for p in pos_order}
    for s in starters:
        pos = s.get("position", "?")
        if pos in grouped:
            grouped[pos].append(s)

    st.markdown(
        """<div style='background:linear-gradient(180deg,#0d6b38 0%,#0a5e30 30%,#0d6b38 60%,#0a5e30 100%);
        border-radius:12px;padding:16px 8px;margin-bottom:12px;'>""",
        unsafe_allow_html=True,
    )

    for pos in pos_order:
        players = grouped[pos]
        if not players:
            continue
        cols = st.columns(max(len(players), 1))
        for i, p in enumerate(players):
            with cols[i]:
                _render_player_card(p, show_pts=show_pts)

    st.markdown("</div>", unsafe_allow_html=True)

    if bench:
        st.markdown("#### 🪑 Bench")
        cols = st.columns(max(len(bench), 1))
        for i, p in enumerate(bench):
            with cols[i]:
                _render_player_card(p, show_pts=show_pts)


# =====================================================================
#  TAB RENDERERS
# =====================================================================

def render_chat_tab(user: dict):
    """Render the Chat tab — message history + agent interaction."""
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if prompt := st.chat_input("Ask anything about FPL…"):
        st.session_state.messages.append({"role": "user", "content": prompt})
        db.save_message(user["id"], "user", prompt)
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Thinking…"):
                try:
                    response = _run_agent(prompt)
                except Exception as e:
                    response = f"❌ Error: {e}"
            st.markdown(response)

        st.session_state.messages.append({"role": "assistant", "content": response})
        db.save_message(user["id"], "assistant", response)


def render_my_team_tab(user: dict):
    """Render the My Team tab — pitch view + Geek View player inspector."""
    if not _fpl_linked():
        st.info("👈 Link your FPL team in the sidebar to see your squad here.")
        st.stop()

    st.subheader("👕 My Squad")

    from fpl.tools import get_my_team_structured  # noqa: E402

    with st.spinner("Loading your squad…"):
        try:
            raw = get_my_team_structured.invoke({})
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            st.error(f"Could not load squad: {e}")
            data = None

    if data and "error" not in data:
        col_a, col_b = st.columns(2)
        col_a.metric("Manager", data.get("manager", "—"))
        col_b.metric("GW", data.get("gameweek", "—"))
        col_c, col_d = st.columns(2)
        col_c.metric("Bank", f"£{data.get('bank', 0):.1f}m")
        col_d.metric("Squad Value", f"£{data.get('squad_value', 0):.1f}m")

        if data.get("active_chip"):
            st.info(f"🃏 Active chip: **{data['active_chip']}**")

        _render_pitch(
            starters=data.get("starters", []),
            bench=data.get("bench", []),
            show_pts=False,
            title=f"GW{data.get('gameweek', '?')} Squad",
        )
    elif data:
        st.warning(data.get("error", "Unknown error"))

    # ── Geek View — player inspection panel ──────────────────────────
    st.divider()
    st.subheader("🔬 Geek View — Player Inspector")
    st.caption("Select a player to see their behavioural archetype, volatility, and reliability metrics.")

    _geek_players: list[str] = []
    if data and "error" not in data:
        for _section in ("starters", "bench"):
            for _p in data.get(_section, []):
                _geek_players.append(_p.get("name", "?"))

    if _geek_players:
        geek_player = st.selectbox(
            "Inspect player (geek view)",
            options=_geek_players,
            key="geek_player_select",
        )
    else:
        geek_player = st.text_input(
            "Player name", placeholder="e.g. Salah", key="geek_player_input"
        )

    if geek_player:
        from fpl.tools import classify_player_archetype as _archetype_tool  # noqa: E402
        from fpl.tools import get_player_volatility_profile as _volatility_tool  # noqa: E402

        with st.spinner(f"Analysing {geek_player}…"):
            try:
                arch_raw = _archetype_tool.invoke({"player_name": geek_player})
                arch = json.loads(arch_raw) if isinstance(arch_raw, str) else arch_raw
            except Exception as e:
                arch = {"error": str(e)}

            try:
                vol_raw = _volatility_tool.invoke({"player_name": geek_player, "window_gws": 8})
                vol = json.loads(vol_raw) if isinstance(vol_raw, str) else vol_raw
            except Exception as e:
                vol = {"error": str(e)}

        if "error" not in arch:
            st.markdown(
                f"### {arch['player']['name']} — *{arch.get('archetype', '?')}*"
            )
            tag_str = "  ".join(f"`{t}`" for t in arch.get("tags", []))
            st.markdown(f"**Tags:** {tag_str}")
            st.markdown(f"📝 {arch.get('summary', '')}")

            metrics = arch.get("metrics", {})
            expl = metrics.get("explosiveness", {})
            reg = metrics.get("regression", {})
            tal = metrics.get("talisman", {})
            rel = metrics.get("reliability", {})

            col1, col2 = st.columns(2)
            with col1:
                st.metric("Avg Pts/GW", expl.get("avg_points", "—"))
                st.metric("Haul Rate", f"{expl.get('haul_rate', 0):.0%}")
                st.metric("Regression", reg.get("regression_flag", "—"))
                st.metric("Reliability", f"{rel.get('reliability_score', '—')}/100")
            with col2:
                st.metric("Blank Rate", f"{expl.get('blank_rate', 0):.0%}")
                st.metric("Max Points", expl.get("max_points", "—"))
                st.metric("Talisman Idx", tal.get("talisman_index", "—"))
                st.metric("Injury Risk", rel.get("injury_risk", "—"))

            if "error" not in vol:
                st.markdown("#### Recent GW Points")
                pts_list = vol.get("points_per_gw", [])
                vstats = vol.get("stats", {})
                if pts_list:
                    st.bar_chart(pts_list, height=150, use_container_width=True)
                st.caption(
                    f"Volatility score: **{vstats.get('volatility_score', '—')}/100** · "
                    f"σ = {vstats.get('std_points', '—')} · "
                    f"Window: last {vol.get('window_gws', '?')} appearances"
                )
        elif "error" in arch:
            st.warning(arch["error"])


def render_transfer_hub_tab(user: dict):
    """Render the Transfer Hub tab — AI transfer recommender."""
    if not _fpl_linked():
        st.info("👈 Link your FPL team in the sidebar to get transfer recommendations.")
        st.stop()

    st.subheader("🔄 Transfer Recommender")

    horizon = st.radio(
        "Horizon",
        ["Next GW", "Next 3–5 GWs"],
        horizontal=True,
        key="tf_horizon",
    )
    risk = st.slider(
        "Risk appetite",
        min_value=0,
        max_value=100,
        value=40,
        help="0 = very conservative (free transfers only), 100 = aggressive (allow hits, chase differentials)",
        key="tf_risk",
    )
    allow_hits = st.checkbox("Allow hits (-4 per extra transfer)", value=(risk >= 50), key="tf_hits")
    effective_risk = risk if allow_hits else min(risk, 29)

    if st.button("🔍 Recommend Transfers", use_container_width=True, key="tf_btn"):
        from fpl.tools import recommend_transfers as _recommend_tool  # noqa: E402

        horizon_str = "1gw" if horizon == "Next GW" else "5gw"
        with st.spinner("Analysing your squad and the market…"):
            try:
                raw = _recommend_tool.invoke({"horizon": horizon_str, "risk": effective_risk})
                plans = json.loads(raw) if isinstance(raw, str) else raw
            except Exception as e:
                st.error(f"Error: {e}")
                plans = []

        if not plans:
            st.info("No transfer plans generated. Your squad may already be optimal!")
        else:
            for idx, plan in enumerate(plans):
                with st.expander(
                    f"{'🥇' if idx == 0 else '🥈' if idx == 1 else '🥉'} "
                    f"{plan.get('plan_name', f'Plan {idx+1}')}  "
                    f"(Upside: {plan.get('expected_upside_score', '—')}  |  "
                    f"Hit: {plan.get('hit_cost', 0)})",
                    expanded=(idx == 0),
                ):
                    transfers = plan.get("transfers", [])
                    if transfers:
                        for t in transfers:
                            st.markdown(
                                f"🔴 **OUT** {t['out']} ({t['out_team']}) "
                                f"£{t['out_price']:.1f}m · Form {t['out_form']}"
                            )
                            st.markdown(
                                f"➡️ 🟢 **IN** {t['in']} ({t['in_team']}) "
                                f"£{t['in_price']:.1f}m · Form {t['in_form']}"
                            )
                            if t.get("in_fixtures"):
                                st.caption(f"📅 {t['in_fixtures']}")
                            st.markdown("")
                        st.markdown("---")

                    st.markdown(f"**Rationale:** {plan.get('rationale', '—')}")
                    st.markdown(f"**Data used:** {plan.get('data_used', '—')}")
                    st.markdown(f"**Confidence:** {plan.get('confidence', '—')}")
                    st.markdown(f"⚠️ **Risk warning:** {plan.get('risk_warning', '—')}")

                    if plan.get("remaining_bank") is not None:
                        st.caption(f"💰 Remaining bank: £{plan['remaining_bank']:.1f}m")

                    plan_summary = (
                        f"I'm considering this transfer plan: "
                        + ", ".join(f"{t['out']} → {t['in']}" for t in transfers)
                        + f". Hit cost: {plan.get('hit_cost', 0)}. "
                        f"What do you think?"
                    ) if transfers else ""
                    if plan_summary:
                        if st.button("💬 Discuss in Chat", key=f"push_plan_{idx}"):
                            st.session_state.messages.append(
                                {"role": "user", "content": plan_summary}
                            )
                            db.save_message(user["id"], "user", plan_summary)
                            st.info("Plan sent to Chat tab — switch to 💬 Chat to continue!")


def render_gameweek_prep_tab(user: dict):
    """Render the Gameweek Prep tab — snapshot, risk profile, draft builder, recommendations, summary."""
    if not _fpl_linked():
        st.info("👈 Link your FPL team in the sidebar to use Gameweek Prep.")
        st.stop()

    st.subheader("📋 Gameweek Prep (Next GW Overview)")

    # ── Section A: Next GW & baseline ────────────────────────────────
    from fpl.tools import get_my_team_structured as _prep_team_tool  # noqa: E402
    from fpl.tools import get_current_gameweek_info as _prep_gw_tool  # noqa: E402

    with st.spinner("Loading baseline…"):
        try:
            _prep_team_raw = _prep_team_tool.invoke({})
            _prep_team = json.loads(_prep_team_raw) if isinstance(_prep_team_raw, str) else _prep_team_raw
        except Exception as _e:
            st.error(f"Could not load squad: {_e}")
            _prep_team = None

        try:
            _prep_gw_raw = _prep_gw_tool.invoke({})
            _prep_gw = json.loads(_prep_gw_raw) if isinstance(_prep_gw_raw, str) else _prep_gw_raw
        except Exception:
            _prep_gw = None

    _prep_gw_num = "—"
    if _prep_gw and _prep_gw.get("next_gameweek"):
        _prep_gw_num = _prep_gw["next_gameweek"].get("id", "—")
    elif _prep_team and "error" not in _prep_team:
        _prep_gw_num = _prep_team.get("gameweek", "—")

    _prep_bank = _prep_team.get("bank", 0) if (_prep_team and "error" not in _prep_team) else 0
    _prep_sv = _prep_team.get("squad_value", 0) if (_prep_team and "error" not in _prep_team) else 0
    _prep_chip = _prep_team.get("active_chip") if (_prep_team and "error" not in _prep_team) else None

    st.markdown("#### Next GW Snapshot")
    _snap_c1, _snap_c2 = st.columns(2)
    _snap_c1.metric("Gameweek", _prep_gw_num)
    _snap_c2.metric("Bank", f"£{_prep_bank:.1f}m")
    _snap_c3, _snap_c4 = st.columns(2)
    _snap_c3.metric("Squad Value", f"£{_prep_sv:.1f}m")
    _snap_c4.metric("Active Chip", _prep_chip or "None")

    st.divider()

    # ── Section B: Squad Risk Profile ────────────────────────────────
    st.markdown("#### Squad Risk Profile")

    from fpl.tools import analyze_squad_risk_profile as _prep_risk_tool  # noqa: E402

    with st.spinner("Analysing squad risk…"):
        try:
            _risk_raw = _prep_risk_tool.invoke({})
            _risk = json.loads(_risk_raw) if isinstance(_risk_raw, str) else _risk_raw
        except Exception as _e:
            _risk = {"error": str(_e)}

    if "error" in _risk:
        st.warning(_risk["error"])
    else:
        _risk_summary = _risk.get("summary", {})
        _rp1, _rp2 = st.columns(2)
        _rp1.metric("Explosive Picks", _risk_summary.get("explosive_count", 0))
        _rp2.metric("Consistent Picks", _risk_summary.get("consistent_count", 0))

        _hr_players = _risk_summary.get("high_risk_players", [])
        if _hr_players:
            st.warning(f"⚠️ High-risk players: {', '.join(_hr_players)}")

        _portfolio_notes = _risk_summary.get("portfolio_notes", "")
        if _portfolio_notes:
            st.info(_portfolio_notes)

        _per_player = _risk.get("per_player", [])
        if _per_player:
            with st.expander("📊 Per-Player Risk Breakdown", expanded=False):
                import pandas as pd  # noqa: E402
                _risk_df = pd.DataFrame(
                    [
                        {
                            "Name": p["name"],
                            "Pos": p["position"],
                            "Team": p["team"],
                            "Archetype": p["archetype"],
                            "Reliability": p["reliability_score"],
                            "Talisman": p["talisman_index"],
                            "Regression": p["regression_flag"],
                            "Bench": "✅" if p["on_bench"] else "",
                        }
                        for p in _per_player
                    ]
                )
                st.dataframe(_risk_df, use_container_width=True, hide_index=True)

    st.divider()

    # ── Section C: Draft Builder ─────────────────────────────────────
    _render_draft_builder(user, _prep_team, _prep_gw_num, _prep_bank)

    st.divider()

    # ── Section D: Recommended Changes ───────────────────────────────
    st.markdown("#### Recommended Changes")

    _prep_horizon = st.radio(
        "Horizon",
        ["Next GW", "Next 3–5 GWs"],
        horizontal=True,
        key="prep_horizon",
    )
    _prep_risk_slider = st.slider(
        "Risk appetite",
        min_value=0,
        max_value=100,
        value=40,
        help="0 = very conservative, 100 = aggressive",
        key="prep_risk",
    )
    _prep_allow_hits = st.checkbox(
        "Allow hits (-4 per extra transfer)",
        value=(_prep_risk_slider >= 50),
        key="prep_hits",
    )
    _prep_effective_risk = _prep_risk_slider if _prep_allow_hits else min(_prep_risk_slider, 29)

    if "prep_plans" not in st.session_state:
        st.session_state.prep_plans = None

    if st.button("🔍 Run Prep Recommendation", use_container_width=True, key="prep_rec_btn"):
        from fpl.tools import recommend_transfers as _prep_rec_tool  # noqa: E402

        _prep_horizon_str = "1gw" if _prep_horizon == "Next GW" else "5gw"
        with st.spinner("Analysing your squad and the market…"):
            try:
                _rec_raw = _prep_rec_tool.invoke({"horizon": _prep_horizon_str, "risk": _prep_effective_risk})
                _rec_plans = json.loads(_rec_raw) if isinstance(_rec_raw, str) else _rec_raw
            except Exception as _e:
                st.error(f"Error: {_e}")
                _rec_plans = []
        st.session_state.prep_plans = _rec_plans

    _rec_plans = st.session_state.prep_plans

    if _rec_plans is not None:
        if not _rec_plans:
            st.info("No changes recommended — your squad looks solid for this horizon. 💪")
        else:
            _top_plan = _rec_plans[0] if isinstance(_rec_plans, list) else _rec_plans
            _plan_name = _top_plan.get("plan_name", "Top Plan")
            _plan_upside = _top_plan.get("expected_upside_score", "—")
            _plan_hit = _top_plan.get("hit_cost", 0)

            with st.expander(
                f"🥇 {_plan_name}  (Upside: {_plan_upside}  |  Hit: {_plan_hit})",
                expanded=True,
            ):
                _transfers = _top_plan.get("transfers", [])
                if _transfers:
                    for _t in _transfers:
                        st.markdown(
                            f"🔴 **OUT** {_t['out']} ({_t['out_team']}) "
                            f"£{_t['out_price']:.1f}m · Form {_t['out_form']}"
                        )
                        st.markdown(
                            f"➡️ 🟢 **IN** {_t['in']} ({_t['in_team']}) "
                            f"£{_t['in_price']:.1f}m · Form {_t['in_form']}"
                        )
                        if _t.get("in_fixtures"):
                            st.caption(f"📅 {_t['in_fixtures']}")
                        st.markdown("")
                    st.markdown("---")

                st.markdown(f"**Rationale:** {_top_plan.get('rationale', '—')}")
                st.markdown(f"**Data used:** {_top_plan.get('data_used', '—')}")
                st.markdown(f"**Confidence:** {_top_plan.get('confidence', '—')}")
                st.markdown(f"⚠️ **Risk warning:** {_top_plan.get('risk_warning', '—')}")

                if _top_plan.get("remaining_bank") is not None:
                    st.caption(f"💰 Remaining bank: £{_top_plan['remaining_bank']:.1f}m")

    st.divider()

    # ── Section E: Prep Summary + Chat Hand-off ──────────────────────
    _render_prep_summary(user, _prep_gw_num, _risk, _rec_plans)


def _render_draft_builder(user: dict, _prep_team: dict | None,
                          _prep_gw_num, _prep_bank: float):
    """Render the interactive Draft Builder section within Gameweek Prep."""
    st.markdown("#### 🛠️ Draft Builder")
    st.caption(
        "Pick a player to replace, explore candidates within budget, "
        "and build your draft squad for the upcoming gameweek."
    )

    _draft_gw = int(_prep_gw_num) if isinstance(_prep_gw_num, int) or (isinstance(_prep_gw_num, str) and _prep_gw_num.isdigit()) else None

    # Initialise draft session state
    if "draft_changes" not in st.session_state:
        st.session_state.draft_changes = {}
    if "draft_candidates" not in st.session_state:
        st.session_state.draft_candidates = None

    # Load saved draft from DB on first load
    if "draft_loaded" not in st.session_state:
        st.session_state.draft_loaded = False
    if not st.session_state.draft_loaded and _draft_gw and _prep_team and "error" not in _prep_team:
        saved = db.get_draft_squad(user["id"], _draft_gw)
        if saved and isinstance(saved, dict):
            st.session_state.draft_changes = saved.get("changes", {})
        st.session_state.draft_loaded = True

    if not (_prep_team and "error" not in _prep_team):
        st.info("Could not load squad data for Draft Builder.")
        return

    # Build player list from current squad
    _all_squad = _prep_team.get("starters", []) + _prep_team.get("bench", [])
    _squad_options = [
        f"{p['name']} ({p['position']} · {p['team']} · £{p['price']:.1f}m)"
        for p in _all_squad
    ]

    _selected_player_label = st.selectbox(
        "Select player to replace",
        options=_squad_options,
        key="draft_out_select",
    )

    _sel_idx = _squad_options.index(_selected_player_label) if _selected_player_label else 0
    _sel_player = _all_squad[_sel_idx] if _sel_idx < len(_all_squad) else None

    if _sel_player:
        _sel_price = _sel_player.get("price", 0)
        _max_budget = round(_sel_price + _prep_bank, 1)

        _dc1, _dc2, _dc3 = st.columns(3)
        with _dc1:
            _draft_max_price = st.number_input(
                "Max price (£m)",
                min_value=3.5,
                max_value=20.0,
                value=min(_max_budget, 20.0),
                step=0.5,
                key="draft_max_price",
            )
        with _dc2:
            _draft_horizon = st.number_input(
                "Horizon (GWs)",
                min_value=1,
                max_value=5,
                value=3,
                step=1,
                key="draft_horizon",
            )
        with _dc3:
            _draft_risk = st.selectbox(
                "Risk level",
                options=["low", "medium", "high"],
                index=1,
                key="draft_risk_level",
            )

        if st.button("🔍 Find Replacement Options", use_container_width=True, key="draft_find_btn"):
            from fpl.tools import suggest_replacements_for_player as _suggest_tool  # noqa: E402

            with st.spinner(f"Finding replacements for {_sel_player['name']}…"):
                try:
                    _sug_raw = _suggest_tool.invoke({
                        "player_name": _sel_player["name"],
                        "max_price": float(_draft_max_price),
                        "horizon_gws": int(_draft_horizon),
                        "risk_level": _draft_risk,
                        "limit": 10,
                    })
                    _sug = json.loads(_sug_raw) if isinstance(_sug_raw, str) else _sug_raw
                except Exception as _e:
                    st.error(f"Error finding replacements: {_e}")
                    _sug = None

            if _sug and "error" not in _sug:
                st.session_state.draft_candidates = _sug
            elif _sug:
                st.warning(_sug.get("error", "Unknown error"))

        # Show candidates
        _sug_data = st.session_state.draft_candidates
        if _sug_data and "candidates" in _sug_data:
            _out_info = _sug_data.get("out_player", {})
            st.markdown(
                f"##### Replacing: **{_out_info.get('name', '?')}** "
                f"({_out_info.get('position', '?')} · {_out_info.get('team', '?')} · "
                f"£{_out_info.get('price', 0):.1f}m · Form {_out_info.get('form', '—')})"
            )

            _candidates = _sug_data.get("candidates", [])
            if not _candidates:
                st.info("No candidates found within budget. Try increasing the max price.")
            else:
                import pandas as pd  # noqa: E402

                _cand_rows = []
                for _c in _candidates:
                    _beh = _c.get("behaviour", {})
                    _stats = _c.get("stats", {})
                    _scores = _c.get("scores", {})
                    _deltas = _c.get("deltas_vs_out", {})
                    _cand_rows.append({
                        "Name": _c["name"],
                        "Team": _c["team"],
                        "Price": f"£{_c['price']:.1f}m",
                        "Form": _stats.get("form", "—"),
                        "Pts": _stats.get("total_points", 0),
                        "Archetype": _beh.get("archetype", "—"),
                        "Reliability": _beh.get("reliability_score", "—"),
                        "Overall": _scores.get("overall_score", 0),
                        "Δ Form": f"{_deltas.get('form_delta', 0):+.1f}",
                        "Δ Fix": f"{_deltas.get('fixture_difficulty_delta', 0):+.1f}",
                    })

                st.dataframe(
                    pd.DataFrame(_cand_rows),
                    use_container_width=True,
                    hide_index=True,
                )

                _cand_names = [c["name"] for c in _candidates]
                _pick_label = st.selectbox(
                    "Choose replacement",
                    options=_cand_names,
                    key="draft_pick_select",
                )
                if st.button("✅ Add to Draft", use_container_width=True, key="draft_add_btn"):
                    _chosen = next((c for c in _candidates if c["name"] == _pick_label), None)
                    if _chosen and _sel_player:
                        _out_eid = str(_sel_player.get("element_id", _sel_player.get("name")))
                        st.session_state.draft_changes[_out_eid] = {
                            "out_name": _sel_player["name"],
                            "out_position": _sel_player["position"],
                            "out_team": _sel_player["team"],
                            "out_price": _sel_player["price"],
                            "in_id": _chosen["id"],
                            "in_name": _chosen["name"],
                            "in_team": _chosen["team"],
                            "in_position": _chosen.get("position", _sel_player["position"]),
                            "in_price": _chosen["price"],
                            "in_form": _chosen.get("stats", {}).get("form", "—"),
                            "in_archetype": _chosen.get("behaviour", {}).get("archetype", "—"),
                        }
                        st.success(f"✅ Draft: {_sel_player['name']} → {_chosen['name']}")
                        st.session_state.draft_candidates = None
                        st.rerun()

    # ── Show current draft changes ───────────────────────────────────
    _changes = st.session_state.draft_changes
    if _changes:
        st.markdown("##### 📝 Current Draft Changes")
        for _eid, _ch in _changes.items():
            _col_out, _col_arrow, _col_in, _col_rm = st.columns([3, 1, 3, 1])
            with _col_out:
                st.markdown(
                    f"🔴 **{_ch['out_name']}** ({_ch['out_position']} · {_ch['out_team']} · "
                    f"£{_ch['out_price']:.1f}m)"
                )
            with _col_arrow:
                st.markdown("**➡️**")
            with _col_in:
                st.markdown(
                    f"🟢 **{_ch['in_name']}** ({_ch['in_position']} · {_ch['in_team']} · "
                    f"£{_ch['in_price']:.1f}m)"
                )
            with _col_rm:
                if st.button("❌", key=f"draft_rm_{_eid}"):
                    del st.session_state.draft_changes[_eid]
                    st.rerun()

        # Draft pitch view — apply changes to squad
        _draft_starters = []
        _draft_bench = []
        _out_ids = {str(eid) for eid in _changes}
        _change_map = {str(eid): ch for eid, ch in _changes.items()}

        for _section, _target in [
            (_prep_team.get("starters", []), _draft_starters),
            (_prep_team.get("bench", []), _draft_bench),
        ]:
            for _p in _section:
                _p_eid = str(_p.get("element_id", ""))
                if _p_eid in _out_ids:
                    _ch = _change_map[_p_eid]
                    _target.append({
                        "name": f"🆕 {_ch['in_name']}",
                        "position": _ch["in_position"],
                        "team": _ch["in_team"],
                        "price": _ch["in_price"],
                        "form": _ch.get("in_form", "—"),
                        "is_captain": _p.get("is_captain", False),
                        "is_vice_captain": _p.get("is_vice_captain", False),
                        "next_fixture": "",
                    })
                else:
                    _target.append(_p)

        _render_pitch(
            starters=_draft_starters,
            bench=_draft_bench,
            show_pts=False,
            title="📝 Draft Squad",
        )

        # Save / Clear draft buttons
        _save_col, _clear_col = st.columns(2)
        with _save_col:
            if st.button("💾 Save Draft", use_container_width=True, key="draft_save_btn"):
                if _draft_gw:
                    _draft_json = {
                        "gameweek": _draft_gw,
                        "changes": st.session_state.draft_changes,
                        "starters": [
                            {"name": p["name"], "position": p["position"],
                             "team": p["team"], "price": p["price"]}
                            for p in _draft_starters
                        ],
                        "bench": [
                            {"name": p["name"], "position": p["position"],
                             "team": p["team"], "price": p["price"]}
                            for p in _draft_bench
                        ],
                    }
                    try:
                        db.save_draft_squad(user["id"], _draft_gw, _draft_json)
                        st.success(f"Draft saved for GW{_draft_gw}! ✅")
                    except Exception as _e:
                        st.error(f"Could not save draft: {_e}")
                else:
                    st.warning("Cannot determine gameweek — draft not saved.")
        with _clear_col:
            if st.button("🗑️ Clear Draft", use_container_width=True, key="draft_clear_btn"):
                st.session_state.draft_changes = {}
                st.session_state.draft_candidates = None
                st.rerun()

    elif _draft_gw:
        if st.button("📂 Load Saved Draft", use_container_width=True, key="draft_load_btn"):
            saved = db.get_draft_squad(user["id"], _draft_gw)
            if saved and isinstance(saved, dict) and saved.get("changes"):
                st.session_state.draft_changes = saved["changes"]
                st.success(f"Draft loaded for GW{_draft_gw}!")
                st.rerun()
            else:
                st.info(f"No saved draft found for GW{_draft_gw}.")


def _render_prep_summary(user: dict, _prep_gw_num, _risk: dict, _rec_plans):
    """Render the Prep Summary section with chat hand-off."""
    st.markdown("#### Prep Summary")

    _summary_parts: list[str] = []
    _summary_parts.append(f"📋 **Gameweek {_prep_gw_num} Prep Summary**\n")

    if "error" not in _risk:
        _portfolio_notes_text = _risk.get("summary", {}).get("portfolio_notes", "")
        if _portfolio_notes_text:
            _summary_parts.append(f"**Squad Profile:** {_portfolio_notes_text}\n")

    # Draft changes (preferred) or transfer recommendation
    _draft_changes_for_summary = st.session_state.get("draft_changes", {})
    if _draft_changes_for_summary:
        _draft_strs = [
            f"{ch['out_name']} → {ch['in_name']}"
            for ch in _draft_changes_for_summary.values()
        ]
        _summary_parts.append(
            f"**Draft Plan:** {', '.join(_draft_strs)}\n"
        )
    elif _rec_plans and isinstance(_rec_plans, list) and len(_rec_plans) > 0:
        _top = _rec_plans[0]
        _tx_strs = [
            f"{t['out']} → {t['in']}" for t in _top.get("transfers", [])
        ]
        if _tx_strs:
            _summary_parts.append(
                f"**Recommended Plan:** {_top.get('plan_name', 'Plan 1')}\n"
                f"Transfers: {', '.join(_tx_strs)}\n"
                f"Expected upside: {_top.get('expected_upside_score', '—')} · "
                f"Hit cost: {_top.get('hit_cost', 0)}\n"
            )
        else:
            _summary_parts.append("**Transfers:** No changes recommended.\n")
    else:
        _summary_parts.append("**Transfers:** No recommendation run yet.\n")

    _prep_summary_text = "\n".join(_summary_parts)
    st.text_area(
        "Summary",
        value=_prep_summary_text,
        height=180,
        disabled=True,
        key="prep_summary_area",
    )

    if st.button("💬 Send Prep Summary to Chat", use_container_width=True, key="prep_send_chat"):
        st.session_state.messages.append({"role": "user", "content": _prep_summary_text})
        db.save_message(user["id"], "user", _prep_summary_text)
        st.info("Prep summary sent to Chat tab — switch to 💬 Chat to discuss.")


def render_dream_team_tab():
    """Render the Dream Team tab — best possible GW squad."""
    st.subheader("🏆 Dream Team (Best Possible GW Squad)")

    from fpl.api_client import FPLClient as _FPLClient  # noqa: E402
    _dt_client = _FPLClient()

    try:
        _dt_current_gw = _dt_client.get_current_gameweek()
    except Exception:
        _dt_current_gw = None

    default_gw = _dt_current_gw or 1
    gw_select = st.number_input(
        "Gameweek",
        min_value=1,
        max_value=38,
        value=default_gw,
        step=1,
        key="dream_gw",
    )

    if st.button("🏆 Generate Dream Squad", use_container_width=True, key="dream_btn"):
        from fpl.tools import get_dream_team_full15 as _dream_tool  # noqa: E402

        with st.spinner(f"Computing best squad for GW{gw_select}…"):
            try:
                raw = _dream_tool.invoke({"gameweek": int(gw_select)})
                dream = json.loads(raw) if isinstance(raw, str) else raw
            except Exception as e:
                st.error(f"Error: {e}")
                dream = None

        if dream and "error" not in dream:
            col_tp, col_bp = st.columns(2)
            col_tp.metric("Total XI Points", dream.get("total_points", "—"))
            col_bp.metric("Bench Points", dream.get("bench_points", "—"))
            st.metric("Captain", f"ⓒ {dream.get('captain', '—')}")

            _render_pitch(
                starters=dream.get("starters", []),
                bench=dream.get("bench", []),
                show_pts=True,
                title=f"GW{gw_select} Dream Squad",
            )
        elif dream:
            st.warning(dream.get("error", "Could not generate dream team."))


# =====================================================================
#  MINI-LEAGUES PAGE
# =====================================================================
def render_leagues_page(user: dict):
    """Render the Mini-Leagues page — league list and standings."""
    st.subheader("🏆 My Mini-Leagues")

    if not _fpl_linked():
        st.info("👈 Link your FPL team in the sidebar to see your leagues.")
        st.stop()

    from fpl.tools import get_my_leagues as _leagues_tool
    from fpl.tools import get_league_standings as _standings_tool

    with st.spinner("Loading your leagues…"):
        try:
            raw = _leagues_tool.invoke({})
            leagues = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            st.error(f"Could not load leagues: {e}")
            leagues = []

    if isinstance(leagues, dict) and "error" in leagues:
        st.error(leagues["error"])
        return

    if not leagues:
        st.info("No leagues found. Join a mini-league on the FPL website!")
        return

    # League selector
    league_options = {f"{lg['name']} ({lg['type'].upper()} · Rank: {lg['rank']})": lg for lg in leagues}
    selected_label = st.selectbox("Select a league", options=list(league_options.keys()))
    selected_league = league_options.get(selected_label)

    if selected_league:
        st.markdown(f"### {selected_league['name']}")

        col1, col2, col3 = st.columns(3)
        col1.metric("Your Rank", selected_league.get("rank", "—"))
        col2.metric("Last Rank", selected_league.get("last_rank", "—"))
        col3.metric("Teams", selected_league.get("entry_count", "—"))

        # Standings
        st.markdown("#### Standings")
        with st.spinner("Loading standings…"):
            try:
                standings_raw = _standings_tool.invoke({
                    "league_id": selected_league["id"],
                    "top_n": 50
                })
                standings_data = json.loads(standings_raw) if isinstance(standings_raw, str) else standings_raw
            except Exception as e:
                st.error(f"Could not load standings: {e}")
                standings_data = None

        if standings_data and "error" not in standings_data and "standings" in standings_data:
            import pandas as pd

            team_id = user.get("fpl_team_id")
            df = pd.DataFrame(standings_data["standings"])

            if not df.empty:
                # Display with highlighting
                st.dataframe(
                    df[["rank", "manager_name", "team_name", "total_points", "event_total"]].rename(columns={
                        "rank": "Rank",
                        "manager_name": "Manager",
                        "team_name": "Team",
                        "total_points": "Total Pts",
                        "event_total": "GW Pts",
                    }),
                    use_container_width=True,
                    hide_index=True,
                )

                # Quick rival buttons
                st.markdown("#### Quick Actions")
                st.caption("Select a rival to analyze their squad")

                rival_cols = st.columns(3)
                for i, entry in enumerate(standings_data["standings"][:9]):
                    if entry.get("team_id") != team_id:
                        with rival_cols[i % 3]:
                            if st.button(
                                f"⚔️ {entry['manager_name'][:12]}",
                                key=f"rival_{entry['team_id']}",
                                use_container_width=True,
                            ):
                                st.session_state.selected_rival_id = entry["team_id"]
                                st.session_state.nav_page = "rivals"
                                st.rerun()
        elif standings_data and "error" in standings_data:
            st.error(standings_data["error"])


# =====================================================================
#  RIVAL ANALYSIS PAGE
# =====================================================================
def render_rival_analysis_page(user: dict):
    """Render the Rival Analysis page — comparison and tracking."""
    st.subheader("⚔️ Rival Analysis")

    if not _fpl_linked():
        st.info("👈 Link your FPL team in the sidebar to analyze rivals.")
        st.stop()

    from fpl.tools import compare_with_rival as _compare_tool
    from fpl.tools import find_auto_rivals as _auto_rivals_tool
    from fpl.tools import track_rival_transfers as _transfers_tool
    from fpl.tools import get_my_leagues as _leagues_tool
    from fpl.tools import get_rival_team as _rival_team_tool

    # Two modes: Auto-detect or Manual
    mode = st.radio("Mode", ["Auto-Detect Rivals", "Manual Rival ID"], horizontal=True)

    if mode == "Auto-Detect Rivals":
        # Get leagues for selection
        with st.spinner("Loading leagues…"):
            try:
                leagues_raw = _leagues_tool.invoke({})
                leagues = json.loads(leagues_raw) if isinstance(leagues_raw, str) else leagues_raw
            except Exception:
                leagues = []

        if isinstance(leagues, dict) and "error" in leagues:
            st.error(leagues["error"])
            leagues = []

        classic_leagues = [lg for lg in leagues if lg.get("type") == "classic"]

        if classic_leagues:
            league_options = {lg["name"]: lg["id"] for lg in classic_leagues}
            selected_league_name = st.selectbox("Select league", options=list(league_options.keys()))
            selected_league_id = league_options.get(selected_league_name)

            proximity = st.slider("Rank proximity (+/-)", min_value=1, max_value=10, value=3)

            if st.button("🔍 Find Rivals", use_container_width=True):
                with st.spinner("Finding nearby rivals…"):
                    try:
                        rivals_raw = _auto_rivals_tool.invoke({
                            "league_id": selected_league_id,
                            "proximity": proximity
                        })
                        rivals_data = json.loads(rivals_raw) if isinstance(rivals_raw, str) else rivals_raw
                    except Exception as e:
                        st.error(f"Error: {e}")
                        rivals_data = None

                if rivals_data and "error" not in rivals_data and "rivals" in rivals_data:
                    st.success(f"Found **{len(rivals_data['rivals'])}** rivals near your rank")
                    st.markdown(f"**Your rank:** {rivals_data['your_rank']} ({rivals_data['your_points']} pts)")

                    for rival in rivals_data["rivals"]:
                        direction = "🔺 Above" if rival['rank_delta'] < 0 else "🔻 Below"
                        gap_sign = "+" if rival['points_gap'] > 0 else ""

                        with st.expander(
                            f"{direction} | {rival['manager_name']} · Rank {rival['rank']} · {gap_sign}{rival['points_gap']} pts gap"
                        ):
                            col1, col2 = st.columns(2)
                            col1.metric("Team", rival['team_name'])
                            col2.metric("Total Points", rival['total_points'])

                            if st.button(
                                f"⚔️ Compare with {rival['manager_name']}",
                                key=f"cmp_{rival['team_id']}",
                                use_container_width=True,
                            ):
                                st.session_state.selected_rival_id = rival["team_id"]
                                st.rerun()
                elif rivals_data and "error" in rivals_data:
                    st.error(rivals_data["error"])
        else:
            st.info("No classic leagues found. Auto-detect works best with classic leagues.")

    else:  # Manual mode
        rival_id = st.number_input(
            "Rival Team ID",
            min_value=1,
            step=1,
            value=st.session_state.get("selected_rival_id") or 1,
        )
        if st.button("Set Rival", use_container_width=True):
            st.session_state.selected_rival_id = int(rival_id)
            st.rerun()

    # Comparison section
    rival_id = st.session_state.get("selected_rival_id")
    if rival_id:
        st.divider()
        st.markdown(f"### Analyzing Team #{rival_id}")

        tab_compare, tab_squad, tab_transfers = st.tabs(["📊 Comparison", "👕 Squad", "🔄 Transfers"])

        with tab_compare:
            if st.button("🔄 Run Comparison", use_container_width=True, key="run_compare"):
                with st.spinner("Comparing squads…"):
                    try:
                        compare_raw = _compare_tool.invoke({"rival_team_id": int(rival_id)})
                        compare_data = json.loads(compare_raw) if isinstance(compare_raw, str) else compare_raw
                    except Exception as e:
                        st.error(f"Error: {e}")
                        compare_data = None

                if compare_data and "error" not in compare_data:
                    # Summary metrics
                    col1, col2, col3 = st.columns(3)
                    col1.metric("Your Points", compare_data["you"]["total_points"])
                    col2.metric("Rival Points", compare_data["rival"]["total_points"])

                    gap = compare_data['points_gap']
                    gap_color = "normal" if gap >= 0 else "inverse"
                    col3.metric("Gap", f"{gap:+}", delta_color=gap_color)

                    # Captain comparison
                    cap_match = "✅ Same" if compare_data["captain_match"] else "❌ Different"
                    st.markdown(
                        f"**Captains:** You: **{compare_data['you']['captain']}** · "
                        f"Rival: **{compare_data['rival']['captain']}** ({cap_match})"
                    )

                    st.divider()

                    # Differentials
                    col_you, col_shared, col_rival = st.columns(3)

                    with col_you:
                        st.markdown("#### 🟢 Your Differentials")
                        for p in compare_data["your_differentials"]:
                            st.markdown(f"**{p['name']}** ({p['position']})")
                            st.caption(f"{p['team']} · Form: {p['form']} · Pts: {p['total_points']}")

                    with col_shared:
                        st.markdown("#### ⚪ Shared Players")
                        for p in compare_data["shared_players"]:
                            st.markdown(f"**{p['name']}** ({p['position']})")

                    with col_rival:
                        st.markdown("#### 🔴 Rival's Differentials")
                        for p in compare_data["rival_differentials"]:
                            st.markdown(f"**{p['name']}** ({p['position']})")
                            st.caption(f"{p['team']} · Form: {p['form']} · Pts: {p['total_points']}")

                elif compare_data and "error" in compare_data:
                    st.error(compare_data["error"])

        with tab_squad:
            if st.button("👕 Load Rival Squad", use_container_width=True, key="load_rival_squad"):
                with st.spinner("Loading rival squad…"):
                    try:
                        squad_raw = _rival_team_tool.invoke({"rival_team_id": int(rival_id)})
                        squad_data = json.loads(squad_raw) if isinstance(squad_raw, str) else squad_raw
                    except Exception as e:
                        st.error(f"Error: {e}")
                        squad_data = None

                if squad_data and "error" not in squad_data:
                    col1, col2 = st.columns(2)
                    col1.metric("Manager", squad_data.get("manager_name", "—"))
                    col2.metric("Team", squad_data.get("team_name", "—"))

                    col3, col4 = st.columns(2)
                    col3.metric("Total Points", squad_data.get("total_points", "—"))
                    col4.metric("Overall Rank", f"{squad_data.get('overall_rank', 0):,}" if squad_data.get('overall_rank') else "—")

                    # Display squad
                    st.markdown("#### Squad")
                    starters = [p for p in squad_data.get("squad", []) if not p.get("on_bench")]
                    bench = [p for p in squad_data.get("squad", []) if p.get("on_bench")]

                    _render_pitch(starters=starters, bench=bench, show_pts=False, title="Rival's Squad")

                    # Recent transfers
                    if squad_data.get("recent_transfers"):
                        st.markdown("#### Recent Transfers")
                        for t in squad_data["recent_transfers"]:
                            st.markdown(f"**GW{t['gw']}:** {t['out']} → {t['in']}")

                elif squad_data and "error" in squad_data:
                    st.error(squad_data["error"])

        with tab_transfers:
            gws = st.slider("Last N gameweeks", min_value=1, max_value=10, value=5, key="rival_transfer_gws")
            if st.button("📋 Track Transfers", use_container_width=True, key="track_transfers"):
                with st.spinner("Loading transfer history…"):
                    try:
                        transfers_raw = _transfers_tool.invoke({
                            "rival_team_id": int(rival_id),
                            "last_n_gws": gws
                        })
                        transfers_data = json.loads(transfers_raw) if isinstance(transfers_raw, str) else transfers_raw
                    except Exception as e:
                        st.error(f"Error: {e}")
                        transfers_data = None

                if transfers_data and "error" not in transfers_data:
                    st.markdown(
                        f"**{transfers_data['rival_name']}** made "
                        f"**{transfers_data['transfers_in_range']}** transfers ({transfers_data['range_gws']})"
                    )

                    if transfers_data.get("transfers"):
                        for t in transfers_data["transfers"]:
                            st.markdown(
                                f"**GW{t['gameweek']}:** {t['out']} (£{t['out_price']:.1f}m) → "
                                f"{t['in']} (£{t['in_price']:.1f}m)"
                            )
                    else:
                        st.info("No transfers in this range.")
                elif transfers_data and "error" in transfers_data:
                    st.error(transfers_data["error"])


# =====================================================================
#  MAIN CONTENT AREA — PAGE ROUTING
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

# ── Page Routing (sidebar navigation) ────────────────────────────────
page = st.session_state.get("nav_page", "chat")

if page == "chat":
    render_chat_tab(user)
elif page == "my_team":
    render_my_team_tab(user)
elif page == "transfers":
    render_transfer_hub_tab(user)
elif page == "leagues":
    render_leagues_page(user)
elif page == "rivals":
    render_rival_analysis_page(user)
elif page == "prep":
    render_gameweek_prep_tab(user)
elif page == "dream":
    render_dream_team_tab()
