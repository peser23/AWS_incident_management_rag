"""Final response builder: citation cards (from OpenSearch metadata, never LLM text) and JSON for every status."""
from ..models import Incident
from ..schemas import (CitationCard, Confidence, Metrics, ModelInfo, TriageAnswer, TriageResponse)
from ..settings import get_settings

SNIPPET_CHARS = 240
NO_MATCH_MSG = ("No historical incident matched this query with enough confidence (best score {best:.2f}; "
                "threshold {thr:.2f}). Try adding the affected system, error text, or symptom, or escalate "
                "per the standard runbook.")


def _snippet(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= SNIPPET_CHARS else text[:SNIPPET_CHARS - 1].rstrip() + "…"


def build_card(inc: Incident, cited: set[str]) -> CitationCard:
    s, m = get_settings(), inc.meta
    chunk = inc.best_chunk
    return CitationCard(
        incident_number=inc.incident_number, title=m.get("short_description"), priority=m.get("priority"),
        affected_service=m.get("affected_service"), affected_ci=m.get("affected_ci"), category=m.get("category"),
        state=m.get("state"), resolution_code=m.get("resolution_code"), closed_at=m.get("closed_at"),
        related_kb_articles=m.get("related_kb_articles") or [], snippet=_snippet(chunk.text),
        confidence=round(inc.confidence, 4), confidence_label=s.label_for(inc.confidence),
        match_reason=inc.match_reason, matched_chunk_type=chunk.chunk_type,
        cited_in_answer=inc.incident_number in cited,
        url=s.sn_link_template.format(instance=s.sn_instance, incident_number=inc.incident_number))


def build_response(state: dict) -> dict:
    s, req = get_settings(), state["request"]
    passed: list[Incident] = state.get("passed") or []
    answer: TriageAnswer | None = state.get("answer")
    threshold = state["threshold"]
    withheld = state.get("withheld_count", 0)
    timings = dict(state.get("timings", {}))
    timings["total"] = sum(v for k, v in timings.items() if k != "total")

    if not passed:
        status, message = "no_match", None
        best = max((i.confidence for i in state.get("candidates", [])), default=0.0)
        message = NO_MATCH_MSG.format(best=best, thr=threshold)
        overall, answer = best, None
    else:
        overall = max(i.confidence for i in passed)
        if state.get("status") == "answer_withheld" or answer is None or answer.insufficient_evidence:
            status, message = "answer_withheld", state.get("message") or (
                answer.summary if answer and answer.insufficient_evidence else
                "The generated answer could not be validated against the retrieved incidents.")
            answer = None
        else:
            status, message = "answered", None

    cited = set(answer.cited_incidents) if answer else set()
    cards = sorted((build_card(i, cited) for i in passed),
                   key=lambda c: (not c.cited_in_answer, -c.confidence))
    resp = TriageResponse(
        request_id=req.request_id, status=status, answer=answer, message=message,
        confidence=Confidence(overall=round(overall, 4), label=s.label_for(overall), threshold=threshold,
                              passed=len(passed), withheld=withheld),
        citations=cards, model=ModelInfo(llm=s.model_id if status != "no_match" else None, embedding=s.embed_model_id),
        metrics=Metrics(latency_ms=timings, tokens=state.get("tokens"), est_cost_usd=state.get("est_cost_usd")),
        config_version=s.config_version)
    if req.options.include_debug:
        resp.debug = {"query": state.get("debug_query"), "timings_ms": timings,
                      "prompt_version": state.get("prompt_version"),
                      "invalid_citations": state.get("invalid_citations", 0),
                      "candidates": [{"incident_number": i.incident_number, "rank": i.rank,
                                      "confidence": round(i.confidence, 4), "match_reason": i.match_reason,
                                      "chunks": [{"chunk_id": c.chunk_id, "hybrid_score": c.hybrid_score,
                                                  "knn_score": (state.get("knn_scores") or {}).get(c.chunk_id),
                                                  "confidence": round(c.confidence, 4)} for c in i.chunks]}
                                     for i in state.get("candidates", [])]}
    return {"status": status, "response": resp}
