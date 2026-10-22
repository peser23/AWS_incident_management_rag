"""Deterministic output validation, run before the response is built.

Unknown citations are removed (and counted), actions left uncited are dropped, the summary must carry at least one
valid inline citation. One retry with the errors appended; a second failure or insufficient_evidence withholds.
"""
import re

from ..schemas import ActionStep, Cited, TriageAnswer

INLINE = re.compile(r"\[(INC\d+)\]")
WITHHELD_MSG = "The generated answer could not be validated against the retrieved incidents."


def _strip_unknown(text: str, valid: set[str]) -> tuple[str, int]:
    removed = 0

    def sub(m: re.Match) -> str:
        nonlocal removed
        if m.group(1) in valid:
            return m.group(0)
        removed += 1
        return ""
    return re.sub(r"\s{2,}", " ", INLINE.sub(sub, text)).strip(), removed


def _clean_ids(ids: list[str], valid: set[str]) -> tuple[list[str], int]:
    kept = list(dict.fromkeys(i for i in ids if i in valid))
    return kept, len(ids) - len([i for i in ids if i in valid])


def validate_answer(state: dict) -> dict:
    valid = set(state["context_incidents"])
    errors: list[str] = []
    invalid = 0
    answer: TriageAnswer | None = state.get("answer")

    if answer is None:
        errors.append(f"The tool call did not match the TriageAnswer schema ({state.get('schema_error') or 'no output'}).")
    else:
        summary, n = _strip_unknown(answer.summary, valid)
        invalid += n
        cause = answer.likely_cause
        if cause:
            text, n = _strip_unknown(cause.text, valid)
            ids, m = _clean_ids(cause.citations + INLINE.findall(text), valid)
            invalid += n + m
            cause = Cited(text=text, citations=ids) if ids else None
        actions = []
        for a in answer.recommended_actions:
            text, n = _strip_unknown(a.step, valid)
            ids, m = _clean_ids(a.citations + INLINE.findall(text), valid)
            invalid += n + m
            if ids:
                actions.append(ActionStep(step=text, citations=ids))
        caveats, n = _strip_unknown(answer.caveats, valid)
        invalid += n
        cited = list(dict.fromkeys(
            INLINE.findall(summary) + (cause.citations if cause else []) + [c for a in actions for c in a.citations]
            + [c for c in answer.cited_incidents if c in valid]))
        invalid += len([c for c in answer.cited_incidents if c not in valid])

        if answer.insufficient_evidence:
            return {"answer": None, "status": "answer_withheld", "invalid_citations": invalid, "validation_errors": [],
                    "message": " ".join(x for x in (answer.summary.strip(), answer.caveats.strip()) if x)
                               or WITHHELD_MSG}
        if not [c for c in INLINE.findall(summary) if c in valid]:
            errors.append("The summary must contain at least one inline citation like [INC0012345] "
                          "using an incident number from <incidents>.")
        answer = answer.model_copy(update={"summary": summary, "likely_cause": cause, "recommended_actions": actions,
                                           "caveats": caveats, "cited_incidents": cited})

    if errors:
        retries = state.get("retries", 0)
        if retries < 1:
            return {"validation_errors": errors, "retries": retries + 1, "answer": answer,
                    "invalid_citations": state.get("invalid_citations", 0) + invalid}
        return {"answer": None, "status": "answer_withheld", "message": WITHHELD_MSG, "validation_errors": errors,
                "invalid_citations": state.get("invalid_citations", 0) + invalid}
    return {"answer": answer, "validation_errors": [], "invalid_citations": state.get("invalid_citations", 0) + invalid}


def route_after_validate(state: dict) -> str:
    """retry at most once (counter in state); withhold on a second failure or insufficient_evidence."""
    if state.get("status") == "answer_withheld":
        return "withhold"
    return "retry" if state.get("validation_errors") else "ok"
