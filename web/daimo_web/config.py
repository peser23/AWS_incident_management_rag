"""UI settings, read from environment variables (systemd sets them in daimo-web.service)."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    retrieval_url: str
    user_id: str
    request_timeout_s: int
    connect_timeout_s: int
    top_k_default: int
    top_k_max: int


def load() -> Config:
    """Read on every call so tests can change the environment; it is a handful of getenv calls."""
    return Config(
        retrieval_url=os.getenv("DAIMO_RETRIEVAL_URL", "http://127.0.0.1:8000").rstrip("/"),
        user_id=os.getenv("DAIMO_UI_USER", "streamlit.operator"),
        request_timeout_s=int(os.getenv("REQUEST_TIMEOUT_S", "45")),
        connect_timeout_s=int(os.getenv("CONNECT_TIMEOUT_S", "5")),
        top_k_default=int(os.getenv("TOP_K_DEFAULT", "5")),
        top_k_max=int(os.getenv("TOP_K_MAX", "10")),  # must match the service's TOP_K_MAX
    )
