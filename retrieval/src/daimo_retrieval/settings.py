"""All tunables, read from environment variables (systemd loads them from SSM /daimo/retrieval/*)."""
import json
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_FIELD_MAP: dict = {
    "vector": "embedding",
    "text": "chunk_text",
    "chunk_id": "chunk_id",
    "chunk_type": "chunk_type",
    "window": "window",
    "incident_number": "incident_number",
    "title": "short_description",
    "priority": "priority",
    "priority_level": "priority_level",
    "category": "category",
    "affected_service": "affected_service",
    "affected_ci": "affected_ci",
    "assignment_group": "assignment_group",
    "state": "state",
    "resolution_code": "resolution_code",
    "closed_at": "closed_at",
    "kb_articles": "related_kb_articles",
    "bm25_fields": ["chunk_text", "short_description^2"],
    "ci_term_fields": {"affected_ci": 3, "configuration_items": 2, "affected_system_name": 2},
    "filter_fields": {
        "priority_level_max": "priority_level",
        "category": "category",
        "affected_service": "affected_service",
        "assignment_group": "assignment_group",
        "closed_after": "closed_at",
    },
}

DEFAULT_PRICE_TABLE: dict = {  # USD per 1K tokens; placeholders until sourced from the Bedrock pricing page
    "llm_input": 0.0, "llm_output": 0.0, "embed_input": 0.0,
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    aws_region: str = "us-east-1"
    daimo_opensearch_endpoint: str = "https://sbcurg5zzjoxvd3g19x3.us-east-1.aoss.amazonaws.com"
    daimo_opensearch_index: str = "daimo-incidents"
    opensearch_timeout_s: int = 30  # first AOSS request timed out at 10 s during phase 0
    hybrid_pipeline_name: str = "daimo-hybrid-pipeline"
    hybrid_weights: str = "0.3,0.7"
    retrieval_mode: str = "pipeline"  # pipeline | client_rrf

    embed_model_id: str = "amazon.titan-embed-text-v2:0"
    embed_dimensions: int = 1024
    embed_normalize: bool = True

    candidate_pool: int = 50
    knn_k: int = 50
    knn_oversample: float = 3.0
    ci_token_min_len: int = 3
    top_k_default: int = 5
    top_k_max: int = 10

    min_confidence: float = 0.55
    conf_cos_floor: float = 0.30
    conf_cos_ceil: float = 0.75
    conf_lexical_weight: float = 0.2
    conf_labels: str = "high>=0.75,medium>=0.55"
    allow_threshold_override: bool = False

    model_id: str = "amazon.nova-pro-v1:0"
    model_allowlist: str = "amazon.nova-pro-v1:0"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 1200
    llm_timeout_s: int = 30
    max_context_chars: int = 24000

    sn_link_template: str = (
        "https://{instance}.service-now.com/nav_to.do?uri=incident.do?sysparm_query=number={incident_number}"
    )
    sn_instance: str = "INSTANCE"
    field_map: dict = Field(default_factory=lambda: dict(DEFAULT_FIELD_MAP))
    price_table: dict = Field(default_factory=lambda: dict(DEFAULT_PRICE_TABLE))
    facets_ttl_s: int = 600
    facets_max_terms: int = 200
    guard_enabled: bool = True
    guard_messages_version: str = "messages_v1"
    guard_llm_enabled: bool = True
    guard_model_id: str = "amazon.nova-micro-v1:0"  # settings only, never selectable by callers
    guard_timeout_s: int = 3
    output_guard_enabled: bool = True
    allow_debug: bool = False
    log_answer_text: bool = False
    config_version: str = "2026-10-27.1"

    @field_validator("field_map", "price_table", mode="before")
    @classmethod
    def _json(cls, v):
        return json.loads(v) if isinstance(v, str) else v

    @property
    def model_allowlist_set(self) -> set[str]:
        return {m.strip() for m in self.model_allowlist.split(",") if m.strip()}

    @property
    def weights(self) -> list[float]:
        return [float(x) for x in self.hybrid_weights.split(",")]

    def label_for(self, conf: float) -> str:
        """CONF_LABELS like 'high>=0.75,medium>=0.55'; anything lower is 'low'."""
        bands = []
        for part in self.conf_labels.split(","):
            name, _, val = part.partition(">=")
            bands.append((float(val), name.strip()))
        for val, name in sorted(bands, reverse=True):
            if conf >= val:
                return name
        return "low"


@lru_cache
def get_settings() -> Settings:
    return Settings()
