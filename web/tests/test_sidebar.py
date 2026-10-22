"""Sidebar filters: values come from the service (or the fallback), persist across reruns, are sent with the next
question, and are shown on each turn and above the conversation."""
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from daimo_web import client
from daimo_web.render import md

APP = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture
def calls(monkeypatch, fixture_response):
    sent = []

    def fake_triage(query, **kw):
        sent.append({"query": query, **kw})
        return fixture_response("answered")

    monkeypatch.setattr(client, "triage", fake_triage)
    return sent


def _btn(at, label):
    return next(b for b in at.button if b.label == label)


def _active(at) -> str:
    return next(c.value for c in at.caption if c.value.startswith("Active filters"))


def _set_filters(at):
    at.selectbox(key="f_priority").set_value(2)
    at.multiselect(key="f_service").set_value(["VistA"])
    at.multiselect(key="f_category").set_value(["Software"])
    at.date_input(key="f_closed_after").set_value(date(2024, 1, 31))
    at.slider(key="f_top_k").set_value(3)
    return at.run()


def test_facets_fill_options():
    at = AppTest.from_file(APP).run()
    assert at.multiselect(key="f_service").options == ["Member Profile", "VistA"]
    assert at.multiselect(key="f_group").options == ["Identity Registry", "Member Profile"]
    assert not any("built-in list" in c.value for c in at.caption)


def test_fallback_when_facets_fail(facets_stub):
    facets_stub.fail = True
    at = AppTest.from_file(APP).run()
    assert "Benefits Determination" in at.multiselect(key="f_group").options  # from web/facets.json
    assert any("built-in list" in c.value for c in at.caption)


def test_facets_cached_across_reruns(facets_stub):
    at = AppTest.from_file(APP).run()
    at.run(), at.run()
    assert facets_stub.calls == 1


def test_filters_sent_shown_and_persisted(calls):
    at = _set_filters(AppTest.from_file(APP).run())
    at.chat_input[0].set_value("vista login timeout").run()
    assert calls[0]["options"] == {"top_k": 3, "filters": {"priority_level_max": 2, "category": ["Software"],
                                                           "affected_service": ["VistA"], "closed_after": "2024-01-31"}}
    turn_chips = next(c.value for c in at.caption if c.value.startswith("Filters:"))
    for chip in ("Priority ≤ 2", "Category: Software", "Service: VistA", "Closed after 2024-01-31", "Top 3"):
        assert md(chip) in turn_chips and md(chip) in _active(at)
    at.run()  # rerun: widgets keep their values
    assert at.selectbox(key="f_priority").value == 2 and at.multiselect(key="f_service").value == ["VistA"]
    assert at.date_input(key="f_closed_after").value == date(2024, 1, 31) and at.slider(key="f_top_k").value == 3


def test_filter_change_only_affects_next_turn(calls):
    at = _set_filters(AppTest.from_file(APP).run())
    at.chat_input[0].set_value("first question").run()
    at.multiselect(key="f_service").set_value(["Member Profile"]).run()
    at.chat_input[0].set_value("second question").run()
    assert calls[1]["options"]["filters"]["affected_service"] == ["Member Profile"]
    t1, t2 = at.session_state.turns
    assert t1.filters["affected_service"] == ["VistA"] and t2.filters["affected_service"] == ["Member Profile"]
    chips = [c.value for c in at.caption if c.value.startswith("Filters:")]
    assert md("Service: VistA") in chips[0] and md("Service: Member Profile") in chips[1]


def test_reset_filters_keeps_conversation(calls):
    at = _set_filters(AppTest.from_file(APP).run())
    at.chat_input[0].set_value("vista login timeout").run()
    _btn(at, "Reset filters").click().run()
    assert at.selectbox(key="f_priority").value is None and at.multiselect(key="f_service").value == []
    assert at.date_input(key="f_closed_after").value is None and at.slider(key="f_top_k").value == 5
    assert _active(at) == "Active filters: Top 5"
    assert len(at.session_state.turns) == 1
    at.chat_input[0].set_value("after reset").run()
    assert calls[1]["options"] == {"top_k": 5}


def test_new_conversation_keeps_filters(calls):
    at = _set_filters(AppTest.from_file(APP).run())
    at.chat_input[0].set_value("vista login timeout").run()
    sid = at.session_state.session_id
    _btn(at, "New conversation").click().run()
    assert at.session_state.turns == [] and len(at.chat_message) == 0
    assert at.session_state.session_id != sid
    assert at.multiselect(key="f_service").value == ["VistA"] and md("Service: VistA") in _active(at)


def test_selected_value_missing_from_facets_is_kept(calls):
    at = AppTest.from_file(APP)
    at.session_state["filters"] = {"priority_max": None, "category": [], "affected_service": ["Retired Service"],
                                   "assignment_group": [], "closed_after": None, "top_k": 5}
    at.run()
    assert "Retired Service" in at.multiselect(key="f_service").options
    at.chat_input[0].set_value("vista login timeout").run()
    assert calls[0]["options"]["filters"]["affected_service"] == ["Retired Service"]
