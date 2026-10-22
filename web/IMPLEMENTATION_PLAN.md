# DAIMO Incident Management — Streamlit Frontend Implementation Plan

## 1. Scope, assumptions and decisions

Build the operator-facing chat UI in `daimo/web`, running on the app EC2 (`daimo-app-ec2-01`) beside `daimo-retrieval.service`. The UI only calls the retrieval service over `http://127.0.0.1:8000`; it has no AWS credentials and does not touch OpenSearch or Bedrock.

### Requirements → design

| # | Requirement | Design |
| --- | --- | --- |
| 1 | Title "DAIMO Incident management" | `st.set_page_config(page_title=...)` + `st.title(...)` in `app.py` |
| 2 | Free-text chat, threaded history, session state across turns, applied filters visible | `st.chat_input` + `st.chat_message`; every turn stored in `st.session_state.turns` with a snapshot of the filters used; filter chips shown per turn and as an "Active filters" line under the title |
| 3 | Sidebar filters persist in the session | Sidebar widgets bound to `st.session_state` keys, mirrored into a `filters` dict that survives reruns; "Reset filters" and "New conversation" buttons |
| 4 | Stream responses as they are generated | **Deferred** (see §11). For now: blocking call to `/v1/triage` with an `st.spinner`; the full answer renders when the response arrives |
| 5 | Call the retrieval service for data | `daimo_web/client.py` → `/v1/triage`, `/v1/facets`, `/readyz` |
| 6 | Show confidence score and evidence | Confidence badge per answer (overall, label, threshold, passed/withheld) and an evidence card per `CitationCard` |

### Decisions

- **No streaming in this release.** The UI calls the existing `POST /v1/triage` and renders the whole response at once (typically 3–5 s, shown with a spinner). The rendering code takes a complete `TriageResponse`, so a streaming endpoint can be added later without changing how turns are drawn (§11).
- **The service has no memory between turns.** Each turn sends a single `query`; the UI keeps and shows the conversation history. Follow-ups that refer back ("what about the second one?") are not resolved. This is an open question (§11).
- **Filter values come from a new `GET /v1/facets` endpoint.** It runs terms aggregations on `category`, `affected_service` and `assignment_group`, and the service caches the result for 10 minutes. If the call fails, the UI uses a static `web/facets.json`.
- **This UI replaces `retrieval/streamlit_client/app.py`.** The `client.py` logic moves to `web/daimo_web/client.py`, and the old test page is deleted at cutover. Done 22 Oct: the old test instance (`/tmp/uivenv`, listening on `*:8501`) was stopped and removed.
- **Runtime:** a separate venv at `/opt/daimo-web/venv` (Python 3.12, same as retrieval) and a `daimo-web.service` systemd unit bound to **127.0.0.1:8501**. Operators connect through an SSM Session Manager port forward, which matches the no-public-IP hardening in the retrieval plan. Exposing the UI behind an ALB with OIDC is out of scope.
- **Ticket text is untrusted.** Snippets, titles and LLM text are rendered without `unsafe_allow_html`, and Markdown control characters in ticket fields are escaped. The corpus contains prompt-injection test incidents (retrieval task 4.3).

## 2. Request flow

```
Operator ──chat_input──▶ Streamlit (127.0.0.1:8501)
                           │  build payload: query + session_id + user + options{top_k, filters}
                           ▼
                   POST /v1/triage  (JSON, timeout 45 s)  — spinner while waiting
                           │
             200 TriageResponse  → store Turn → render answer, confidence badge, evidence cards
             422 / 500 / 503     → store Turn with error → render error message in the thread
```

## 3. Retrieval service additions (contract)

`/v1/triage` is used unchanged. The only addition is a facets endpoint for the filter values.

### `GET /v1/facets`

```json
{"category": ["Software", "..."], "affected_service": ["VistA", "..."], "assignment_group": ["..."], "priority_levels": [1,2,3,4,5]}
```

Field names come from `settings.field_map`. The result is cached in-process for `FACETS_TTL_S=600`.

## 4. UI layout

