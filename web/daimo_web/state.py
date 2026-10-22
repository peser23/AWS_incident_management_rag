"""Session state: the conversation, the sidebar filters, and the mapping from filters to the API's Options.
Functions take the session-state mapping as an argument so they can be tested without Streamlit."""
import time
import uuid
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from datetime import date

QUERY_MIN, QUERY_MAX = 3, 1000  # TriageRequest.query bounds


def default_filters(top_k: int = 5) -> dict:
    return {"priority_max": None, "category": [], "affected_service": [], "assignment_group": [],
            "closed_after": None, "top_k": top_k}


@dataclass
class Turn:
    """One question and its outcome. `filters` is a snapshot, so later filter changes don't rewrite history."""
    query: str
    filters: dict
    response: dict | None = None
    error: dict | None = None  # ClientError.to_dict()
    ts: float = field(default_factory=time.time)

    @property
    def status(self) -> str:
        return self.response["status"] if self.response else "error"

    @property
    def request_id(self) -> str | None:
        return self.response["request_id"] if self.response else (self.error or {}).get("request_id")


def new_session_id() -> str:
    return f"st-{uuid.uuid4().hex[:8]}"


def init_state(ss: MutableMapping, top_k_default: int = 5) -> None:
    """Idempotent: only fills keys that are missing, so reruns keep everything."""
    ss.setdefault("session_id", new_session_id())
    ss.setdefault("turns", [])
    ss.setdefault("filters", default_filters(top_k_default))
    ss.setdefault("pending_turn", None)  # a submitted question whose request hasn't completed yet


def new_conversation(ss: MutableMapping) -> None:
    """Clear the thread and start a new session id; filters are kept."""
    ss["turns"] = []
    ss["session_id"] = new_session_id()
    ss["pending_turn"] = None


def validate_query(text: str) -> str | None:
    """Error message for a query the service would reject, else None."""
    n = len(text.strip())
    if n < QUERY_MIN:
        return f"Please enter at least {QUERY_MIN} characters."
    if n > QUERY_MAX:
        return f"Please keep the description under {QUERY_MAX} characters (currently {n})."
    return None


def filters_to_options(filters: dict, top_k_max: int = 10) -> dict:
    """UI filters -> TriageRequest.options. Unset filters are omitted so the service applies its defaults."""
    api_filters: dict = {}
    if filters.get("priority_max"):
        api_filters["priority_level_max"] = int(filters["priority_max"])
    for key in ("category", "affected_service", "assignment_group"):
        if filters.get(key):
            api_filters[key] = list(filters[key])
    closed = filters.get("closed_after")
    if closed:
        api_filters["closed_after"] = closed.isoformat() if isinstance(closed, date) else str(closed)
    options: dict = {}
    if filters.get("top_k"):
        options["top_k"] = max(1, min(int(filters["top_k"]), top_k_max))
    if api_filters:
        options["filters"] = api_filters
    return options


def describe_filters(filters: dict) -> list[str]:
    """Short labels for the chips shown on each turn and above the input."""
    chips = []
    if filters.get("priority_max"):
        chips.append(f"Priority ≤ {filters['priority_max']}")
    for key, label in (("category", "Category"), ("affected_service", "Service"),
                       ("assignment_group", "Group")):
        if filters.get(key):
            chips.append(f"{label}: {', '.join(filters[key])}")
    if filters.get("closed_after"):
        closed = filters["closed_after"]
        chips.append(f"Closed after {closed.isoformat() if isinstance(closed, date) else closed}")
    if filters.get("top_k"):
        chips.append(f"Top {filters['top_k']}")
    return chips
