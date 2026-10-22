"""HTTP client for the retrieval service. The only module in web/ that does network I/O."""
import uuid

import requests

from .config import Config, load


class ClientError(Exception):
    """A failed call, reduced to what the UI shows. kind: invalid_request | busy | internal | unreachable."""

    def __init__(self, kind: str, message: str, request_id: str | None = None):
        super().__init__(message)
        self.kind, self.message, self.request_id = kind, message, request_id

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message, "request_id": self.request_id}


def _detail(resp: requests.Response) -> str:
    """422 bodies are {"error","detail":[pydantic errors]} or FastAPI's {"detail": "..."}."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300] or resp.reason
    detail = body.get("detail", body.get("error", ""))
    if isinstance(detail, list):
        return "; ".join(f"{'.'.join(str(p) for p in d.get('loc', [])[1:])}: {d.get('msg', '')}" for d in detail)
    return str(detail)


def _raise_for(resp: requests.Response, request_id: str) -> None:
    if resp.ok:
        return
    if resp.status_code == 422:
        raise ClientError("invalid_request", f"Request rejected: {_detail(resp)}", request_id)
    try:
        request_id = resp.json().get("request_id") or request_id
    except ValueError:
        pass
    if resp.status_code == 503:
        raise ClientError("busy", "The service is busy or an upstream model timed out. Try again.", request_id)
    raise ClientError("internal", f"The service returned an error ({resp.status_code}).", request_id)


def triage(query: str, *, session_id: str | None, user_id: str | None, options: dict | None = None,
           cfg: Config | None = None) -> dict:
    """POST /v1/triage and return the TriageResponse dict. The request_id is generated here so even a
    timeout has an id the operator can quote."""
    cfg = cfg or load()
    request_id = str(uuid.uuid4())
    payload = {"request_id": request_id, "session_id": session_id, "query": query,
               "user": {"id": user_id} if user_id else None, "options": options or {}}
    try:
        resp = requests.post(f"{cfg.retrieval_url}/v1/triage", json={k: v for k, v in payload.items() if v is not None},
                             timeout=(cfg.connect_timeout_s, cfg.request_timeout_s))
    except requests.Timeout:
        raise ClientError("unreachable", f"No answer within {cfg.request_timeout_s} s. Try again.", request_id)
    except requests.ConnectionError:
        raise ClientError("unreachable", "Cannot reach the retrieval service.", request_id)
    _raise_for(resp, request_id)
    return resp.json()


def facets(cfg: Config | None = None) -> dict:
    """GET /v1/facets: filter values. Raises ClientError; the caller falls back to facets.json."""
    cfg = cfg or load()
    try:
        resp = requests.get(f"{cfg.retrieval_url}/v1/facets", timeout=(cfg.connect_timeout_s, 10))
    except requests.RequestException as e:
        raise ClientError("unreachable", f"Cannot load filter values: {type(e).__name__}")
    _raise_for(resp, "")
    return resp.json()


def ready(cfg: Config | None = None) -> dict:
    """GET /readyz. Never raises: returns {"ready": bool, "checks": {name: {"ok", "detail"}}}."""
    cfg = cfg or load()
    try:
        resp = requests.get(f"{cfg.retrieval_url}/readyz", timeout=(cfg.connect_timeout_s, 8))  # page waits on it
        body = resp.json()
        return {"ready": bool(body.get("ready")) and resp.ok, "checks": body.get("checks", {})}
    except (requests.RequestException, ValueError) as e:
        return {"ready": False, "checks": {"service": {"ok": False, "detail": f"{type(e).__name__}"}}}