```
┌ Sidebar ───────────────────┐ ┌ DAIMO Incident management ─────────────────────────────┐
│ Filters                    │ │ Active filters: Priority ≤ 2 · Service: VistA · Top 5    │
│  Priority      [Any ▾]     │ │ ⚠ banner if /readyz fails                                │
│  Category      [multi]     │ │                                                          │
│  Service       [multi]     │ │ 🧑 VistA logins timing out after patch                    │
│  Assign. group [multi]     │ │    Filters: Priority ≤ 2 · Service: VistA · Top 5         │
│  Closed after  [date]      │ │ 🛠️ ● Medium confidence · 0.73                             │
│  Incidents     1━━●━━10    │ │    Threshold 0.55 · 5 passed · 0 below threshold          │
│ [Reset filters]            │ │    Summary … [INC41220398]                                │
│ ─────────────              │ │    Likely cause … · Recommended actions 1. … · Caveats    │
│ Session st-1a2b3c4d        │ │    ▸ Evidence (5)  INC41220398 · 0.82 high · ✓ cited      │
│ [New conversation]         │ │    request … · 3.4 s · model                              │
│ Service: ● ready           │ │ [ Describe the incident…                         ⏎ ]     │
└────────────────────────────┘ └──────────────────────────────────────────────────────────┘
```

## 5. Session state model

| Key | Type | Set by | Notes |
| --- | --- | --- | --- |
| `session_id` | `str` | first run | `st-<uuid8>`; sent as `session_id`; a new one is created by "New conversation" |
| `user_id` | `str` | config | `DAIMO_UI_USER` env var for now (§11) |
| `turns` | `list[Turn]` | after each response or error | `Turn(query, filters, request_id, status, response: dict | None, error: str | None, ts)` |
| `filters` | `dict` | sidebar | Canonical copy; widget keys `f_priority`, `f_category`, `f_service`, `f_group`, `f_closed_after`, `f_top_k` |
| `facets` | `dict` | `st.cache_data(ttl=600)` | Values for the filter multiselects |
| `pending_turn` | `Turn \| None` | chat submit | The question being sent. The input is disabled while it is set; if the run is interrupted (e.g. a sidebar click) it is resent with its original filters instead of being lost |

Rules:

- **Filters apply to the next turn only.** Each `Turn` keeps a snapshot, so earlier turns still show the filters they actually used.
- **One function renders both live and past turns.** `render_turn(turn)` draws a turn from session state on every rerun and is also used for the turn that just completed, so live and replayed turns look the same.
- **Filters map directly to the API schema.** `filters_to_options()` maps the UI state to `Options` exactly:
  - "Any" priority is omitted;
  - the date is formatted with `.isoformat()`;
  - empty lists are dropped;
  - `top_k` is capped at 10 (`TOP_K_MAX`).

## 6. Code layout

```
daimo/web/
  app.py                     # entry: page config, title, readiness banner, sidebar, history, chat loop
  daimo_web/
    __init__.py
    config.py                # env: DAIMO_RETRIEVAL_URL, DAIMO_UI_USER, REQUEST_TIMEOUT_S, TOP_K_MAX
    client.py                # triage(); facets(); ready(); error mapping
    state.py                 # Turn dataclass, init_state(), new_conversation(), filters_to_options()
    sidebar.py               # filter widgets, reset, session info, health indicator
    render.py                # render_turn, confidence_badge, evidence_card, filter_chips, answer_chunks()
  facets.json                # static fallback for filter values
  .streamlit/config.toml     # headless, 127.0.0.1, port 8501, XSRF on, usage stats off, theme
  requirements.txt           # streamlit, requests
  requirements.lock          # pinned at build time
  deploy/
    daimo-web.service        # systemd unit (user daimo, After=daimo-retrieval.service)
    install.sh               # venv in /opt/daimo-web, install unit, restart, smoke check
  tests/
    conftest.py              # requests mock; loads retrieval/tests/fixtures/*.json
    test_client.py
    test_state.py
    test_app.py              # streamlit.testing.v1.AppTest
retrieval/ (changes)
  src/daimo_retrieval/api.py # + /v1/facets
  tests/test_facets.py
```

### Key modules

