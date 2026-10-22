import pytest

from daimo_retrieval.models import Chunk, Incident
from daimo_retrieval.nodes.gate import ip_score_to_cosine, match_reason, score_and_gate
from daimo_retrieval.nodes.respond import build_response
from daimo_retrieval.schemas import TriageRequest


def test_ip_score_inversion():
    assert ip_score_to_cosine(1.8) == pytest.approx(0.8)
    assert ip_score_to_cosine(1.0) == 0.0
    assert ip_score_to_cosine(1 / 1.5) == pytest.approx(-0.5)  # ip=-0.5 -> score 1/(1+0.5)


def _inc(num, knn_cos, text="login timeout patch"):
    ch = Chunk(f"{num}#symptom", "symptom", text, {"incident_number": num, "short_description": "t",
                                                    "chunk_id": f"{num}#symptom"}, rank=1)
    return Incident(num, [ch]), {ch.chunk_id: 1 + knn_cos}


def _state(inc, knn, query="login timeout patch", **opts):
    req = TriageRequest(request_id="r", query=query, options=opts)
    return {"request": req, "candidates": [inc], "knn_scores": knn}


def test_boundaries():
    # cos at ceil with full lexical coverage -> conf 1.0; at floor with none -> 0
    inc, knn = _inc("A", 0.75)
    out = score_and_gate(_state(inc, knn))
    assert inc.confidence == pytest.approx(1.0) and out["passed"] == [inc]
    inc, knn = _inc("B", 0.30, text="unrelated words")
    out = score_and_gate(_state(inc, knn))
    assert inc.confidence == pytest.approx(0.0, abs=1e-9) and out["passed"] == [] and out["withheld_count"] == 1


def test_keyword_only_cannot_pass():
    inc, _ = _inc("C", 0.0)
    out = score_and_gate(_state(inc, {}))  # no kNN score, full lexical coverage
    assert inc.confidence == pytest.approx(0.2) and out["passed"] == []


def test_threshold_override_ignored_when_disabled():
    inc, knn = _inc("D", 0.5)
    assert score_and_gate(_state(inc, knn, min_confidence=0.0))["threshold"] == 0.55


def test_match_reason():
    assert match_reason(0.9, 0.9) == "semantic+keyword"
    assert match_reason(0.9, 0.1) == "semantic"
    assert match_reason(0.1, 0.9) == "keyword"


def test_no_match_response():
    inc, knn = _inc("E", 0.1, text="unrelated")
    st = _state(inc, knn)
    st.update(score_and_gate(st))
    out = build_response(st)
    r = out["response"]
    assert out["status"] == "no_match" and r.answer is None and r.citations == []
    assert r.confidence.passed == 0 and "threshold 0.55" in r.message
