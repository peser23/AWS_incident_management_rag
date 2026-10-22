import pytest
import requests

from daimo_web import client
from daimo_web.config import load
from conftest import FakeResponse

pytestmark = pytest.mark.real_client


@pytest.fixture
def capture(monkeypatch):
    """Patch requests.post; returns a dict that records the call and lets a test choose the reply."""
    calls = {"reply": FakeResponse(200, {"status": "answered"})}

    def fake_post(url, json, timeout):
        calls.update(url=url, json=json, timeout=timeout)
        reply = calls["reply"]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(client.requests, "post", fake_post)
    return calls


def test_payload_shape(capture, monkeypatch):
    monkeypatch.setenv("DAIMO_RETRIEVAL_URL", "http://svc:9000/")
    client.triage("vista login timeout", session_id="st-1", user_id="op1", options={"top_k": 3})
    assert capture["url"] == "http://svc:9000/v1/triage"
    body = capture["json"]
    assert body["query"] == "vista login timeout" and body["session_id"] == "st-1"
    assert body["user"] == {"id": "op1"} and body["options"] == {"top_k": 3}
    assert body["request_id"]
    assert capture["timeout"] == (5, 45)


def test_payload_validates_against_service_schema(capture):
    from daimo_retrieval.schemas import TriageRequest
    client.triage("vista login timeout", session_id=None, user_id=None,
                  options={"top_k": 3, "filters": {"category": ["Software"]}})
    TriageRequest.model_validate(capture["json"])  # raises if the UI sends anything the contract forbids
    assert "user" not in capture["json"] and "session_id" not in capture["json"]


def test_success_returns_body(capture, fixture_response):
    capture["reply"] = FakeResponse(200, fixture_response("answered"))
    assert client.triage("abc", session_id=None, user_id=None)["status"] == "answered"


@pytest.mark.parametrize("reply,kind", [
    (FakeResponse(422, {"error": "invalid_request", "detail": [{"loc": ["body", "query"], "msg": "too short"}]}),
     "invalid_request"),
    (FakeResponse(422, {"detail": "top_k must be 1..10"}), "invalid_request"),
    (FakeResponse(503, {"request_id": "srv-1", "error": "upstream_failure", "stage": "synthesize"}), "busy"),
    (FakeResponse(500, {"request_id": "srv-1", "error": "internal_error", "stage": None}), "internal"),
    (FakeResponse(502, None, "Bad gateway"), "internal"),
    (requests.ReadTimeout(), "unreachable"),
    (requests.ConnectionError(), "unreachable"),
])
def test_error_mapping(capture, reply, kind):
    capture["reply"] = reply
    with pytest.raises(client.ClientError) as e:
        client.triage("abc", session_id=None, user_id=None)
    assert e.value.kind == kind
    assert e.value.request_id  # always something the operator can quote


def test_422_detail_is_readable(capture):
    capture["reply"] = FakeResponse(422, {"error": "invalid_request",
                                          "detail": [{"loc": ["body", "query"], "msg": "too short"}]})
    with pytest.raises(client.ClientError) as e:
        client.triage("abc", session_id=None, user_id=None)
    assert "query: too short" in e.value.message


def test_server_request_id_preferred(capture):
    capture["reply"] = FakeResponse(503, {"request_id": "srv-1", "error": "upstream_failure"})
    with pytest.raises(client.ClientError) as e:
        client.triage("abc", session_id=None, user_id=None)
    assert e.value.request_id == "srv-1"


def test_ready_never_raises(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError()
    monkeypatch.setattr(client.requests, "get", boom)
    assert client.ready(load())["ready"] is False


def test_ready_reports_failed_checks(monkeypatch):
    body = {"ready": False, "checks": {"index": {"ok": False, "detail": "timeout"}}}
    monkeypatch.setattr(client.requests, "get", lambda *a, **k: FakeResponse(503, body))
    r = client.ready(load())
    assert r["ready"] is False and r["checks"]["index"]["ok"] is False


def test_facets_error_mapping(monkeypatch):
    real = client
    monkeypatch.setattr(real.requests, "get", lambda *a, **k: FakeResponse(503, {"error": "upstream_failure"}))
    with pytest.raises(real.ClientError) as e:
        real.facets(load())
    assert e.value.kind == "busy"

    def boom(*a, **k):
        raise requests.ConnectionError()
    monkeypatch.setattr(real.requests, "get", boom)
    with pytest.raises(real.ClientError) as e:
        real.facets(load())
    assert e.value.kind == "unreachable"
