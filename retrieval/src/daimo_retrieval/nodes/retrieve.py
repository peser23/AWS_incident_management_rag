"""Hybrid retrieval (BM25 + kNN on AOSS): query builders, parallel execution, RRF fallback, collapse to incidents.

Two queries run per request: the hybrid query through the normalization pipeline (ranking) and a kNN-only query
(raw faiss scores for the confidence gate). Results join on chunk_id.
"""
from concurrent.futures import ThreadPoolExecutor

from opensearchpy.exceptions import ConnectionTimeout, TransportError

from ..clients import opensearch
from ..models import Chunk, Incident
from ..schemas import Filters
from ..settings import Settings, get_settings
from ..text import ci_tokens, normalize_query

SOURCE_FIELDS = ["incident_number", "chunk_id", "chunk_type", "window", "chunk_text", "short_description",
                 "priority", "priority_level", "category", "subcategory", "affected_service", "affected_ci",
                 "affected_system_name", "configuration_items", "assignment_group", "state", "resolution_code",
                 "technical_cause", "caused_by_change", "related_kb_articles", "closed_at", "first_note_at"]
RRF_K = 60


def build_filter_clauses(filters: Filters, fm: dict) -> list[dict]:
    ff, out = fm["filter_fields"], []
    if filters.priority_level_max is not None:
        out.append({"range": {ff["priority_level_max"]: {"lte": filters.priority_level_max}}})
    for key in ("category", "affected_service", "assignment_group"):
        values = getattr(filters, key)
        if values:
            out.append({"terms": {ff[key]: values}})
    if filters.closed_after:
        out.append({"range": {ff["closed_after"]: {"gte": filters.closed_after}}})
    return out


def build_lexical_query(query: str, filters: Filters, s: Settings) -> dict:
    fm = s.field_map
    should = [{"multi_match": {"query": normalize_query(query), "fields": fm["bm25_fields"]}}]
    tokens = ci_tokens(query, s.ci_token_min_len)
    if tokens:
        should += [{"terms": {field: tokens, "boost": boost}} for field, boost in fm["ci_term_fields"].items()]
    bool_q: dict = {"should": should, "minimum_should_match": 1}
    clauses = build_filter_clauses(filters, fm)
    if clauses:
        bool_q["filter"] = clauses
    return {"bool": bool_q}


def build_knn_query(vector: list[float], filters: Filters, s: Settings) -> dict:
    fm = s.field_map
    knn: dict = {"vector": vector, "k": s.knn_k, "rescore": {"oversample_factor": s.knn_oversample}}
    clauses = build_filter_clauses(filters, fm)
    if clauses:
        knn["filter"] = {"bool": {"filter": clauses}}
    return {"knn": {fm["vector"]: knn}}


def build_hybrid_body(query: str, vector: list[float], filters: Filters, s: Settings) -> dict:
    return {"size": s.candidate_pool, "_source": {"includes": SOURCE_FIELDS},
            "query": {"hybrid": {"queries": [build_lexical_query(query, filters, s),
                                             build_knn_query(vector, filters, s)]}}}


def build_knn_only_body(vector: list[float], filters: Filters, s: Settings) -> dict:
    return {"size": s.knn_k, "_source": {"includes": [s.field_map["chunk_id"]]},
            "query": build_knn_query(vector, filters, s)}


def _search(body: dict, pipeline: str | None = None) -> dict:
    """One retry on 5xx/429/timeouts."""
    s, client = get_settings(), opensearch()
    params = {"search_pipeline": pipeline} if pipeline else {}
    for attempt in (0, 1):
        try:
            return client.search(index=s.daimo_opensearch_index, body=body, params=params)
        except (ConnectionTimeout, TransportError) as e:
            status = getattr(e, "status_code", None)
            retryable = isinstance(e, ConnectionTimeout) or status == 429 or (isinstance(status, int) and status >= 500)
            if attempt == 1 or not retryable:
                raise
    raise RuntimeError("unreachable")


