"""DAIMO Incident management — Streamlit entry point. Run: streamlit run app.py (from daimo/web).

A question is handled over two runs. The submit run stores it as `pending_turn` and reruns, so the input is
disabled while the request is in flight. The next run sends it, moves it into `turns` and reruns again to
re-enable the input. If that run is interrupted (e.g. a sidebar click), the pending turn is still in session state
and is resent with its original filters instead of being lost."""
import streamlit as st

from daimo_web import client, config
from daimo_web.render import AVATAR, md, render_turn, render_turn_body, render_user
from daimo_web.sidebar import render_sidebar
from daimo_web.state import Turn, describe_filters, filters_to_options, init_state, validate_query

st.set_page_config(page_title="DAIMO Incident management", page_icon="🛠️", layout="wide")
st.title("DAIMO Incident management")

cfg = config.load()
ss = st.session_state
init_state(ss, cfg.top_k_default)
health = render_sidebar(cfg)
st.caption("Active filters: " + " · ".join(md(c) for c in describe_filters(ss.filters)))
if not health["ready"]:
    failing = ", ".join(f"{md(name)} ({md(c.get('detail'))})" for name, c in health["checks"].items() if not c.get("ok"))
    st.error(f"The retrieval service is not ready: {failing or 'unknown'}. Answers may fail until it recovers.",
             icon="⚠️")

for t in ss.turns:
    render_turn(t)

pending: Turn | None = ss.pending_turn
if prompt := st.chat_input("Describe the incident…", disabled=pending is not None):
    ss.pending_turn = Turn(query=prompt.strip(), filters=dict(ss.filters))
    st.rerun()

if pending is not None:
    with st.chat_message("user", avatar=AVATAR["user"]):
        render_user(pending)
    with st.chat_message("assistant", avatar=AVATAR["assistant"]):
        if problem := validate_query(pending.query):
            pending.error = {"kind": "invalid_request", "message": problem, "request_id": None}
        else:
            with st.spinner("Searching past incidents…"):
                try:
                    pending.response = client.triage(pending.query, session_id=ss.session_id, user_id=cfg.user_id,
                                                     options=filters_to_options(pending.filters, cfg.top_k_max),
                                                     cfg=cfg)
                except client.ClientError as e:
                    pending.error = e.to_dict()
                except Exception as e:  # anything else would leave the turn pending and the input disabled
                    pending.error = {"kind": "internal", "request_id": None,
                                     "message": f"Unexpected error handling the response ({type(e).__name__})."}
    ss.turns.append(pending)
    ss.pending_turn = None
    st.rerun()
