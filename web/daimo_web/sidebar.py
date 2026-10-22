"""Sidebar: filters that persist for the session, plus session controls.

`st.session_state.filters` is the canonical copy. Widgets use their own keys (f_*), seeded from it once and copied
back after each run, so the values survive reruns. Reset and New conversation run as on_click callbacks, the only
place Streamlit allows writing to a widget's key."""
import json
from pathlib import Path

import streamlit as st

from . import client
from .config import Config
from .state import default_filters, new_conversation

FALLBACK = Path(__file__).resolve().parents[1] / "facets.json"
WIDGET_KEYS = {"priority_max": "f_priority", "category": "f_category", "affected_service": "f_service",
               "assignment_group": "f_group", "closed_after": "f_closed_after", "top_k": "f_top_k"}
PRIORITY_LEVELS = [None, 1, 2, 3, 4, 5]  # Filters.priority_level_max is 1..5; 1 is the most urgent


@st.cache_data(ttl=600, show_spinner=False)
def _live_facets(url: str, _cfg: Config) -> dict:
    """Keyed on the URL. Only successes are cached (exceptions are not), so a recovered service is picked up."""
    return client.facets(_cfg)


@st.cache_data(ttl=30, show_spinner=False)
def _readiness(url: str, _cfg: Config) -> dict:
    return client.ready(_cfg)


def load_facets(cfg: Config) -> tuple[dict, bool]:
    """(facets, live). Falls back to facets.json when the service can't provide them."""
    try:
        return _live_facets(cfg.retrieval_url, cfg), True
    except client.ClientError:
        return json.loads(FALLBACK.read_text()), False


def _reset_filters(top_k_default: int) -> None:
    st.session_state.filters = default_filters(top_k_default)
    for name, key in WIDGET_KEYS.items():
        st.session_state[key] = st.session_state.filters[name]


def _new_conversation() -> None:
    new_conversation(st.session_state)


def _priority_label(v) -> str:
    return "Any" if v is None else ("P1 only" if v == 1 else f"P1–P{v}")


def _options(values: list[str], selected: list[str]) -> list[str]:
    """Facet values plus anything already selected, so a value that left the index stays selectable."""
    return sorted(set(values) | set(selected))


def render_sidebar(cfg: Config) -> dict:
    """Draws the sidebar, updates st.session_state.filters, and returns the cached /readyz result."""
    ss = st.session_state
    for name, key in WIDGET_KEYS.items():  # seed widgets from the canonical copy (first run, or after a reset)
        if key not in ss:
            ss[key] = ss.filters[name]
    facets, live = load_facets(cfg)
    health = _readiness(cfg.retrieval_url, cfg)

    with st.sidebar:
        st.header("Filters")
        st.selectbox("Priority", PRIORITY_LEVELS, key="f_priority", format_func=_priority_label,
                     help="Only incidents at this priority or more urgent.")
        st.multiselect("Category", _options(facets.get("category", []), ss.f_category), key="f_category",
                       placeholder="Any")
        st.multiselect("Affected service", _options(facets.get("affected_service", []), ss.f_service),
                       key="f_service", placeholder="Any")
        st.multiselect("Assignment group", _options(facets.get("assignment_group", []), ss.f_group),
                       key="f_group", placeholder="Any")
        st.date_input("Closed after", key="f_closed_after", format="YYYY-MM-DD",
                      help="Only incidents closed on or after this date. Leave empty for any date.")
        st.slider("Incidents to return", 1, cfg.top_k_max, key="f_top_k")
        if not live:
            st.caption("Filter values are from the built-in list; the service's list is unavailable.")
        st.caption("Filter changes apply to your next question.")
        st.button("Reset filters", on_click=_reset_filters, args=(cfg.top_k_default,), width="stretch")

        st.divider()
        st.caption(f"Session {ss.session_id} · {len(ss.turns)} question(s)")
        st.button("New conversation", on_click=_new_conversation, width="stretch",
                  help="Clears the conversation. Filters are kept.")
        st.caption("Service: :green[● ready]" if health["ready"] else "Service: :red[● not ready]")

    ss.filters = {name: list(ss[key]) if isinstance(ss[key], list) else ss[key] for name, key in WIDGET_KEYS.items()}
    ss.filters["top_k"] = min(ss.filters["top_k"] or cfg.top_k_default, cfg.top_k_max)
    return health
