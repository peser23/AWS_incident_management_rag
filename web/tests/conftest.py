import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[2] / "retrieval" / "tests" / "fixtures"


@pytest.fixture
def fixture_response():
    """Load a TriageResponse fixture shared with the retrieval service: answered | no_match | answer_withheld."""
    return lambda name: json.loads((FIXTURES / f"{name}.json").read_text())


class FakeResponse:
    def __init__(self, status_code: int, body=None, text: str = ""):
        self.status_code, self._body, self.text, self.reason = status_code, body, text, "reason"

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


LIVE_FACETS = {"category": ["Service", "Software"], "affected_service": ["Member Profile", "VistA"],
               "assignment_group": ["Identity Registry", "Member Profile"], "priority_levels": [2, 3, 4]}


@pytest.fixture(autouse=True)
def facets_stub(request, monkeypatch):
    """No test reaches the network for filter values. Tests can set .fail = True to exercise the fallback.
    Modules marked `pytestmark = pytest.mark.real_client` test the real functions instead."""
    import streamlit as st
    from daimo_web import client
    if request.node.get_closest_marker("real_client"):
        yield None
        return

    class Stub:
        fail, calls = False, 0

        def __call__(self, cfg=None):
            self.calls += 1
            if self.fail:
                raise client.ClientError("unreachable", "down")
            return LIVE_FACETS

    stub = Stub()
    monkeypatch.setattr(client, "facets", stub)
    st.cache_data.clear()
    yield stub
    st.cache_data.clear()


@pytest.fixture(autouse=True)
def ready_stub(request, monkeypatch):
    """Service reported ready unless a test sets .result."""
    from daimo_web import client
    if request.node.get_closest_marker("real_client"):
        return None

    class Stub:
        result = {"ready": True, "checks": {}}
        calls = 0

        def __call__(self, cfg=None):
            self.calls += 1
            return self.result

    stub = Stub()
    monkeypatch.setattr(client, "ready", stub)
    return stub
