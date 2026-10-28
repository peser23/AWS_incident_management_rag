"""Rendering: escaping, every status, evidence cards, and the chat flow with the client mocked."""
import copy
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from daimo_web import client
from daimo_web.render import cited_md, md

APP = str(Path(__file__).resolve().parents[1] / "app.py")


def _render(turn_kwargs: dict) -> AppTest:
    """Render one Turn in isolation."""
    def script(turn_kwargs):
        from daimo_web.render import render_turn
        from daimo_web.state import Turn
        render_turn(Turn(**turn_kwargs))
    return AppTest.from_function(script, kwargs={"turn_kwargs": turn_kwargs}).run()


def _all_text(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [e.value for e in (*at.info, *at.warning, *at.error)] + [x.label for x in at.expander]
    return "\n".join(parts)


# --- escaping ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "![x](https://evil.example/p.png)", "[click](https://evil.example)", "<script>alert(1)</script>",
    "$x^2$", ":red[alert] :material/home:", "# heading", "**bold**", "`code`", "a\\b",
])
def test_md_escapes_markup(raw):
    out = md(raw)
    assert "](" not in out.replace("\\](", "").replace("\\]\\(", "")
    for ch in "[]()<>$:#*`!":
        assert all(out[i - 1] == "\\" for i, c in enumerate(out) if c == ch), (raw, out)


def test_md_handles_none():
    assert md(None) == ""


def test_cited_md_badges_only_incident_refs():
    out = cited_md("Restart the broker [INC41220398]. See [click](http://x) and [INC12].")
    assert ":blue-badge[INC41220398]" in out
    assert "\\[click\\]\\(http\\:" in out and "\\[INC12\\]" in out  # not an incident number → left escaped


# --- one turn per status ----------------------------------------------------------------------------------

def test_answered(fixture_response):
    at = _render({"query": "vista login timeout", "filters": {"top_k": 5, "priority_max": 2},
                  "response": fixture_response("answered")})
    text = _all_text(at)
    assert len(at.chat_message) == 2
    assert "High confidence · 0.82" in text
    assert "Threshold 0.55 · 1 incident(s) passed · 4 below threshold" in text
    assert "RPC broker listener" in text and ":blue-badge[INC41220398]" in text
    assert "**Recommended actions**" in text and "Caveats" in text
    assert "Filters: Priority ≤ 2 · Top 5" in text
    assert len(at.expander) == 1 and "✓ cited" in at.expander[0].label
    assert "request mock\\-1" in text and "3.5 s" in text


def test_no_match(fixture_response):
    at = _render({"query": "printer jam", "filters": {}, "response": fixture_response("no_match")})
    assert len(at.info) == 1 and "No historical incident matched" in at.info[0].value
    assert "Low confidence · 0.31" in _all_text(at)
    assert len(at.expander) == 0


def test_withheld(fixture_response):
    at = _render({"query": "vista login timeout", "filters": {}, "response": fixture_response("answer_withheld")})
    assert len(at.warning) == 1 and "insufficient evidence" in at.warning[0].value
    assert len(at.expander) == 1  # evidence still shown


def _guard_response(status, message, **extra):
    return {"request_id": "g-1", "status": status, "answer": None, "message": message, "notice": None,
            "confidence": None, "citations": [], "model": None, "metrics": {"latency_ms": {"total": 0}},
            "config_version": "x", "guard": {"intent": status, "layer": "rule", "reason_code": None, "redacted": []},
            **extra}


def test_greeting_has_no_confidence_or_box(fixture_response):
    at = _render({"query": "hi", "filters": {}, "response": fixture_response("greeting")})
    text = _all_text(at)
    assert "incident triage assistant" in text and "confidence" not in text.lower()
    assert not (at.info or at.warning or at.error or at.expander)


def test_out_of_scope_is_info(fixture_response):
    at = _render({"query": "joke", "filters": {}, "response": fixture_response("out_of_scope")})
    assert len(at.info) == 1 and "IT incident triage" in at.info[0].value and "confidence" not in _all_text(at).lower()


def test_blocked_is_warning(fixture_response):
    at = _render({"query": "x", "filters": {}, "response": fixture_response("blocked")})
    assert len(at.warning) == 1 and "can't process" in at.warning[0].value


