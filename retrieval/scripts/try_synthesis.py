"""Live smoke: embed -> retrieve -> gate -> synthesize -> validate for a few queries (read-only)."""
import json, sys
from daimo_retrieval.nodes.embed import embed_text
from daimo_retrieval.nodes.retrieve import retrieve
from daimo_retrieval.nodes.gate import score_and_gate
from daimo_retrieval.nodes.synthesize import synthesize
from daimo_retrieval.nodes.validate import validate_answer, route_after_validate
from daimo_retrieval.schemas import TriageRequest

def run(q, mutate=None):
    req = TriageRequest(request_id="x", query=q)
    c, k, _ = retrieve(q, embed_text(q), req.options.filters, 5)
    st = {"request": req, "candidates": c, "knn_scores": k}
    st.update(score_and_gate(st))
    if mutate: mutate(st)
    if not st["passed"]:
        return st, "no_match"
    while True:
        st.update(synthesize(st)); st.update(validate_answer(st))
        r = route_after_validate(st)
        if r != "retry": return st, r

if __name__ == "__main__":
    for q in sys.argv[1:]:
        st, route = run(q)
        print(f"\n=== {q}\nroute={route} retries={st.get('retries',0)} invalid_citations={st.get('invalid_citations')} tokens={st.get('tokens')} t={st.get('timings')}")
        if st.get("answer"): print(json.dumps(st["answer"].model_dump(), indent=1))
        else: print("message:", st.get("message"))
