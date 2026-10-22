"""Readiness banner, error turns, the pending-turn guard, service URL, and no AWS SDK in the UI."""
import ast
from pathlib import Path

import pytest
from streamlit.runtime.scriptrunner_utils.exceptions import StopException
from streamlit.testing.v1 import AppTest

from daimo_web import client
from daimo_web.state import Turn, default_filters

WEB = Path(__file__).resolve().parents[1]
APP = str(WEB / "app.py")


def _banner(at):
    return [e.value for e in at.error if "not ready" in e.value]


# --- readiness ----------------------------------------------------------------------------------------------

def test_no_banner_when_ready():
    at = AppTest.from_file(APP).run()
    assert _banner(at) == [] and any("● ready" in c.value for c in at.sidebar.caption)


def test_banner_names_failing_checks_and_chat_stays_enabled(ready_stub):
    ready_stub.result = {"ready": False, "checks": {"index": {"ok": False, "detail": "ConnectionTimeout"},
                                                    "embedding": {"ok": True, "detail": 1024}}}
    at = AppTest.from_file(APP).run()
    [banner] = _banner(at)
    assert "index" in banner and "ConnectionTimeout" in banner and "embedding" not in banner
    assert any("not ready" in c.value for c in at.sidebar.caption)
    assert not at.chat_input[0].disabled


def test_readiness_cached_across_reruns(ready_stub):
    at = AppTest.from_file(APP).run()
    at.run(), at.run()
    assert ready_stub.calls == 1


# --- pending turn -------------------------------------------------------------------------------------------

def test_interrupted_request_keeps_turn_and_disables_input(monkeypatch, fixture_response):
    """A run stopped mid-request (e.g. a sidebar click) must not lose the question or allow a second one."""
    def interrupted(query, **kw):
        raise StopException()
    monkeypatch.setattr(client, "triage", interrupted)
    at = AppTest.from_file(APP).run()
    at.chat_input[0].set_value("vista login timeout").run()
    assert at.session_state.pending_turn is not None and at.session_state.turns == []
    assert at.chat_input[0].disabled

    sent = []
    monkeypatch.setattr(client, "triage", lambda q, **kw: sent.append(kw) or fixture_response("answered"))
    at.multiselect(key="f_service").set_value(["VistA"]).run()  # the interrupting interaction
    assert len(sent) == 1 and "filters" not in sent[0]["options"]  # resent with the filters it was asked with
    assert [t.query for t in at.session_state.turns] == ["vista login timeout"]
    assert at.session_state.pending_turn is None and not at.chat_input[0].disabled


def test_pending_turn_resent_once(monkeypatch, fixture_response):
    sent = []
    monkeypatch.setattr(client, "triage", lambda q, **kw: sent.append(q) or fixture_response("answered"))
    at = AppTest.from_file(APP)
    at.session_state["pending_turn"] = Turn("left over question", {**default_filters(), "top_k": 2})
    at.run()
    at.run()
    assert sent == ["left over question"] and len(at.session_state.turns) == 1


def test_new_conversation_drops_pending():
    from daimo_web.state import new_conversation
    ss = {"pending_turn": Turn("x", {}), "turns": [], "session_id": "s"}
    new_conversation(ss)
    assert ss["pending_turn"] is None


# --- error turns --------------------------------------------------------------------------------------------

def test_unexpected_exception_becomes_error_turn(monkeypatch):
    def broken(query, **kw):
        raise KeyError("status")
    monkeypatch.setattr(client, "triage", broken)
    at = AppTest.from_file(APP).run()
    at.chat_input[0].set_value("vista login timeout").run()
    assert "Unexpected error" in at.error[0].value and "KeyError" in at.error[0].value
    assert at.session_state.pending_turn is None and not at.chat_input[0].disabled


def test_busy_turn_shows_retry_and_request_id(monkeypatch):
    def busy(query, **kw):
        raise client.ClientError("busy", "The service is busy or an upstream model timed out. Try again.", "srv-7")
    monkeypatch.setattr(client, "triage", busy)
    at = AppTest.from_file(APP).run()
    at.chat_input[0].set_value("vista login timeout").run()
    assert "Try again" in at.error[0].value
    assert any("request srv\\-7" in c.value for c in at.caption)


def test_too_long_query_rejected_in_ui(monkeypatch):
    sent = []
    monkeypatch.setattr(client, "triage", lambda q, **kw: sent.append(q))
    at = AppTest.from_file(APP).run()
    at.chat_input[0].set_value("x" * 1001).run()
    assert sent == [] and "under 1000 characters" in at.error[0].value


# --- requirement 5: the UI talks only to the retrieval service ---------------------------------------------

def test_calls_configured_retrieval_url(monkeypatch, fixture_response):
    from conftest import FakeResponse
    monkeypatch.setenv("DAIMO_RETRIEVAL_URL", "http://retrieval.test:8123")
    urls = []
    monkeypatch.setattr(client.requests, "post",
                        lambda url, **kw: urls.append(url) or FakeResponse(200, fixture_response("answered")))
    at = AppTest.from_file(APP).run()
    at.chat_input[0].set_value("vista login timeout").run()
    assert urls == ["http://retrieval.test:8123/v1/triage"]
    assert at.session_state.turns[0].status == "answered"


@pytest.mark.parametrize("path", sorted(p for p in [WEB / "app.py", *(WEB / "daimo_web").glob("*.py")]),
                         ids=lambda p: p.name)
def test_no_aws_sdk_imports(path):
    tree = ast.parse(path.read_text())
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not mods & {"boto3", "botocore", "opensearchpy", "langchain_aws", "daimo_retrieval"}