def test_unknown_future_status_falls_back_to_info():
    at = _render({"query": "q", "filters": {}, "response": _guard_response("brand_new", "Something new.")})
    assert len(at.info) == 1 and "Something new" in at.info[0].value


def test_redaction_notice_shown_above_answer(fixture_response):
    at = _render({"query": "q", "filters": {}, "response": fixture_response("answered_redacted")})
    assert "🔒 Sensitive personal data" in _all_text(at)
    values = [getattr(e, "value", "") for e in at.chat_message[1].children.values()]  # document order
    first = next(i for i, v in enumerate(values) if "Sensitive personal data" in v)
    badge = next(i for i, v in enumerate(values) if "High confidence" in v)
    assert first < badge


def test_notice_text_is_escaped():
    r = _guard_response("greeting", "hello", notice="[click](https://evil.example)")
    assert "\\[click\\]" in _all_text(_render({"query": "q", "filters": {}, "response": r}))


def test_error_turn():
    at = _render({"query": "x" * 5, "filters": {},
                  "error": {"kind": "busy", "message": "The service is busy.", "request_id": "r-9"}})
    assert len(at.error) == 1 and "busy" in at.error[0].value
    assert "request r\\-9" in _all_text(at)


def test_evidence_order_and_injection(fixture_response):
    r = fixture_response("answered")
    evil = copy.deepcopy(r["citations"][0])
    evil.update(incident_number="INC00000001", title="[click](https://evil.example)", confidence=0.9,
                cited_in_answer=False, snippet="![x](https://evil.example/p.png) <script>alert(1)</script> $x$",
                url="javascript:alert(1)")
    r["citations"].append(evil)
    at = _render({"query": "q" * 5, "filters": {}, "response": r})
    labels = [e.label for e in at.expander]
    assert labels[0].startswith("INC41220398")  # cited first, even with lower confidence
    assert "\\[click\\]\\(https\\:" in labels[1]
    body = "\n".join(m.value for m in at.markdown)
    assert "\\!\\[x\\]\\(https\\:" in body and "\\<script\\>" in body and "\\$x\\$" in body
    assert all("javascript" not in str(lb.proto) for lb in at.get("link_button"))
    assert len(at.get("link_button")) == 1  # only the https link


# --- chat flow ----------------------------------------------------------------------------------------------

@pytest.fixture
def mocked(monkeypatch, fixture_response):
    calls = []
    replies = iter(["answered", "no_match", "answer_withheld"])

    def fake_triage(query, **kw):
        calls.append({"query": query, **kw})
        return fixture_response(next(replies))

    monkeypatch.setattr(client, "triage", fake_triage)
    return calls


def test_conversation_threads_and_persists(mocked):
    at = AppTest.from_file(APP).run()
    assert at.title[0].value == "DAIMO Incident management"
    at.chat_input[0].set_value("vista login timeout").run()
    at.chat_input[0].set_value("printer jam on floor 3").run()
    assert [m.name for m in at.chat_message] == ["user", "assistant", "user", "assistant"]
    at.run()  # plain rerun: history is redrawn from session state
    assert len(at.chat_message) == 4
    assert [t.status for t in at.session_state.turns] == ["answered", "no_match"]
    assert mocked[0]["session_id"] == mocked[1]["session_id"] == at.session_state.session_id
    assert mocked[0]["options"] == {"top_k": 5}


def test_blank_query_not_sent(mocked):
    at = AppTest.from_file(APP).run()
    at.chat_input[0].set_value("   ").run()
    assert mocked == [] and len(at.error) == 1 and "enter a message" in at.error[0].value
    assert at.session_state.turns[0].status == "error"


def test_two_character_greeting_is_sent(mocked):
    at = AppTest.from_file(APP).run()
    at.chat_input[0].set_value("hi").run()
    assert [c["query"] for c in mocked] == ["hi"] and at.session_state.turns[0].status == "answered"


def test_client_error_becomes_turn(monkeypatch):
    def boom(query, **kw):
        raise client.ClientError("unreachable", "Cannot reach the retrieval service.", "rid-1")
    monkeypatch.setattr(client, "triage", boom)
    at = AppTest.from_file(APP).run()
    at.chat_input[0].set_value("vista login timeout").run()
    assert "Cannot reach" in at.error[0].value
    at.run()
    assert len(at.error) == 1 and at.session_state.turns[0].request_id == "rid-1"
