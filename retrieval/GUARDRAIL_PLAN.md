# DAIMO — Greeting, Scope Rejection and Basic Guardrail Implementation Plan

## 1. Scope and decisions

Add a guard step to the retrieval service in front of retrieval, plus a light output check after synthesis. The web UI only needs to render the new statuses.

### Requirements → design

| # | Requirement | Design |
| --- | --- | --- |
| 1 | Greet back and say what the tool can do | New `guard` node detects a message that is *only* a greeting or small talk ("hi", "good morning", "thanks"). It returns `status="greeting"` with a fixed capabilities message. No embedding, retrieval or LLM call. |
| 2 | Politely reject out-of-context questions | `guard` classifies intent as `triage` or `out_of_scope` (rules first, then a small LLM classifier). Out-of-scope returns `status="out_of_scope"` with a fixed, polite message. No retrieval. |
| 3 | Basic guardrail | **Input:** length and character checks and sensitive-data **redaction** (SSN, card numbers are replaced with placeholders and the query continues). Prompt-injection patterns return `status="blocked"`. **Output:** a new check in `validate` withholds an answer that contains PII patterns, or emails or URLs that are not in the cited records. |

### Decisions

- **Greeting and capability text are static, not LLM-generated.** They are deterministic, cost nothing and cannot hallucinate features. The text lives in `prompts/messages_v1.json` so it can be edited without code changes.
- **A greeting is only a greeting if nothing else is left.** "Hi, Coverage login is failing" goes to triage. After removing greeting and filler tokens, any remaining content term means `triage`.
- **Two-layer scope check.** (1) Cheap rules decide the obvious cases. (2) Ambiguous cases go to a small classifier model (`amazon.nova-micro-v1:0`, temperature 0, structured output, ~150 ms). The classifier sees only the operator query, never ticket text.
- **Sensitive data is redacted, not rejected.** SSNs and card numbers in the query are replaced with `<SSN>` / `<CARD_NUMBER>` (the same placeholder style as the redacted records) before any other step. The original value never reaches the embedding model, OpenSearch, the LLMs or the logs. The response tells the operator that something was removed.
- **Fail open to triage.** If the classifier errors or times out, the query goes on to retrieval. The confidence gate and citation validator already stop unsupported answers, so a classifier outage should not block operators. Prompt-injection rule hits always **fail closed**.
- **Bedrock Guardrails is deferred.** The managed service (denied topics, prompt-attack filter, PII, contextual grounding) is a stronger phase-2 option, but it needs a new AWS resource, IAM changes and a per-request cost. The in-code guard is designed so a `BedrockGuardrail` check can replace or supplement layer 2 later (§9).
- **No conversation memory changes.** The guard sees one query, as the rest of the service does.

## 2. Request flow

```
POST /v1/triage
   │
   ▼
 guard: redact sensitive data (query is rewritten in state; continues)
   │
   ├── greeting      ──▶ respond (status=greeting, capabilities message)
   ├── out_of_scope  ──▶ respond (status=out_of_scope, polite rejection)
   ├── blocked       ──▶ respond (status=blocked, prompt-injection message)
   └── triage        ──▶ embed → retrieve → gate → synthesize → validate(+output guard) → respond
```

Every path still ends in `respond`, keeping the existing graph invariant.

## 3. Guard node (`nodes/guard.py`)

Checks run in this order. The first decisive check wins.

| Step | Check | Result |
| --- | --- | --- |
| 1 | Normalize (`text.normalize_query`), strip zero-width/control characters | — |
| 2 | Sensitive data: SSN (`\b\d{3}-\d{2}-\d{4}\b`, also 9 digits after "SSN"), card number (13–19 digits, spaces/hyphens allowed, passing Luhn). Replace with `<SSN>` / `<CARD_NUMBER>`; set `req.query` to the redacted text and record `redactions` (detector name and count only) | Not decisive: continue to step 3 with the redacted query. If nothing but placeholders is left, `out_of_scope` |
| 3 | Prompt-injection patterns: "ignore (all )?(previous\|prior) instructions", "you are now", "system prompt", "act as", `</?(system\|incidents?\|operator_query)>` tags, "developer mode" | `blocked` / `prompt_injection` |
| 4 | Greeting/small talk: tokens all in `GREETING_TERMS` ∪ `FILLER_TERMS` (hi, hello, hey, good morning/afternoon/evening, thanks, thank you, bye, how are you, …) | `greeting` (subtype `hello` / `thanks` / `bye`) |
| 5 | Strong in-scope signal: `ci_tokens()` non-empty (error codes, CI names), INC number, or ≥1 term from `IT_OPS_TERMS` (outage, error, down, latency, login, timeout, server, database, deploy, certificate, …) | `triage` |
| 6 | Otherwise → LLM classifier | `triage` / `out_of_scope` / `greeting` |

