"""FastAPI app: POST /v1/triage, GET /v1/facets, GET /healthz, GET /readyz. Sync handlers run in FastAPI's thread pool."""
import json
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
import structlog

from .graph import StageError, triage_graph
from .schemas import ErrorResponse, TriageRequest, TriageResponse
from .settings import get_settings

log = structlog.get_logger("daimo_retrieval")
app = FastAPI(title="daimo-retrieval", version=__version__)


@app.exception_handler(RequestValidationError)
def _validation(_: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": "invalid_request", "detail": jsonable_encoder(exc.errors(), custom_encoder={Exception: str})})


def validate_options(req: TriageRequest) -> TriageRequest:
    """Rules that depend on settings; 422 for anything the contract forbids."""
    s, o = get_settings(), req.options
    if o.top_k is not None and o.top_k > s.top_k_max:
        raise HTTPException(422, f"top_k must be 1..{s.top_k_max}")
    if o.model_id is not None and o.model_id not in s.model_allowlist_set:
        raise HTTPException(422, "model_id not in MODEL_ALLOWLIST")
    if o.include_debug and not s.allow_debug:
        raise HTTPException(422, "include_debug is disabled")
    if o.min_confidence is not None and not s.allow_threshold_override:
        raise HTTPException(422, "min_confidence override is disabled")
    return req


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": __version__, "config_version": get_settings().config_version}


@app.get("/readyz")
def readyz():
    """Index reachable, pipeline exists, Titan dimension matches config."""
    from .clients import bedrock_runtime, opensearch
    s, checks = get_settings(), {}

    def run(name, fn):
        try:
            checks[name] = {"ok": True, "detail": fn()}
        except Exception as e:  # report, never raise
            checks[name] = {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:200]}"}

    def index():
        return opensearch().search(index=s.daimo_opensearch_index, body={"size": 0})["hits"]["total"]

    def pipeline():
        if s.retrieval_mode != "pipeline":
            return "skipped (client_rrf)"
        opensearch().transport.perform_request("GET", f"/_search/pipeline/{s.hybrid_pipeline_name}")
        return s.hybrid_pipeline_name

    def titan():
        r = bedrock_runtime().invoke_model(modelId=s.embed_model_id, body=json.dumps(
            {"inputText": "readiness probe", "dimensions": s.embed_dimensions, "normalize": s.embed_normalize}))
        n = len(json.loads(r["body"].read())["embedding"])
        if n != s.embed_dimensions:
            raise ValueError(f"dimension {n} != {s.embed_dimensions}")
        return n

    run("index", index), run("pipeline", pipeline), run("embedding", titan)
    ok = all(c["ok"] for c in checks.values())
    return JSONResponse(status_code=200 if ok else 503, content={"ready": ok, "checks": checks})


FACET_KEYS = ("category", "affected_service", "assignment_group")
_facets_cache: dict = {"at": 0.0, "value": None}
_facets_lock = threading.Lock()


def load_facets() -> dict:
    """Distinct filter values from the index (terms aggregations on the filter fields), for the UI's dropdowns."""
    from .clients import opensearch
    s = get_settings()
    ff = s.field_map["filter_fields"]
    aggs = {k: {"terms": {"field": ff[k], "size": s.facets_max_terms}} for k in FACET_KEYS}
    aggs["priority_levels"] = {"terms": {"field": ff["priority_level_max"], "size": 10}}
    r = opensearch().search(index=s.daimo_opensearch_index, body={"size": 0, "aggs": aggs})["aggregations"]
    out = {k: sorted(str(b["key"]) for b in r[k]["buckets"] if str(b["key"]).strip()) for k in FACET_KEYS}
    out["priority_levels"] = sorted(int(b["key"]) for b in r["priority_levels"]["buckets"])
    return out


@app.get("/v1/facets")
def facets():
    """Cached per worker for FACETS_TTL_S; a failed refresh serves the last good value if there is one."""
    s = get_settings()
    with _facets_lock:
        if _facets_cache["value"] is not None and time.time() - _facets_cache["at"] < s.facets_ttl_s:
            return _facets_cache["value"]
        try:
            _facets_cache.update(value=load_facets(), at=time.time())
        except Exception as e:
            log.error("facets_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
            if _facets_cache["value"] is None:
                return JSONResponse(status_code=503, content=ErrorResponse(
                    request_id=str(uuid.uuid4()), error="upstream_failure", stage="facets").model_dump())
        return _facets_cache["value"]


@app.post("/v1/triage", response_model=TriageResponse, responses={422: {}, 500: {"model": ErrorResponse}})
def triage(req: TriageRequest):
    req = validate_options(req)
    req.request_id = req.request_id or str(uuid.uuid4())
    try:
        state = triage_graph.invoke({"request": req, "timings": {}, "retries": 0})
    except Exception as e:  # clean error JSON with request_id, never a stack trace to the client
        stage = e.stage if isinstance(e, StageError) else None
        cause = e.cause if isinstance(e, StageError) else e
        log.error("triage_failed", request_id=req.request_id, stage=stage, error=f"{type(cause).__name__}: {cause}")
        transient = type(cause).__name__ in {"ThrottlingException", "ReadTimeoutError", "ConnectionTimeout",
                                             "ServiceUnavailableException", "ModelTimeoutException"}
        return JSONResponse(status_code=503 if transient else 500,
                            content=ErrorResponse(request_id=req.request_id, error="upstream_failure" if transient
                                                  else "internal_error", stage=stage).model_dump())
    resp = state["response"]
    log.info("triage", request_id=req.request_id, session_id=req.session_id, user=req.user.id if req.user else None,
             status=resp.status, top_confidence=resp.confidence.overall, passed=resp.confidence.passed,
             withheld=resp.confidence.withheld, latency_ms=resp.metrics.latency_ms, tokens=resp.metrics.tokens,
             invalid_citations=state.get("invalid_citations", 0), config_version=resp.config_version)
    return resp
