import pytest
from fastapi.testclient import TestClient

from daimo_retrieval import api, clients
from daimo_retrieval.api import app

c = TestClient(app)


def _aggs(**buckets):
    return {"aggregations": {k: {"buckets": [{"key": v, "doc_count": 1} for v in vals]} for k, vals in buckets.items()}}


class FakeOS:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def search(self, index, body):
        self.calls.append(body)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture
def fake(monkeypatch):
    api._facets_cache.update(at=0.0, value=None)
    holder = FakeOS(_aggs(category=["Software", "Service"], affected_service=["VistA"],
                          assignment_group=["Member Profile", " "], priority_levels=[4, 2, 3]))
    monkeypatch.setattr(clients, "opensearch", lambda: holder)
    yield holder
    api._facets_cache.update(at=0.0, value=None)


def test_shape_sorted_and_blank_dropped(fake):
    r = c.get("/v1/facets")
    assert r.status_code == 200
    assert r.json() == {"category": ["Service", "Software"], "affected_service": ["VistA"],
                        "assignment_group": ["Member Profile"], "priority_levels": [2, 3, 4]}
    body = fake.calls[0]
    assert body["size"] == 0 and body["aggs"]["category"]["terms"]["field"] == "category"
    assert body["aggs"]["priority_levels"]["terms"]["field"] == "priority_level"


def test_cached_within_ttl(fake):
    c.get("/v1/facets"), c.get("/v1/facets")
    assert len(fake.calls) == 1


def test_refresh_after_ttl(fake):
    c.get("/v1/facets")
    api._facets_cache["at"] -= 10_000
    c.get("/v1/facets")
    assert len(fake.calls) == 2


def test_failure_without_cache_is_503(fake):
    fake.reply = RuntimeError("aoss down")
    r = c.get("/v1/facets")
    assert r.status_code == 503 and r.json()["error"] == "upstream_failure" and r.json()["stage"] == "facets"


def test_failure_serves_stale(fake):
    first = c.get("/v1/facets").json()
    api._facets_cache["at"] -= 10_000
    fake.reply = RuntimeError("aoss down")
    assert c.get("/v1/facets").json() == first
