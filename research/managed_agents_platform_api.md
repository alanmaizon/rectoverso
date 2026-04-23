# Day 5 Research: Managed Agents Platform API

Companion to [scaling_managed_agents.md](scaling_managed_agents.md) (the Day-1 *why* — Anthropic's design rationale, session-as-durable-log, decoupled-harness). This doc is the Day-5 *how* — concrete API surfaces as they exist on 2026-04-23, mapped to decisions we need to make for the `AnthropicManagedAgentsSession` Editor commit.

**Sources**
- [Quickstart](https://platform.claude.com/docs/en/managed-agents/quickstart)
- [Define your agent](https://platform.claude.com/docs/en/managed-agents/agent-setup)
- [Session event stream](https://platform.claude.com/docs/en/managed-agents/events-and-streaming)
- [Cloud environment setup](https://platform.claude.com/docs/en/managed-agents/environments)
- [Container reference](https://platform.claude.com/docs/en/managed-agents/cloud-containers)
- [Tools](https://platform.claude.com/docs/en/managed-agents/tools)
- [Sessions API reference](https://platform.claude.com/docs/en/api/beta/sessions)

All requests require the `managed-agents-2026-04-01` beta header (SDK sets it automatically).

---

## The five primitives, concretely

### Agent

Versioned, reusable config — `model`, `system`, `tools`, `mcp_servers`, `skills`, `callable_agents`, `metadata`. Create once, reference by `id` in many sessions. Each update generates a new `version`; pass the current `version` on update to enforce known-state writes.

Our pipeline should create one agent per Tier-2 specialist (Producer, Shot Judge, Audio Agent, Editor Agent, Creative Director) and version them. The Editor agent id lives in `.env` or `state/agents.json`; the orchestrator references it by id, not by re-uploading the prompt each time.

**Model** — `{id: "claude-opus-4-7"}` is what CLAUDE.md pins. Fast mode is available on Opus 4.6 only (`{"id": "claude-opus-4-6", "speed": "fast"}`) — not relevant for us.

**Tools** — `{type: "agent_toolset_20260401"}` gives the full pre-built set (bash, read, write, edit, glob, grep, web_fetch, web_search). For the Editor, we want all of these on. Custom tools are declared at the agent level and executed client-side via the `user.custom_tool_result` event loop — we probably don't need custom tools for the Editor because `npx hyperframes` is already a bash subcommand.

### Environment

Container template (Ubuntu 22.04 LTS, x86_64, 8 GB RAM, 10 GB disk). Pre-installed: Python 3.12+, Node.js 20+, Go, Rust, Java, Ruby, PHP, C/C++, git, curl, jq, tar/zip, ripgrep, make/cmake, tmux, SQLite, PostgreSQL/Redis clients (not servers). Network **disabled by default** — must be enabled in the environment config.

**Packages field** for pre-installation (apt, pip, npm, cargo, gem, go — alphabetical run order). Cached across sessions sharing the same environment.

**Networking** — `unrestricted` (safety blocklist only) vs `limited` (allowlist-based). For the Editor:
- `npx hyperframes` needs npm registry + its chrome download → `allow_package_managers: true` is mandatory (or `unrestricted`).
- `npx hyperframes render` invokes headless Chrome to render frames → Chrome is downloaded at first use (~107 MB per CLAUDE.md); cached across sessions on the same environment.
- No outbound calls to Veo / Kling / Wan / ElevenLabs from the Editor — those are Producer-side dispatches. Editor only reads MP3/MP4 files already on disk.
- Lean `unrestricted` for v1; switch to `limited` with `["registry.npmjs.org", "cdn.googleapis.com"]` before any production deploy.

**Environments are not versioned.** If we update the `packages` list, log it on our side. Multiple sessions can share one environment; each session gets its own isolated container instance.

**Missing from docs, confirmed by probe:** `ffmpeg` isn't in the pre-installed utilities list but is declarable via `packages.apt: ["ffmpeg"]`. Our probe report ([scratch/hyperframes-probe/PROBE_REPORT.md](../scratch/hyperframes-probe/PROBE_REPORT.md)) verified this works end-to-end. Keep the apt declaration.

### Session

Running agent instance within an environment, performing one task. Created with `agent=<id>`, `environment_id=<id>`, optional `title`. Has `status`, cumulative `usage` (input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens), `stats` (active_seconds, duration_seconds).

**Conversation history persists until session is explicitly deleted.** Checkpoints (full container state) are preserved for **30 days after last activity**. To extend, send periodic `user.message` events to reset the inactivity timer. Archive sessions we want to keep but not resume.

**Sessions can be resumed** by sending a new `user.message` to their id. The container state — filesystem, installed packages, agent-created files — survives the idle → running transition. This is materially different from what we assumed in our `film_status: assembling` recovery design (more on this below).

**Resources** — sessions have a separate `/v1/sessions/{id}/resources` API for mounting:
- `BetaManagedAgentsFileResourceParams` — mount a file uploaded via the Files API into the session (`mount_path` field).
- `BetaManagedAgentsGitHubRepositoryResourceParams` — clone a repo into the container with auth bundled (token used at clone time, not exposed to the agent per Day-1 design).
- `BetaManagedAgentsMemoryStoreResourceParam` — attach a memory store (with `access` level + `instructions`).

Resources can be added/removed/updated on a live session. This is how we'd feed the manifest *into* the Editor session: either (a) upload `state/manifest.json` via Files API + mount at `/workspace/state/manifest.json`, or (b) clone our repo via GitHub resource with a scoped token. For v1 the file-resource path is simpler.

### Events

Everything that happens is an event. Full list (from the SessionsEvents models):

**User events** (we send):
- `user.message` — user text input
- `user.interrupt` — stop agent mid-execution
- `user.custom_tool_result` — response to a custom tool call
- `user.tool_confirmation` — approve/deny a tool call when permission policy requires it
- `user.define_outcome` — **define an outcome for the agent to work toward** (this is the verification primitive; no separate docs page exists at `/managed-agents/outcomes` — the docs link 404s — but the event type is real)

**Agent events** (we receive):
- `agent.message` — text response blocks
- `agent.thinking` — extended-thinking progress signal (no content)
- `agent.tool_use` — built-in agent tool invocation
- `agent.tool_result` — result of agent tool
- `agent.mcp_tool_use` / `agent.mcp_tool_result` — MCP tool calls
- `agent.custom_tool_use` — invocation of a client-executed custom tool
- `agent.thread_context_compacted` — context auto-compaction happened (validates Day-1's "durable session, context-engineered harness" design)

**Session events**:
- `session.status_running` — actively working
- `session.status_idle` — paused, awaiting input. Carries a `stop_reason` union: `end_turn` (natural completion), `requires_action` (blocking on user input — tool confirmation or custom tool result; `event_ids` lists which events need response), `retries_exhausted` (`max_iterations` hit), or terminal error.
- `session.status_rescheduled` — recovering from an error
- `session.status_terminated` — ended, either by error or completion
- `session.error` — execution error
- `session.deleted` — terminates the event stream

**Span events** — observability layer:
- `span.model_request_start` / `span.model_request_end` — per model call timing + token usage. Every request produces a pair. `is_error` on the end event signals failures.

**Error types** — `BetaManagedAgentsBillingError` (out of credits — don't retry), `ModelOverloadedError`, `ModelRateLimitedError`, `ModelRequestFailedError`, `MCPAuthenticationFailedError`, `MCPConnectionFailedError`, `UnknownError`. Each has a `retry_status` field: `retrying` (server is auto-retrying), `exhausted` (retry budget blown, turn dead), `terminal` (session will terminate). We just observe — the platform owns the retry logic.

**API surfaces** for events:
- `POST /v1/sessions/{id}/events` — send user events (batched — pass an `events: [...]` array)
- `GET /v1/sessions/{id}/events` — list past events (for auditing / replay)
- `GET /v1/sessions/{id}/events/stream` — SSE stream of new events

### Tools (per-session)

Pre-built `agent_toolset_20260401` includes bash, read, write, edit, glob, grep, web_fetch, web_search. All enabled by default. Disable selectively via `configs: [{name: "web_fetch", enabled: false}]`, or flip the default and allowlist with `default_config: {enabled: false}, configs: [...enabled ones]`.

Custom tools execute client-side. Agent emits `agent.custom_tool_use`, session goes idle with `stop_reason: requires_action`, client responds with `user.custom_tool_result`. This is how you'd add pipeline-specific operations (e.g. "submit a Veo render") as agent-callable tools — but for the Editor, bash + read + write + edit cover `npx hyperframes lint` + `npx hyperframes render` + file manipulation.

---

## Implications for our Editor commit

### Resume model is different than we designed

**What we built** in [film_status.py](../src/producer/film_status.py): on orchestrator startup with `film_status: assembling`, destructive recovery — clear `artifacts/edit/`, transition to pending, let the next trigger re-dispatch from scratch.

**What the platform actually supports**: sessions are resumable by default. A session that went idle can be woken by sending a new `user.message`. The container state (files, packages, in-flight work) survives. This materially changes the recovery semantics:

- **Dead-session recovery** (our current scope): `session.status_terminated` or network drop on our side before we saw the idle event. Check session status via `GET /v1/sessions/{id}`. If `archived_at` is null and status is `idle`, it's recoverable. If terminated, it's not.
- **Destructive recovery** (what we built): still right when the session *itself* terminated, not just our client connection.

**Decision** — keep the destructive recovery as our default, add a non-destructive resume path as an opt-in. Concretely:

1. Manifest needs a new field `edit.session_id` to hold the Managed Agents session id across resume. Schema change.
2. `recover_on_startup` on `assembling`: if `edit.session_id` is set, call `GET /v1/sessions/{id}`. If session is still `idle` and within 30-day checkpoint window, reconnect via event stream. Else fall through to destructive path.
3. Non-destructive resume wins the latency race — container state already has chrome downloaded, normalized assets copied in, any partial composition authored. Destructive starts from zero.

The non-destructive path is **nice to have for v1**. The destructive path is correct; the resume upgrade is a post-hackathon nicety.

### Session lifecycle is event-driven, not RPC

Our `EditorTool.__call__` is currently shaped like a blocking RPC that returns when done. The real session lifecycle is:
1. Create session (fast — no container yet).
2. Send initial `user.message` with the kickoff prompt.
3. **Container provisions lazily on first tool use**, not on session create. First tool call adds ~a few seconds of provisioning latency; subsequent are instant.
4. Stream events. Agent works, invokes tools (which we observe but don't execute for built-in toolset), occasionally emits `agent.thinking`.
5. Watch for `session.status_idle` with `stop_reason: end_turn`. That's "agent thinks it's done."
6. Fetch the session to read cumulative `usage`; compute cost.

Our `EditorTool` dispatcher wraps this loop. Shape stays: input → `dispatch_result` dict. Internals shift from "fire-and-forget request" to "stream-and-aggregate event loop."

### Outcome verification IS a primitive

The `user.define_outcome` event type exists. The dedicated docs page doesn't. We have two options:

- **Use the platform primitive.** Send `user.define_outcome` with a rubric description after the initial message. The platform presumably scores the agent's final state against the rubric and reports pass/fail on idle. We'd need to probe the exact event shape (`content` field? `rubric` field?). Not risk-free without docs.
- **Keep our `EDITOR_RESULT:` marker parser** ([src/producer/editor.py](../src/producer/editor.py) `unparseable_verdict` / `agent_reported_fail` failure stages). Works today, tested, inspectable. No reliance on undocumented event shapes.

**Decision** — stay with our marker parser for v1. Migrate to `user.define_outcome` when the docs page materializes or we confirm the event shape via a probe. The marker approach is one of those "we had to build what the platform now provides" moments Anthropic flagged in the Day-1 post — but it works *today*, and replacing it isn't on the critical path.

### Artifact extraction: not a primitive

**No documented API for "download files from the session's container to the host."** The session's filesystem is what the agent writes to; checkpoints preserve it for 30 days; but getting `artifacts/edit/out.mp4` *out* is our problem.

Options:

1. **Have the Editor agent upload artifacts via web_fetch to our control server**. Requires a control endpoint, auth, chunked upload for large MP4s. Complex; gives us observability. Probably overkill.
2. **Mount a shared resource (Files API) at a known path, agent writes into it, we download via Files API post-session**. Files API must support upload direction from container — unclear from docs whether a mounted `FileResource` is bidirectional. Worth a probe.
3. **Have the agent run a final bash step that `tar czf /tmp/out.tar.gz artifacts/edit/ && curl -T /tmp/out.tar.gz <our-upload-url>`**. Explicit, simple, requires an upload endpoint on our side but we already have the pipeline running locally.
4. **Skip download; verify success via session event + use manifest-level metadata**. Keep the MP4 inside the session container until the operator manually extracts it via `ant` CLI (if such a command exists — docs don't say explicitly). Crap for production, fine for demo.

**Decision — option (3), and add option (4) as a fallback for demo mode.** Specifically: our Editor agent's final bash step uploads `artifacts/edit.tar.gz` to a signed-URL endpoint we stand up; `EditorTool` polls that endpoint post-session for the artifact; writes `render_md5` from the local file.

Standing up the upload endpoint is real work. Worth probing whether Files API (`FileResource.mount_path`) supports reverse upload before committing to option (3).

### Cost accounting is session-level, not per-event

The session's `usage` field gives cumulative `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`. Fetch session on idle, compute USD from token rates. Per-request spans exist (`BetaManagedAgentsSpanModelUsage`) if we want per-turn cost attribution.

Our `budget.spent_usd` threading should use session.usage, not sum of span events. Simpler, and spans are observability-only.

**Cost formula** — Opus 4.7 input $15/MTok, output $75/MTok (current public pricing). Cache creation $18.75/MTok (1.25x input), cache read $1.50/MTok (0.1x input). Formula:

```
cost_usd = (
    (input_tokens * 15.0 / 1_000_000) +
    (output_tokens * 75.0 / 1_000_000) +
    (cache_creation_input_tokens * 18.75 / 1_000_000) +
    (cache_read_input_tokens * 1.50 / 1_000_000)
)
```

Put this in `src/producer/_common.py` as `compute_opus_47_cost(usage)`. Use it from `EditorTool` and any other session-based dispatcher.

### Container state between invocations

**Cached across sessions sharing the same environment**: pre-installed packages from `packages:` config. So `apt: ["ffmpeg"]` is installed once per environment, not per session.

**NOT cached**: `npx hyperframes` at the per-session level. Chrome download happens inside the container; each fresh container does it again. Workaround: declare `packages.npm: ["hyperframes"]` at the environment level so `npx` doesn't re-resolve. Chrome download still happens per-container but is a one-time ~107 MB hit per session lifetime.

**Checkpoints preserve the full container state** for 30 days. If we resume a session, Chrome is already downloaded. Material for iteration loops during development; not material for first-shot Editor dispatch on demo day.

### Billing errors are terminal

`BetaManagedAgentsBillingError` has `retry_status` but the docs say *"retrying with the same credentials will not succeed; the caller must resolve the billing state."* Our $500 Anthropic budget is finite; a budget blow-up mid-Editor session means the session terminates and we've spent ~half the budget to get there. Defense-in-depth:

- Pre-check budget before dispatch (already done in [budget.py](../src/producer/budget.py) `check_before_editor`).
- Kill-switch on partial consumption: if a running session's `usage` projects a final cost > `editor_estimate_usd * 2`, send `user.interrupt` and transition to `compose_failed`. We don't have this yet. Worth adding when the real session lands.

---

## Concrete shape for `AnthropicManagedAgentsSession`

Putting the findings together, here's what the class looks like, for reference when the commit lands:

```python
class AnthropicManagedAgentsSession:
    """Real implementation of the EditorSession Protocol.

    Spawns a Claude Managed Agents session, streams events, drains to
    idle/end_turn, extracts the EDITOR_RESULT: marker from the transcript,
    fetches final usage for cost accounting, and returns a dispatch_result
    dict to EditorTool.
    """

    def __init__(
        self,
        *,
        anthropic_client,                    # anthropic.Anthropic()
        agent_id: str,                       # pre-created agent id (env var)
        environment_id: str,                 # pre-created env id (env var)
        timeout_s: float = 7200.0,           # 2h hard cap per EditorTool
        kill_switch_multiplier: float = 2.0, # interrupt if projected > 2x estimate
    ) -> None:
        ...

    def run(
        self,
        *,
        manifest_path: Path,
        workspace_dir: Path,
        brief_slice: dict,
        estimated_cost_usd: float,
    ) -> dict:
        # 1. Create session
        session = self._client.beta.sessions.create(
            agent=self._agent_id,
            environment_id=self._environment_id,
            title=f"editor-{manifest_path.parent.name}",
        )

        # 2. Attach manifest + workspace as resources
        #    (Files API upload + mount OR GitHub repo resource)
        self._attach_resources(session.id, manifest_path, workspace_dir)

        # 3. Send initial message
        self._client.beta.sessions.events.send(session.id, events=[{
            "type": "user.message",
            "content": [{"type": "text", "text": self._kickoff_prompt(brief_slice)}],
        }])

        # 4. Drain event stream until idle end_turn or terminal
        transcript, terminated = self._drain_stream(
            session.id,
            estimated_cost_usd=estimated_cost_usd,
        )

        # 5. Fetch final usage, compute cost
        final = self._client.beta.sessions.retrieve(session.id)
        cost_usd = compute_opus_47_cost(final.usage)

        # 6. Parse EDITOR_RESULT: marker from transcript
        verdict = parse_editor_result(transcript)

        # 7. Extract artifacts (option 3: agent uploads to our endpoint)
        #    OR (option 4: skip, return path inside container for manual extract)
        artifacts = self._extract_artifacts(session.id)

        # 8. Return dispatch_result dict
        return {...}
```

Anthropic SDK is [`anthropic` on PyPI](https://pypi.org/project/anthropic/); the Python SDK exposes `client.beta.sessions.*` once the `managed-agents-2026-04-01` beta header is set (SDK auto-sets).

---

## Open questions (flag before real-session commit)

1. **Files API bidirectional?** Can a container write to a mounted `FileResource` and have us read it post-session? If yes, artifact extraction is free. If no, we build the upload endpoint.
2. **`user.define_outcome` event shape?** No docs page. Probe a minimal agent with an outcome, inspect the returned event to reverse-engineer the schema.
3. **Span events vs session.usage** — which is authoritative for cost? Both should agree; if they diverge, we need to know which one the billing team blesses.
4. **Archival vs delete** — archived sessions stay readable; deleted sessions terminate streams. For our demo-fixture capture, archive after success.
5. **Concurrent sessions** — no rate-limit docs on how many sessions can run concurrently per org. Relevant for Shot Judge parallelism (we plan parallel per-shot judges) but not for Editor (single session).

---

## What this changes in our commit plan

Previously I proposed:
- Commit 1: `AnthropicManagedAgentsSession` productization.
- Commit 2: `DEMO_MODE` fixture path.

Revised based on the actual API surface:

- **Commit 1: Probe — answer open question 1 + 2.** Stand up `scratch/managed_agents_editor_probe.py` — create an agent, create an env, spawn a session with `apt: ["ffmpeg"], npm: ["hyperframes"]`, send a tiny kickoff ("echo hello to /workspace/hello.txt"), drain stream, verify we can read `hello.txt` back somehow. Output: a probe report that settles the Files-API-bidirectional question and the `user.define_outcome` shape. Before spending on a real Editor session, spend tiny on a hello-world.
- **Commit 2: `AnthropicManagedAgentsSession` productization.** With the probe results in hand, real session code. `anthropic` SDK dependency, session create, resource attachment, stream drain, cost accounting, artifact extraction using whichever mechanism the probe showed works.
- **Commit 3: `DEMO_MODE` fixture path.** Captured from a live commit-2 run. Replays the event stream + extracted artifacts for deterministic demo-day playback.

Commit 1 costs pennies (minimal tokens on a trivial session). Commits 2 and 3 can slip without blocking the demo if we keep `ToolSet.editor=None` wiring + the sub-film pipeline path we already have.
