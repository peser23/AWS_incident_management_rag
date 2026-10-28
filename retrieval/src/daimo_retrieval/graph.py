"""LangGraph wiring: guard -> embed -> retrieve -> gate -> (synthesize -> validate)* -> respond. Every path ends in respond."""
import time
from functools import wraps
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from .nodes.embed import embed_query
from .nodes.gate import score_and_gate
from .nodes.guard import guard
from .nodes.respond import build_response
from .nodes.retrieve import hybrid_retrieve
from .nodes.synthesize import synthesize
from .nodes.validate import route_after_validate, validate_answer


class StageError(Exception):
    """A node failed; carries the stage name so the API can report it without a stack trace."""

    def __init__(self, stage: str, cause: Exception):
        super().__init__(f"{stage}: {type(cause).__name__}: {cause}")
        self.stage, self.cause = stage, cause


class TriageState(TypedDict, total=False):
    request: object
    guard_intent: Literal["triage", "greeting", "out_of_scope", "blocked"]
    guard_layer: str
    guard_reason: str | None
    output_guard_hits: list[str]
    guard_reason_code: str | None
    guard_greeting_kind: str | None
    redactions: dict[str, int]
    query_vector: list[float]
    candidates: list
    knn_scores: dict[str, float]
    debug_query: dict
    passed: list
    withheld_count: int
    threshold: float
    answer: object
    context_incidents: list[str]
    schema_error: str | None
    validation_errors: list[str]
    invalid_citations: int
    retries: int
    message: str
    tokens: dict
    est_cost_usd: float
    prompt_version: str
    status: Literal["answered", "no_match", "answer_withheld", "greeting", "out_of_scope", "blocked"]
    response: object
    timings: dict[str, int]


def timed(stage: str, fn, record: bool = True):
    """Wrap a node: wrap failures in StageError and record stage latency (synthesize records its own, summed over retries)."""
    @wraps(fn)
    def run(state: TriageState) -> dict:
        t0 = time.time()
        try:
            out = fn(state)
        except Exception as e:
            raise StageError(stage, e) from e
        if record:
            timings = dict(state.get("timings") or {})
            timings[stage] = timings.get(stage, 0) + int((time.time() - t0) * 1000)
            out = {**out, "timings": timings}
        return out
    return run


def build_graph():
    g = StateGraph(TriageState)
    g.add_node("guard", timed("guard", guard))
    g.add_node("embed", timed("embed", embed_query))
    g.add_node("retrieve", timed("retrieve", hybrid_retrieve))
    g.add_node("gate", timed("gate", score_and_gate))
    g.add_node("synthesize", timed("synthesize", synthesize, record=False))
    g.add_node("validate", timed("validate", validate_answer))
    g.add_node("respond", timed("respond", build_response, record=False))

    g.add_edge(START, "guard")
    g.add_conditional_edges("guard", lambda s: "embed" if s["guard_intent"] == "triage" else "respond",
                            {"embed": "embed", "respond": "respond"})
    g.add_edge("embed", "retrieve")
    g.add_edge("retrieve", "gate")
    g.add_conditional_edges("gate", lambda s: "synthesize" if s["passed"] else "respond",
                            {"synthesize": "synthesize", "respond": "respond"})
    g.add_edge("synthesize", "validate")
    g.add_conditional_edges("validate", route_after_validate,
                            {"retry": "synthesize", "ok": "respond", "withhold": "respond"})
    g.add_edge("respond", END)
    return g.compile()


triage_graph = build_graph()
