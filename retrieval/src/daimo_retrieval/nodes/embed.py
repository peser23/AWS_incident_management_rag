"""Titan V2 query embedding. Titan has no query/document asymmetry, so queries embed exactly like ingest text."""
import json

from ..clients import bedrock_runtime
from ..settings import get_settings
from ..text import normalize_query


def embed_text(text: str) -> list[float]:
    s = get_settings()
    r = bedrock_runtime().invoke_model(modelId=s.embed_model_id, body=json.dumps(
        {"inputText": normalize_query(text), "dimensions": s.embed_dimensions, "normalize": s.embed_normalize}))
    vec = json.loads(r["body"].read())["embedding"]
    if len(vec) != s.embed_dimensions:
        raise ValueError(f"embedding has {len(vec)} dims, expected {s.embed_dimensions}")
    return vec


def embed_query(state: dict) -> dict:
    """LangGraph node: request -> query_vector."""
    return {"query_vector": embed_text(state["request"].query)}


def startup_check(sample_doc: dict | None = None) -> None:
    """Fail fast if config and the embedding model/index disagree."""
    s = get_settings()
    embed_text("startup check")
    if sample_doc:
        if sample_doc.get("embedding_model") != s.embed_model_id:
            raise ValueError("index embedding_model differs from EMBED_MODEL_ID")
        if sample_doc.get("embedding_dimensions") != s.embed_dimensions:
            raise ValueError("index embedding_dimensions differs from EMBED_DIMENSIONS")