Rule-based outcomes skip step 6, so most real triage queries (which contain error text or system names) pay no extra latency.

### Classifier (layer 2)

- Model: `GUARD_MODEL_ID` (default `amazon.nova-micro-v1:0`). It must be in a new `GUARD_MODEL_ALLOWLIST`; it is not exposed to callers.
- Prompt `prompts/guard_system_v1.txt`: define in scope as "IT operations incidents: symptoms, errors, outages, degraded services, configuration items, past incidents and their resolutions, triage steps". Define out of scope as "general knowledge, coding help, writing tasks, HR/policy/personal questions, opinions, requests about the assistant itself beyond what it can do". Include 10–15 labeled few-shot examples.
- Output tool schema: `GuardDecision{intent: Literal["triage","out_of_scope","greeting"], reason: str (≤120 chars)}`.
- Query wrapped in `<operator_query>` and HTML-escaped, as in `synthesize.build_user_message`.
- Timeout `GUARD_TIMEOUT_S` (default 3 s). On error or timeout: `triage` and log `guard_fail_open`.

### State additions (`TriageState`)

`guard_intent`, `guard_reason`, `guard_layer` (`rule` | `llm` | `fail_open`), `redactions: dict[str, int]` (e.g. `{"ssn": 1}`).

The redacted query replaces `request.query` in state, so `embed`, `retrieve`, `gate` (lexical coverage) and `synthesize` all see only the redacted text without code changes. `<SSN>`-style tokens contain no content terms, so they do not affect lexical scoring.

## 4. Output guardrail (extend `nodes/validate.py`)

After the existing citation checks, scan `summary`, `likely_cause.text`, `recommended_actions[].step` and `caveats` for:

- SSN and card-number patterns (same detectors as §3).
- Email addresses and URLs that do not appear in the text of the incidents that were sent (`context_incidents`).
- Phrases that typically follow an injection, such as "disable MFA" or "send … to" combined with an external address. Keep this to a short deny list and use it as a safety net only.

A hit gives `withhold` directly, with no retry. The answer must not be regenerated with the bad content echoed back into the prompt. Message: "The generated answer was withheld by a content safety check." Record `output_guard_hits` in state and logs.

## 5. Contract changes (`schemas.py`)

- `TriageResponse.status`: add `"greeting" | "out_of_scope" | "blocked"`.
- `TriageResponse.confidence`: make it `Confidence | None = None`. For guard statuses it is `None`, and `citations` is `[]`.
- New optional `guard: GuardInfo | None` with `{intent, layer, reason_code, redacted: list[str]}`. `reason_code` is one of `prompt_injection`, `off_topic`, `null`. `redacted` lists the detector names that fired (e.g. `["ssn"]`) and is set on any status, including `answered`. It is always returned, because it is safe to show and the UI uses it for wording. Free-text `reason` only appears under `debug`.
- `TriageRequest.query`: lower `min_length` from 3 to **1**. Today "hi" (2 chars) is rejected with 422 before the guard can greet back.
- `ModelInfo.llm`: the guard model id when layer 2 ran, otherwise `None` for guard statuses.
- Add contract fixtures `greeting.json`, `out_of_scope.json` and `blocked.json`, plus an `answered_redacted.json` variant, and extend the parametrize list in `tests/test_contract.py`.

## 6. Messages (`prompts/messages_v1.json`)

```json (shown as YAML for readability; the file is JSON, to avoid a new dependency)
greeting:
  hello: |
    Hello! I'm DAIMO, the incident triage assistant. I can help you:
    - find past incidents similar to the one you're working on
    - see likely causes and actions that resolved them, with citations to the incident records
    - narrow results by priority, category, affected service, assignment group or closed date (sidebar filters)
    Describe the symptom, error message or affected system to get started.
  thanks: "You're welcome. Describe another incident any time."
  bye: "Goodbye. Good luck with the incident."
out_of_scope: |
  Sorry, I can only help with IT incident triage based on historical incident records,
  so I can't answer that. Try describing an incident's symptom, error text or affected system.
redaction_notice: |
  Sensitive personal data (for example an SSN or card number) was removed from your message
  before it was processed. Please avoid including personal details in incident descriptions.
blocked:
  prompt_injection: |
    I can't process that request. Please describe the incident you're working on.
```

## 7. Settings (`settings.py`, SSM `/daimo/retrieval/*`)

| Setting | Default | Purpose |
| --- | --- | --- |
| `GUARD_ENABLED` | `true` | Master switch. When false, everything goes straight to `embed`. |
| `GUARD_LLM_ENABLED` | `true` | Turn layer 2 off and rely on rules only (ambiguous queries become `triage`) |
| `GUARD_MODEL_ID` | `amazon.nova-micro-v1:0` | Classifier model |
| `GUARD_TIMEOUT_S` | `3` | Classifier read timeout (separate boto client config) |
| `GUARD_MESSAGES_VERSION` | `messages_v1` | Message file |
| `OUTPUT_GUARD_ENABLED` | `true` | §4 checks |

