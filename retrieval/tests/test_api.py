from fastapi.testclient import TestClient

from daimo_retrieval.api import app
from daimo_retrieval.settings import Settings

c = TestClient(app)
Q = {"query": "VistA login timeouts after patch window"}


def test_healthz():
    assert c.get("/healthz").json()["status"] == "ok"


def test_query_length_bounds():
    assert c.post("/v1/triage", json={"query": ""}).status_code == 422
    assert c.post("/v1/triage", json={"query": "x" * 1001}).status_code == 422


def test_unknown_filter_key_rejected():
    r = c.post("/v1/triage", json={**Q, "options": {"filters": {"bogus": 1}}})
    assert r.status_code == 422


def test_option_gates():
    assert c.post("/v1/triage", json={**Q, "options": {"top_k": 99}}).status_code == 422
    assert c.post("/v1/triage", json={**Q, "options": {"model_id": "global.x"}}).status_code == 422
    assert c.post("/v1/triage", json={**Q, "options": {"include_debug": True}}).status_code == 422
    assert c.post("/v1/triage", json={**Q, "options": {"min_confidence": 0.1}}).status_code == 422


def test_label_bands():
    s = Settings()
    assert [s.label_for(x) for x in (0.8, 0.6, 0.3)] == ["high", "medium", "low"]
