"""Confidence score and gate.

The hybrid score ranks; the gate needs an absolute measure, so each chunk gets
    conf = (1 - w) * clip((cos - c_floor) / (c_ceil - c_floor), 0, 1) + w * lexical_coverage
An incident's confidence is the max over its chunks. A chunk with no kNN score has s_sem = 0.
"""
from ..settings import get_settings
from ..text import content_terms, lexical_coverage


def ip_score_to_cosine(score: float) -> float:
    """Invert OpenSearch's faiss innerproduct scoring; equals cosine for unit vectors."""
    return score - 1.0 if score >= 1.0 else 1.0 - 1.0 / score


def semantic_score(raw: float | None, floor: float, ceil: float) -> float:
    if raw is None:
        return 0.0
    return min(max((ip_score_to_cosine(raw) - floor) / (ceil - floor), 0.0), 1.0)


def match_reason(s_sem: float, lex: float) -> str:
    """semantic / keyword / semantic+keyword; when neither signal reaches 0.5 the stronger one is named."""
    sem, kw = s_sem >= 0.5, lex >= 0.5
    if sem and kw:
        return "semantic+keyword"
    if sem or (not kw and s_sem >= lex):
        return "semantic"
    return "keyword"


def score_and_gate(state: dict) -> dict:
    s, req = get_settings(), state["request"]
    terms = content_terms(req.query)
    override = req.options.min_confidence
    threshold = override if (s.allow_threshold_override and override is not None) else s.min_confidence
    w = s.conf_lexical_weight
    for inc in state["candidates"]:
        for ch in inc.chunks:
            s_sem = semantic_score(state["knn_scores"].get(ch.chunk_id), s.conf_cos_floor, s.conf_cos_ceil)
            lex = lexical_coverage(terms, ch.searchable_text)
            ch.confidence = (1 - w) * s_sem + w * lex
            ch.match_reason = match_reason(s_sem, lex)
        best = max(inc.chunks, key=lambda c: c.confidence)
        inc.confidence, inc.best_chunk, inc.match_reason = best.confidence, best, best.match_reason
    passed = [i for i in state["candidates"] if i.confidence >= threshold]
    return {"passed": passed, "withheld_count": len(state["candidates"]) - len(passed), "threshold": threshold}
