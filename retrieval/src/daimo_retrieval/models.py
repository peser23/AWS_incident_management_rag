"""Internal (non-API) dataclasses passed between graph nodes."""
from dataclasses import dataclass, field


@dataclass
class Chunk:
    chunk_id: str
    chunk_type: str
    text: str
    source: dict                      # full _source subset returned by OpenSearch
    window: int | None = None
    hybrid_score: float | None = None
    rank: int = 0                     # 1-based position in the hybrid result list
    confidence: float = 0.0           # set by the gate
    match_reason: str = "semantic"

    @property
    def searchable_text(self) -> str:
        s = self.source
        parts = [self.text, s.get("short_description"), s.get("affected_ci"), s.get("affected_system_name")]
        parts += s.get("configuration_items") or []
        return " ".join(p for p in parts if p)


@dataclass
class Incident:
    incident_number: str
    chunks: list[Chunk] = field(default_factory=list)
    rank: int = 0                     # position by best chunk hybrid rank
    confidence: float = 0.0
    best_chunk: Chunk | None = None
    match_reason: str = "semantic"

    @property
    def meta(self) -> dict:
        return self.chunks[0].source if self.chunks else {}