Bump `CONFIG_VERSION`. IAM: the instance role needs `bedrock:InvokeModel` on the Nova Micro model ARN. Model access must be enabled in the account (check in phase 0).

## 8. Web UI changes (`web/daimo_web/render.py`)

- `render_turn_body`: skip `confidence_badge` when `confidence` is `None`.
- `greeting` → `st.markdown(message)` (plain assistant text, no box). `out_of_scope` → `st.info`. `blocked` → `st.warning`.
- Unknown future statuses fall through to `st.info(message)` instead of breaking.
- When `guard.redacted` is non-empty, show `redaction_notice` as an `st.caption` above the answer. The returned notice text comes from the service (add it as `TriageResponse.notice: str | None`).
- The user's own message in the chat history is shown as typed. This is intentional: it stays in the browser session and is never sent anywhere except the service, which redacts it. No client-side redaction.
- Client-side: no minimum-length check, so "hi" reaches the service (confirm `app.py` does not add one).
- Tests: add cases to `tests/test_render.py` for the three statuses and for `confidence=None`.

## 9. Tasks

| Phase | Task | Done when |
| --- | --- | --- |
| 0 | Confirm Nova Micro access and IAM in the account; measure p50 latency with 20 sample queries | Latency < 400 ms p50 |
| 1 | `nodes/guard.py` rules layer + term lists; `text.py` helpers (redaction with Luhn check, injection regexes) | Unit tests pass (§10) |
| 1 | Graph: `START → guard`, conditional edge to `embed` or `respond`; `respond` builds guard responses | `test_graph.py` covers all four routes |
| 1 | Schema/contract changes (§5) and fixtures | `test_contract.py` passes |
| 2 | Classifier layer: prompt, `GuardDecision`, fail-open path | Mocked-LLM unit tests pass, including timeout |
| 2 | Output guard in `validate` | Unit tests pass; the existing `test_adversarial_live.py` still passes |
| 3 | Web rendering (§8), including the redaction notice | `web` tests pass |
| 3 | Eval set `eval/guard_cases.jsonl` (~80 labeled queries: 20 greeting, 30 triage, 20 off-topic, 10 attack) and a script to report the confusion matrix | No in-scope query is rejected on the triage set; ≥ 90 % off-topic recall |
| 4 | Deploy: SSM params, `sudo retrieval/deploy/install.sh`, `sudo web/deploy/install.sh`, restart both services (with confirmation) | Smoke test: "hi", "what's the capital of France", "ORA-12541 on claims DB" give `greeting`, `out_of_scope`, `answered`/`no_match` |
| later | Optional Bedrock Guardrail (denied topics + prompt-attack + PII) as an extra input/output check | Separate plan |

## 10. Tests

**Unit (`tests/test_guard.py`)**

- Greetings: "hi", "Hello!", "good morning team", "thanks", "thank you!", "bye" → `greeting` with the right subtype, no LLM call.
- Greeting plus content: "hi, coverage app login fails" → `triage`.
- In-scope rules: "ORA-12541", "INC0012345 similar?", "VPN down for remote users" → `triage` without LLM.
- Off-topic (mocked LLM): "capital of France", "write a python script to sort a list", "what's the weather" → `out_of_scope`.
- Redaction: "user SSN 123-45-6789 can't log in to Coverage" → query becomes "user SSN <SSN> can't log in to Coverage", intent `triage`, `redactions={"ssn": 1}`. A valid Luhn card number is redacted. An invalid Luhn 16-digit number and an INC number are *not* touched. A message that is only an SSN → `out_of_scope`.
- No leak: with mocked embed and LLM, assert the original SSN string never appears in any call argument or captured log line.
- Blocked: the three payloads from `test_adversarial_live.INJECTIONS` → `blocked`.
- Fail-open: the classifier raises `ReadTimeoutError` → `triage`, and `guard_layer="fail_open"`.
- `GUARD_ENABLED=false` → always `triage`.

**API (`tests/test_api.py`)**: `query="hi"` returns 200 with `status="greeting"`, `confidence` null and `citations` `[]`. Embed and retrieve are never called (assert on mocks).

**Integration (`-m integration`)**: run the eval set against real Bedrock and record precision and recall per class.

## 11. Logging and metrics

Extend the `triage` log line with `guard_intent`, `guard_layer`, `guard_reason_code`, `output_guard_hits`. Log only the redacted query and the `redactions` counts; never the matched value. `log_answer_text` behaviour is unchanged. Add `latency_ms.guard`.

## 12. Open questions

1. Is there an authoritative scope list (supported services, assignment groups) to put in the classifier prompt? It could also be built from `/v1/facets` values.
2. Should non-triage turns count toward any usage or cost reporting?
3. Is a fail-open classifier acceptable, or does policy require fail-closed?
