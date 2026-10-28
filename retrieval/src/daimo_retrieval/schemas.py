"""Pydantic request/response models; the OpenAPI spec is generated from these."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class User(_Strict):
    id: str
    role: str | None = None


class Filters(_Strict):
    priority_level_max: int | None = Field(None, ge=1, le=5)
    category: list[str] = []
    affected_service: list[str] = []
    assignment_group: list[str] = []
    closed_after: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class Options(_Strict):
    top_k: int | None = Field(None, ge=1)
    min_confidence: float | None = Field(None, ge=0, le=1)
    filters: Filters = Filters()
    model_id: str | None = None
    include_debug: bool = False


class TriageRequest(_Strict):
    request_id: str | None = None
    session_id: str | None = None
    user: User | None = None
    query: str = Field(min_length=1, max_length=1000)
    options: Options = Options()


class Cited(_Strict):
    text: str
    citations: list[str] = []


class ActionStep(_Strict):
    step: str
    citations: list[str] = []


class TriageAnswer(_Strict):
    """Structured LLM output (tool schema for the synthesis LLM) and the `answer` block of the response."""
    summary: str = Field(max_length=600)
    likely_cause: Cited | None = None
    recommended_actions: list[ActionStep] = Field(default=[], max_length=5)
    caveats: str = ""
    insufficient_evidence: bool = False
    cited_incidents: list[str] = []


class Confidence(_Strict):
    overall: float
    label: str
    threshold: float
    passed: int
    withheld: int


class CitationCard(_Strict):
    incident_number: str
    title: str | None = None
    priority: str | None = None
    affected_service: str | None = None
    affected_ci: str | None = None
    category: str | None = None
    state: str | None = None
    resolution_code: str | None = None
    closed_at: str | None = None
    related_kb_articles: list[str] = []
    snippet: str = ""
    confidence: float
    confidence_label: str
    match_reason: Literal["semantic", "keyword", "semantic+keyword"]
    matched_chunk_type: str
    cited_in_answer: bool = False
    url: str | None = None


class ModelInfo(_Strict):
    llm: str | None = None
    embedding: str


class Metrics(_Strict):
    latency_ms: dict[str, int] = {}
    tokens: dict[str, int] | None = None
    est_cost_usd: float | None = None


class GuardInfo(_Strict):
    intent: Literal["triage", "greeting", "out_of_scope", "blocked"]
    layer: str
    reason_code: Literal["prompt_injection", "off_topic"] | None = None
    redacted: list[str] = []


class TriageResponse(_Strict):
    request_id: str
    status: Literal["answered", "no_match", "answer_withheld", "greeting", "out_of_scope", "blocked"]
    answer: TriageAnswer | None = None
    message: str | None = None
    notice: str | None = None
    confidence: Confidence | None = None
    citations: list[CitationCard] = []
    model: ModelInfo | None = None
    metrics: Metrics = Metrics()
    config_version: str
    guard: GuardInfo | None = None
    debug: dict | None = None


class ErrorResponse(_Strict):
    request_id: str
    error: str
    stage: str | None = None
