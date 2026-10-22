"""Offline graph/API tests: AOSS and Bedrock are faked, so every status and the error path run without AWS."""
import pytest
from fastapi.testclient import TestClient

from daimo_retrieval.api import app
from daimo_retrieval.nodes import embed, retrieve, synthesize as syn
from daimo_retrieval.schemas import ActionStep, Cited, TriageAnswer

client = TestClient(app)
Q = {"query": "login timeouts after patch window", "request_id": "req-1"}


def _hit(inc, ctype, score):
    src = {"incident_number": inc, "chunk_id": f"{inc}#{ctype}", "chunk_type": ctype, "window": None,
           "chunk_text": "login timeouts after patch window; restarted broker", "short_description": "Login timeouts",
           "priority": "2 - High", "affected_service": "VistA", "closed_at": "2025-11-04 16:05:00",
           "related_kb_articles": ["KB1"]}
    return {"_id": "x", "_score": score, "_source": src}


def _fake_search(cos):
    def search(body, pipeline=None):
        hits = [_hit("INC1", "symptom", 0.9), _hit("INC2", "symptom", 0.5)]
        if pipeline is None:  # kNN-only call: raw faiss score = 1 + cosine
            for h in hits:
                h["_score"] = 1 + cos
        return {"hits": {"hits": hits}}
    return search


class _Raw:
    usage_metadata = {"input_tokens": 10, "output_tokens": 5}


def _llm(parsed):
    class Fake:
        calls = 0

        def invoke(self, msgs):
            Fake.calls += 1
            return {"raw": _Raw(), "parsed": parsed(Fake.calls), "parsing_error": None}
    return Fake()


GOOD = TriageAnswer(summary="Logins time out after patches [INC1].",
                    likely_cause=Cited(text="Broker stale [INC1].", citations=["INC1"]),
                    recommended_actions=[ActionStep(step="Restart broker [INC1].", citations=["INC1"])])


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(embed, "embed_text", lambda t: [0.1] * 4)
    monkeypatch.setattr(retrieve, "_search", _fake_search(0.8))
    monkeypatch.setattr(syn, "get_llm", lambda _id: _llm(lambda n: GOOD))


def test_answered():
    r = client.post("/v1/triage", json=Q).json()
    assert r["status"] == "answered" and r["answer"]["cited_incidents"] == ["INC1"]
    assert r["citations"][0]["incident_number"] == "INC1" and r["citations"][0]["cited_in_answer"] is True
    assert r["confidence"]["passed"] == 2 and r["metrics"]["tokens"] == {"input": 10, "output": 5}
    assert {"embed", "retrieve", "synthesize", "total"} <= set(r["metrics"]["latency_ms"])


def test_no_match_makes_no_llm_call(monkeypatch):
    monkeypatch.setattr(retrieve, "_search", _fake_search(0.1))
    monkeypatch.setattr(syn, "get_llm", lambda _id: pytest.fail("LLM must not be called"))
    r = client.post("/v1/triage", json=Q).json()
    assert r["status"] == "no_match" and r["answer"] is None and r["citations"] == []
    assert "synthesize" not in r["metrics"]["latency_ms"]


def test_retry_then_answered(monkeypatch):
    bad = GOOD.model_copy(update={"summary": "No citation here."})
    fake = _llm(lambda n: bad if n == 1 else GOOD)
    monkeypatch.setattr(syn, "get_llm", lambda _id: fake)
    assert client.post("/v1/triage", json=Q).json()["status"] == "answered"


def test_withheld_after_two_validation_failures(monkeypatch):
    bad = GOOD.model_copy(update={"summary": "No citation here."})
    monkeypatch.setattr(syn, "get_llm", lambda _id: _llm(lambda n: bad))
    r = client.post("/v1/triage", json=Q).json()
    assert r["status"] == "answer_withheld" and r["answer"] is None and len(r["citations"]) == 2


def test_withheld_on_insufficient_evidence(monkeypatch):
    ins = GOOD.model_copy(update={"summary": "Records do not cover this.", "insufficient_evidence": True})
    monkeypatch.setattr(syn, "get_llm", lambda _id: _llm(lambda n: ins))
    r = client.post("/v1/triage", json=Q).json()
    assert r["status"] == "answer_withheld" and "do not cover" in r["message"] and r["citations"]


def test_stage_failure_returns_clean_error(monkeypatch):
    def boom(body, pipeline=None):
        raise RuntimeError("aoss down")
    monkeypatch.setattr(retrieve, "_search", boom)
    resp = client.post("/v1/triage", json=Q)
    assert resp.status_code == 500
    assert resp.json() == {"request_id": "req-1", "error": "internal_error", "stage": "retrieve"}
