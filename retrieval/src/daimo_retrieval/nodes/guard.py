"""Input guard, first node in the graph: redact sensitive data, then route greeting / blocked / out_of_scope / triage.

Two layers: rules decide the clear cases for free; a message the rules cannot place goes to a small classifier model.
If the classifier fails or times out the message is sent on as triage (fail open): the retrieval confidence gate and
the citation validator already stop unsupported answers. Injection hits always block.
"""
import json
import re
from functools import lru_cache
from html import escape
from importlib import resources

import structlog

from ..settings import get_settings
from ..text import (injection_hit, normalize_query, only_placeholders, redact_sensitive, strip_control)

GREETING_TERMS = frozenset("""hi hello hey hiya howdy greetings good morning afternoon evening day yo thanks thank
    thx ty cheers bye goodbye later cya see ok okay cool great nice appreciated""".split())
FILLER_TERMS = frozenset("""how are you is it going doing there team all everyone folks a the so much very for
    your my help helpful that this again now please and to you've you're i'm im""".split())
BYE_TERMS = frozenset("bye goodbye cya later".split())
THANKS_TERMS = frozenset("thanks thank thx ty cheers appreciated".split())

IT_OPS_TERMS = frozenset("""outage outages error errors exception exceptions down latency slow slowness login logon
    signin timeout timeouts timed server servers database db deploy deployed deployment patch patched certificate
    cert expired expiry vpn dns ssl tls cpu memory disk queue crash crashed crashing failed failure failures failing
    incident incidents ticket restart restarted service services application app network firewall connection
    connections unreachable degraded degradation alert alerts monitoring backup batch job jobs api endpoint portal
    mfa password lockout locked unavailable reboot rollback""".split())

log = structlog.get_logger("daimo_retrieval")
INTENTS = ("triage", "out_of_scope", "greeting")
GUARD_TOOL = {"toolSpec": {"name": "GuardDecision", "inputSchema": {"json": {
    "type": "object", "required": ["intent", "reason"],
    "properties": {"intent": {"type": "string", "enum": list(INTENTS)},
                   "reason": {"type": "string", "maxLength": 120}}}}}}

_WORD = re.compile(r"[a-z0-9_']+")
_CI_LIKE = re.compile(r"\b[A-Za-z]+[-_]?\d+[A-Za-z0-9_-]*\b|\b[A-Za-z0-9]+_\w+\b")  # ORA-12541, INC0012345, srv01, db_prod
_PLACEHOLDER_WORDS = re.compile(r"<[A-Z_]+>")


@lru_cache
def messages() -> dict:
    name = get_settings().guard_messages_version
    return json.loads(resources.files("daimo_retrieval.prompts").joinpath(f"{name}.json").read_text())


@lru_cache
def guard_system_prompt() -> str:
    return resources.files("daimo_retrieval.prompts").joinpath("guard_system_v1.txt").read_text()


def _words(q: str) -> list[str]:
    return _WORD.findall(_PLACEHOLDER_WORDS.sub(" ", q).lower())


def greeting_kind(q: str) -> str | None:
    """hello / thanks / bye when the message is nothing but greeting and filler words, else None."""
    words = _words(q)
    if not words or not all(w in GREETING_TERMS or w in FILLER_TERMS for w in words):
        return None
    if not any(w in GREETING_TERMS for w in words):
        return None
    if any(w in BYE_TERMS for w in words):
        return "bye"
    if any(w in THANKS_TERMS for w in words):
        return "thanks"
    return "hello"


def in_scope_signal(q: str) -> bool:
    """Error codes, CI-like tokens or incident numbers, or at least two distinct IT-operations terms. One IT word
    alone ("explain what DNS stands for") is not enough: that goes to the classifier."""
    if _CI_LIKE.search(q):
        return True
    return len({w.rstrip("s") if w.rstrip("s") in IT_OPS_TERMS else w for w in _words(q)
                if w in IT_OPS_TERMS or w.rstrip("s") in IT_OPS_TERMS}) >= 2


def classify(query: str) -> dict:
    """Pure function: query -> {query, redactions, intent, layer, reason_code, greeting_kind}."""
    q, redactions = redact_sensitive(normalize_query(strip_control(query)))
    out = {"query": q, "redactions": redactions, "intent": "triage", "layer": "rule", "reason_code": None,
           "greeting_kind": None}
    if only_placeholders(q):
        return {**out, "intent": "out_of_scope", "reason_code": "off_topic"}
    if injection_hit(q):
        return {**out, "intent": "blocked", "reason_code": "prompt_injection"}
    kind = greeting_kind(q)
    if kind:
        return {**out, "intent": "greeting", "greeting_kind": kind}
    if in_scope_signal(q):
        return out
    return {**out, "layer": "ambiguous"}  # the rules cannot place it: guard() asks the classifier


def llm_classify(query: str) -> tuple[str, str]:
    """Nova Micro with a forced GuardDecision tool call. Raises on any failure; the caller fails open."""
    from ..clients import guard_bedrock_runtime
    s = get_settings()
    r = guard_bedrock_runtime().converse(
        modelId=s.guard_model_id, system=[{"text": guard_system_prompt()}],
        messages=[{"role": "user", "content": [{"text": f"<operator_query>\n{escape(query, quote=False)}\n</operator_query>"}]}],
        toolConfig={"tools": [GUARD_TOOL], "toolChoice": {"tool": {"name": "GuardDecision"}}},
        inferenceConfig={"maxTokens": 100, "temperature": 0})
    block = next(b for b in r["output"]["message"]["content"] if "toolUse" in b)["toolUse"]["input"]
    if block.get("intent") not in INTENTS:
        raise ValueError(f"unexpected guard intent {block.get('intent')!r}")
    return block["intent"], str(block.get("reason", ""))[:120]


def guard(state: dict) -> dict:
    s, req = get_settings(), state["request"]
    if not s.guard_enabled:
        return {"guard_intent": "triage", "guard_layer": "off", "redactions": {}}
    d = classify(req.query)
    reason = None
    if d["layer"] == "ambiguous":
        if not s.guard_llm_enabled:
            d["layer"] = "rule"
        else:
            try:
                d["intent"], reason = llm_classify(d["query"])
                d["layer"] = "llm"
                if d["intent"] == "out_of_scope":
                    d["reason_code"] = "off_topic"
                elif d["intent"] == "greeting":
                    d["greeting_kind"] = greeting_kind(d["query"]) or "hello"
            except Exception as e:  # fail open: never block an operator because the classifier is down
                d["layer"] = "fail_open"
                log.warning("guard_fail_open", request_id=req.request_id, error=f"{type(e).__name__}: {str(e)[:200]}")
    update = {"guard_reason": reason, "guard_intent": d["intent"], "guard_layer": d["layer"], "guard_reason_code": d["reason_code"],
              "guard_greeting_kind": d["greeting_kind"], "redactions": d["redactions"]}
    if d["query"] != req.query:
        update["request"] = req.model_copy(update={"query": d["query"]})
    return update
