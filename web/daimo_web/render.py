"""Draw one conversation turn: the question with its filters, then the answer, confidence and evidence.
The same functions draw live and replayed turns. Everything from the service (ticket fields and LLM text) is
untrusted: it goes through md() so Markdown links, images, LaTeX and Streamlit directives render as plain text."""
import re

import streamlit as st

from .state import Turn, describe_filters

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#+\-.!|~$:])")
_INC_REF = re.compile(r"\\\[(INC\d{5,})\\\]")  # an escaped "[INC12345678]" citation in answer text
_SAFE_URL = re.compile(r"^https://[^\s()<>\"']+$")

LABEL_COLOR = {"high": "green", "medium": "orange", "low": "red"}
AVATAR = {"user": "🧑", "assistant": "🛠️"}


def md(text) -> str:
    """Escape untrusted text for st.markdown (HTML is already inert without unsafe_allow_html)."""
    return _MD_SPECIAL.sub(r"\\\1", str(text if text is not None else ""))


def cited_md(text: str) -> str:
    """Escape LLM text, then turn its [INC…] references (shape-checked by the regex) into badges."""
    return _INC_REF.sub(r":blue-badge[\1]", md(text))


def _refs(citations: list[str], text: str) -> str:
    """Badges for citations not already written inline in the text."""
    extra = [c for c in citations if f"[{c}]" not in text and re.fullmatch(r"INC\d{5,}", c)]
    return "".join(f" :blue-badge[{c}]" for c in extra)


def render_user(turn: Turn) -> None:
    st.markdown(md(turn.query))
    if chips := describe_filters(turn.filters):
        st.caption("Filters: " + " · ".join(md(c) for c in chips))


def confidence_badge(conf: dict) -> None:
    label = conf.get("label", "low")
    color = LABEL_COLOR.get(label, "gray")
    st.badge(f"{label.capitalize()} confidence · {conf['overall']:.2f}", color=color)
    st.caption(f"Threshold {conf['threshold']:.2f} · {conf['passed']} incident(s) passed · "
               f"{conf['withheld']} below threshold")


def render_answer(answer: dict) -> None:
    st.markdown(cited_md(answer["summary"]))
    if cause := answer.get("likely_cause"):
        st.markdown("**Likely cause:** " + cited_md(cause["text"]) + _refs(cause.get("citations", []), cause["text"]))
    if steps := answer.get("recommended_actions"):
        st.markdown("**Recommended actions**")
        st.markdown("\n".join(f"{i}. {cited_md(s['step'])}{_refs(s.get('citations', []), s['step'])}"
                              for i, s in enumerate(steps, 1)))
    if answer.get("caveats"):
        st.caption("Caveats: " + md(answer["caveats"]))


def evidence_card(c: dict) -> None:
    title = f"{md(c['incident_number'])} · {md(c.get('title') or 'Untitled')} · " \
            f"{c['confidence']:.2f} {md(c['confidence_label'])}" + (" · ✓ cited" if c.get("cited_in_answer") else "")
    with st.expander(title):
        st.progress(min(max(float(c["confidence"]), 0.0), 1.0),
                    text=f"Confidence {c['confidence']:.2f} ({md(c['confidence_label'])})")
        facts = [("Priority", c.get("priority")), ("Service", c.get("affected_service")), ("CI", c.get("affected_ci")),
                 ("Category", c.get("category")), ("State", c.get("state")),
                 ("Resolution", c.get("resolution_code")), ("Closed", (c.get("closed_at") or "")[:10] or None)]
        st.markdown(" · ".join(f"**{k}:** {md(v)}" for k, v in facts if v))
        if c.get("snippet"):
            st.markdown(md(c["snippet"]))
        st.caption(f"Match: {md(c['match_reason'])} on {md(c['matched_chunk_type'])} · "
                   f"KB: {md(', '.join(c.get('related_kb_articles') or [])) or '—'}")
        if c.get("url") and _SAFE_URL.match(c["url"]):
            st.link_button("Open in ServiceNow", c["url"])


def render_evidence(citations: list[dict]) -> None:
    if not citations:
        return
    ordered = sorted(citations, key=lambda c: (not c.get("cited_in_answer"), -c["confidence"]))
    st.markdown(f"**Evidence ({len(ordered)})**")
    for c in ordered:
        evidence_card(c)


def render_footer(turn: Turn) -> None:
    parts = [f"request {md(turn.request_id)}"] if turn.request_id else []
    if turn.response:
        total = (turn.response.get("metrics") or {}).get("latency_ms", {}).get("total")
        if total is not None:
            parts.append(f"{total / 1000:.1f} s")
        if llm := (turn.response.get("model") or {}).get("llm"):
            parts.append(md(llm))
    if parts:
        st.caption(" · ".join(parts))


def render_turn_body(turn: Turn) -> None:
    """The assistant side of a turn, by status."""
    if turn.error:
        st.error(md(turn.error["message"]))
        render_footer(turn)
        return
    r = turn.response
    if r.get("notice"):
        st.caption("🔒 " + md(r["notice"]))
    if r.get("confidence"):  # absent for greeting / out_of_scope / blocked
        confidence_badge(r["confidence"])
    status = r["status"]
    if status == "answered":
        render_answer(r["answer"])
    elif status == "greeting":
        st.markdown(md(r.get("message") or "Hello."))
    elif status == "no_match":
        st.info(md(r.get("message") or "No matching incidents."))
    elif status == "answer_withheld":
        st.warning(md(r.get("message") or "The answer was withheld."))
    elif status == "blocked":
        st.warning(md(r.get("message") or "This request can't be processed."))
    else:  # out_of_scope, and any status added later
        st.info(md(r.get("message") or "This request is outside what I can help with."))
    render_evidence(r.get("citations") or [])
    render_footer(turn)


def render_turn(turn: Turn) -> None:
    with st.chat_message("user", avatar=AVATAR["user"]):
        render_user(turn)
    with st.chat_message("assistant", avatar=AVATAR["assistant"]):
        render_turn_body(turn)