def hits_to_chunks(resp: dict) -> list[Chunk]:
    chunks = []
    for i, h in enumerate(resp["hits"]["hits"], start=1):
        src = h["_source"]
        chunks.append(Chunk(chunk_id=src["chunk_id"], chunk_type=src.get("chunk_type", ""),
                            text=src.get("chunk_text") or "", source=src, window=src.get("window"),
                            hybrid_score=h.get("_score"), rank=i))
    return chunks


def knn_scores_from(resp: dict) -> dict[str, float]:
    return {h["_source"]["chunk_id"]: h["_score"] for h in resp["hits"]["hits"]}


def rrf_fuse(lexical: list[Chunk], semantic: list[Chunk], pool: int) -> list[Chunk]:
    """Reciprocal rank fusion over chunk_id (client-side fallback and evaluation baseline)."""
    scores: dict[str, float] = {}
    by_id: dict[str, Chunk] = {}
    for ranked in (lexical, semantic):
        for pos, ch in enumerate(ranked, start=1):
            scores[ch.chunk_id] = scores.get(ch.chunk_id, 0.0) + 1.0 / (RRF_K + pos)
            by_id.setdefault(ch.chunk_id, ch)
    fused = sorted(by_id.values(), key=lambda c: scores[c.chunk_id], reverse=True)[:pool]
    for i, ch in enumerate(fused, start=1):
        ch.hybrid_score, ch.rank = scores[ch.chunk_id], i
    return fused


def collapse_to_incidents(chunks: list[Chunk], top_k: int) -> list[Incident]:
    """Group chunks by incident_number (symptom, resolution and every narrative window). Incidents are ranked by
    their best chunk's hybrid rank; only the first top_k are kept, each with all of its retrieved chunks."""
    groups: dict[str, Incident] = {}
    for ch in sorted(chunks, key=lambda c: c.rank):
        inc = groups.setdefault(ch.source["incident_number"], Incident(ch.source["incident_number"]))
        inc.chunks.append(ch)
    ranked = sorted(groups.values(), key=lambda i: i.chunks[0].rank)[:top_k]
    for pos, inc in enumerate(ranked, start=1):
        inc.rank = pos
    return ranked


def retrieve(query: str, vector: list[float], filters: Filters, top_k: int) -> tuple[list[Incident], dict[str, float], dict]:
    """Returns (candidates, knn_scores by chunk_id, debug info)."""
    s = get_settings()
    knn_body = build_knn_only_body(vector, filters, s)
    with ThreadPoolExecutor(max_workers=2) as pool:
        if s.retrieval_mode == "client_rrf":
            lex_f = pool.submit(_search, {"size": s.candidate_pool, "_source": {"includes": SOURCE_FIELDS},
                                          "query": build_lexical_query(query, filters, s)})
            sem_f = pool.submit(_search, {"size": s.knn_k, "_source": {"includes": SOURCE_FIELDS},
                                          "query": build_knn_query(vector, filters, s)})
            sem_resp = sem_f.result()
            chunks = rrf_fuse(hits_to_chunks(lex_f.result()), hits_to_chunks(sem_resp), s.candidate_pool)
            knn_scores = knn_scores_from(sem_resp)
            body = {"mode": "client_rrf"}
        else:
            body = build_hybrid_body(query, vector, filters, s)
            hyb_f = pool.submit(_search, body, s.hybrid_pipeline_name)
            knn_f = pool.submit(_search, knn_body)
            chunks, knn_scores = hits_to_chunks(hyb_f.result()), knn_scores_from(knn_f.result())
    return collapse_to_incidents(chunks, top_k), knn_scores, {"query": body}


def hybrid_retrieve(state: dict) -> dict:
    """LangGraph node: query_vector -> candidates, knn_scores."""
    s, req = get_settings(), state["request"]
    top_k = req.options.top_k or s.top_k_default
    cands, knn_scores, dbg = retrieve(req.query, state["query_vector"], req.options.filters, top_k)
    return {"candidates": cands, "knn_scores": knn_scores, "debug_query": dbg}