**`client.py`**
- `triage(query, session_id, user_id, options) -> dict`: `requests.post(..., timeout=(5, REQUEST_TIMEOUT_S))`, returns the parsed `TriageResponse`. Builds on the existing `retrieval/streamlit_client/client.py`.
- Errors are turned into a typed `ClientError(kind, message, request_id)`:
  - 422 → `invalid_request` (detail shown);
  - 503 → `busy` ("try again");
  - 5xx → `internal`;
  - connect or read timeout → `unreachable`.

**`render.py`**
- `render_answer(answer)`: summary, likely cause, numbered actions with citation chips, and caveats.
- **Confidence badge**: green for high, amber for medium, red for low, built with `st.badge`/`st.metric`. It shows `overall` to 2 decimal places plus the threshold and passed/withheld counts.
- **Evidence card** (`st.expander` per citation, cited ones first, then by confidence):
  - the header shows number, title, confidence, label and a cited ✓;
  - inside: priority, service and CI, state, resolution code, closed date, a `st.progress(confidence)` bar, match reason and chunk type, the snippet (escaped), KB articles, and a ServiceNow link.
- **Rendering by status:**
  - `answered`: answer and evidence;
  - `no_match`: `st.info(message)` and any evidence below the threshold, labelled as such;
  - `answer_withheld`: `st.warning(message)` and the evidence.

**`app.py`** handles a question over two runs:

```python
if prompt := st.chat_input("Describe the incident…", disabled=ss.pending_turn is not None):
    ss.pending_turn = Turn(query=prompt.strip(), filters=dict(ss.filters))
    st.rerun()                                   # next run draws the input disabled

if pending := ss.pending_turn:
    ...draw user message, spinner, call client.triage(...), set pending.response or pending.error...
    ss.turns.append(pending); ss.pending_turn = None
    st.rerun()                                   # re-enable the input; history redraws the turn
```

## 7. Error and edge handling

| Case | UI behaviour |
| --- | --- |
| `/readyz` 503 or unreachable (checked with `st.cache_data(ttl=30)`) | Red banner at the top with the failing check names; chat stays enabled |
| Query < 3 or > 1000 chars | Rejected in the UI before the call, with a message in the thread |
| 422 | Shown as an assistant error turn with the detail |
| 503 / timeout | "Service busy or slow — try again" plus the `request_id`; the turn is kept in history |
| Filter value no longer in facets | Kept in state, shown as a chip, sent as is |

## 8. EC2 deployment

- **`install.sh`** (run as root): creates `/opt/daimo-web/venv` with `python3.12`, installs `requirements.lock`, copies the app to `/opt/daimo-web/app`, installs and enables `daimo-web.service`, waits for `/_stcore/health` on 8501, then runs a smoke test against `/v1/triage` with the answered fixture query.
- **Unit:**
  - command: `streamlit run /opt/daimo-web/app/app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true`;
  - runs as `User=daimo` with `Restart=always`;
  - environment: `Environment=DAIMO_RETRIEVAL_URL=http://127.0.0.1:8000`;
  - the same hardening flags as the retrieval unit, with `HOME`/`XDG_CACHE_HOME` pointed at `/var/lib/daimo-web` so Streamlit can write its machine id.
- **Access:** `aws ssm start-session --target <instance> --document-name AWS-StartPortForwardingSession --parameters portNumber=8501,localPortNumber=8501`, then open `http://localhost:8501`. No security group change is needed.
- **Cutover:** stop the `/tmp/uivenv` test process and delete `retrieval/streamlit_client/` once the new UI passes the acceptance tests.

## 9. Testing and acceptance

**Unit**
- `test_client.py`: payload shape; mapping 422, 503, 500 and timeouts to `ClientError`.
- `test_state.py`: `filters_to_options` produces payloads that pass `daimo_retrieval.schemas.Options` validation. The tests import the schema so the UI and the contract cannot drift apart.

**AppTest** (`streamlit.testing.v1`, mocked client, fixtures `answered`, `no_match`, `answer_withheld`)

