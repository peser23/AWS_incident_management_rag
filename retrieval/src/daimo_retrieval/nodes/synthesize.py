"""Prompt assembly and structured synthesis on the Bedrock chat model (Converse tool use)."""
import time
from functools import lru_cache
from html import escape
from importlib import resources

from langchain_aws import ChatBedrockConverse

from ..models import Incident
from ..schemas import TriageAnswer
from ..settings import get_settings
from ..text import normalize_query

PROMPT_VERSION = "triage_system_v1"


@lru_cache
def system_prompt() -> str:
    return resources.files("daimo_retrieval.prompts").joinpath(f"{PROMPT_VERSION}.txt").read_text()


@lru_cache
def get_llm(model_id: str):
    s = get_settings()
    chat = ChatBedrockConverse(model=model_id, region_name=s.aws_region, temperature=s.llm_temperature,
                               max_tokens=s.llm_max_tokens, client=_runtime_client())
    return chat.with_structured_output(TriageAnswer, include_raw=True)


def _runtime_client():
    from ..clients import bedrock_runtime
    return bedrock_runtime()


def incident_text(inc: Incident) -> str:
    """Best chunk, plus the resolution chunk when the best one is not the full narrative (rule 5 needs the outcome)."""
    best = inc.best_chunk or inc.chunks[0]
    parts = [best.text]
    if best.chunk_type != "full_narrative":
        parts += [c.text for c in inc.chunks if c.chunk_type == "resolution" and c.chunk_id != best.chunk_id]
    return "\n".join(parts)


def _attr(v) -> str:
    return escape(str(v if v is not None else ""), quote=True)


def build_user_message(passed: list[Incident], query: str, max_chars: int,
                       errors: list[str] | None = None) -> tuple[str, list[str]]:
    """Returns (message, incident numbers actually sent). Candidate text is escaped so a ticket cannot close the
    <incidents> fence; the cap keeps rank order and drops incidents that no longer fit."""
    blocks, sent, used = [], [], 0
    for inc in passed:
        m = inc.meta
        body = escape(incident_text(inc), quote=False)
        title = escape(str(m.get("short_description") or ""), quote=False)
        head = (f'<incident number="{_attr(inc.incident_number)}" priority="{_attr(m.get("priority"))}" '
                f'service="{_attr(m.get("affected_service"))}" ci="{_attr(m.get("affected_ci"))}" '
                f'category="{_attr(m.get("category"))}" resolution_code="{_attr(m.get("resolution_code"))}" '
                f'closed="{_attr((m.get("closed_at") or "")[:10])}" match="{_attr(inc.best_chunk.chunk_type if inc.best_chunk else "")}">\n'
                f"Title: {title}\n")
        room = max_chars - used - len(head) - len("\n</incident>")
        if room < 200 and sent:
            break
        body = body[:max(room, 200)]
        block = f"{head}{body}\n</incident>"
        blocks.append(block), sent.append(inc.incident_number)
        used += len(block)
    msg = (f"<incidents>\n{chr(10).join(blocks)}\n</incidents>\n\n"
           f"<operator_query>\n{escape(normalize_query(query), quote=False)}\n</operator_query>")
    if errors:
        msg += ("\n\nYour previous answer failed validation. Fix these problems and answer again:\n- "
                + "\n- ".join(errors))
    return msg, sent


def synthesize(state: dict) -> dict:
    """LangGraph node: passed incidents -> answer (or schema error for the validator to handle)."""
    s, req = get_settings(), state["request"]
    model_id = req.options.model_id or s.model_id
    msg, sent = build_user_message(state["passed"], req.query, s.max_context_chars, state.get("validation_errors"))
    t0 = time.time()
    out = get_llm(model_id).invoke([("system", system_prompt()), ("human", msg)])
    ms = int((time.time() - t0) * 1000)

    usage = getattr(out.get("raw"), "usage_metadata", None) or {}
    tokens = dict(state.get("tokens") or {"input": 0, "output": 0})
    tokens["input"] += usage.get("input_tokens", 0)
    tokens["output"] += usage.get("output_tokens", 0)
    p = s.price_table
    cost = tokens["input"] / 1000 * p.get("llm_input", 0) + tokens["output"] / 1000 * p.get("llm_output", 0)
    timings = dict(state.get("timings") or {})
    timings["synthesize"] = timings.get("synthesize", 0) + ms
    update = {"answer": out.get("parsed"), "context_incidents": sent, "tokens": tokens,
              "est_cost_usd": round(cost, 6), "timings": timings, "prompt_version": PROMPT_VERSION,
              "schema_error": str(out["parsing_error"]) if out.get("parsing_error") else None}
    return update
