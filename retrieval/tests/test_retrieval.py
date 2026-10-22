from daimo_retrieval.models import Chunk
from daimo_retrieval.nodes.retrieve import (build_filter_clauses, build_hybrid_body, build_knn_only_body,
                                            collapse_to_incidents, rrf_fuse)
from daimo_retrieval.schemas import Filters
from daimo_retrieval.settings import Settings
from daimo_retrieval.text import ci_tokens, content_terms, lexical_coverage, normalize_query

S = Settings()
F = Filters(priority_level_max=2, category=["Software"], closed_after="2025-01-01")
VEC = [0.1] * 4


def _ch(cid, inc, rank, ctype="symptom"):
    return Chunk(cid, ctype, "t", {"incident_number": inc, "chunk_id": cid}, rank=rank)


def test_filters_in_all_queries():
    clauses = build_filter_clauses(F, S.field_map)
    assert len(clauses) == 3
    body = build_hybrid_body("VistA login", VEC, F, S)
    lex, knn = body["query"]["hybrid"]["queries"]
    assert lex["bool"]["filter"] == clauses
    assert knn["knn"]["embedding"]["filter"]["bool"]["filter"] == clauses
    assert build_knn_only_body(VEC, F, S)["query"]["knn"]["embedding"]["filter"]["bool"]["filter"] == clauses
    assert knn["knn"]["embedding"]["rescore"]["oversample_factor"] == S.knn_oversample


def test_no_filters_omits_filter_keys():
    lex, knn = build_hybrid_body("login problem", VEC, Filters(), S)["query"]["hybrid"]["queries"]
    assert "filter" not in lex["bool"] and "filter" not in knn["knn"]["embedding"]


def test_ci_terms_only_when_tokens():
    plain = build_hybrid_body("users cannot log in", VEC, Filters(), S)["query"]["hybrid"]["queries"][0]
    assert len(plain["bool"]["should"]) == 1
    ci = build_hybrid_body("VistA-RPC-Broker down", VEC, Filters(), S)["query"]["hybrid"]["queries"][0]
    assert len(ci["bool"]["should"]) == 1 + len(S.field_map["ci_term_fields"])


def test_ci_tokens_and_codes():
    assert ci_tokens("ORA-12541 on vista-rpc-broker at VAMC", 3) == [
        "ORA-12541", "vista-rpc-broker", "VISTA-RPC-BROKER", "VAMC"]
    assert ci_tokens("users cannot log in") == []
    assert "ora-12541" in content_terms("Getting ORA-12541 on login")
    assert normalize_query("  a \n b  ") == "a b"


def test_lexical_coverage():
    assert lexical_coverage(["login", "timeout"], "Login failure") == 0.5
    assert lexical_coverage([], "x") == 0.0


def test_collapse_groups_and_ranks():
    chunks = [_ch("A#symptom", "A", 3), _ch("B#symptom", "B", 1), _ch("A#resolution", "A", 2), _ch("C#symptom", "C", 4)]
    out = collapse_to_incidents(chunks, top_k=2)
    assert [i.incident_number for i in out] == ["B", "A"]
    assert [c.chunk_id for c in out[1].chunks] == ["A#resolution", "A#symptom"]
    assert [i.rank for i in out] == [1, 2]


def test_rrf_prefers_chunks_in_both_lists():
    lex = [_ch("x", "X", 1), _ch("y", "Y", 2)]
    sem = [_ch("z", "Z", 1), _ch("y", "Y", 2)]
    assert rrf_fuse(lex, sem, 10)[0].chunk_id == "y"