| Test | Proves requirement |
| --- | --- |
| Title text equals "DAIMO Incident management" | 1 |
| Two chat submissions → two user and two assistant messages, in order, after rerun | 2 |
| Set filters, submit, rerun → sidebar values unchanged; turn shows filter chips; payload contains the filters | 2, 3 |
| Change filters after turn 1 → turn 1 chips unchanged, turn 2 uses the new filters | 2, 3 |
| "New conversation" clears turns and changes `session_id`; filters are kept | 2, 3 |
| Client called with `DAIMO_RETRIEVAL_URL`; no AWS SDK imported by `web/` | 5 |
| Each status renders the confidence badge (value, label, threshold) and one card per citation | 6 |
| Snippet containing `<script>`/Markdown link injection renders as literal text | security |

**Retrieval service:** `test_facets.py` checks the response shape and caching.

**Live smoke on EC2:**
- run 5 golden queries and 2 known no-match queries through the port forward;
- the end-to-end time added by the UI must stay under 200 ms compared with calling `curl` directly.

## 10. Phased build plan

| ID | Phase | Task | Hours | Deliverable |
| --- | --- | --- | --- | --- |
| 1.1 | Scaffold | `web/` layout, config, requirements, `.streamlit/config.toml`, title and empty chat | 1 | App runs locally on 127.0.0.1:8501 |
| 1.2 | Scaffold | `client.py` + `state.py` + payload tests against `Options` | 2 | Client and state tested |
| 2.1 | Chat | History rendering, `Turn` model, `render_turn` for all 3 statuses from fixtures | 3 | Threaded conversation working with mocks |
| 2.2 | Chat | Confidence badge + evidence cards + escaping | 2 | Requirement 6 met |
| 3.1 | Filters | Sidebar widgets, persistence, reset, chips per turn and under the title | 2 | Requirement 3 met |
| 3.2 | Filters | `GET /v1/facets` on retrieval + tests; UI cache + static fallback | 2 | Live filter values |
| 4.1 | Hardening | Readiness banner, error turns, double-submit guard | 2 | Error table in §7 covered |
| 4.2 | Tests | AppTest suite for the acceptance table | 3 | Green test run |
| 5.1 | Deploy | `daimo-web.service`, `install.sh`, SSM port-forward runbook; reinstall retrieval with `/v1/facets` | 2 | Running on EC2 |
| 5.2 | Deploy | Live smoke, cutover, delete old `streamlit_client/` | 1 | Old test page retired |

**Total: about 20 hours.** Phases 1–3 can be built against fixtures; 3.2 is the only retrieval-side change. 5.1 depends on everything before it.

## 11. Risks and open questions

**Risks**
- **Streamlit reruns.** Any widget change reruns the whole script, so a sidebar change during a request would interrupt it. Mitigation: `pending_turn` disables the input during a request and survives an interrupted run, and the sidebar warns that filter changes apply to the next turn.
- **Perceived latency.** Without streaming, the operator waits 3–5 s with only a spinner. Acceptable for now; streaming is the fix if feedback says otherwise.
- **Load.** `uvicorn --workers 2` and synchronous handlers allow about 2 parallel LLM calls, which is enough for a small operator group. Revisit if more than ~5 people use it at once.

**Open questions**
1. **Streaming (deferred, requirement 4):** when it's picked up, add `POST /v1/triage/stream` (NDJSON) using `triage_graph.stream(stream_mode="updates")`. It would send stage progress, then confidence and evidence once the gate finishes (~0.5 s), then the answer after citation validation. Raw token streaming is harder: the answer can be retried or withheld after generation, so tokens would need a "provisional" label and a way to retract them. Estimate: about 6 hours for the stage-level version (endpoint, client, UI, tests).
2. **Follow-up questions:** should follow-ups use the earlier turns? That needs a `history` (or condensed-query) field in `TriageRequest` and a query-rewrite step in the graph. It's a contract change owned by retrieval.
3. **User identity:** a fixed `DAIMO_UI_USER` works for now. Should the UI use Streamlit OIDC (`st.login`), or an ALB with Cognito/IdP headers, so that `user.id` in the logs is the real operator?
4. **Debug panel:** should `include_debug` and `min_confidence` override be exposed to admins? They only work when `ALLOW_DEBUG` and `ALLOW_THRESHOLD_OVERRIDE` are on, so they would be hidden unless `/healthz` reports them.
