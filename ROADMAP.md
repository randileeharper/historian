# Historian Status And Roadmap

This document tracks what is implemented, what still needs operational validation, and what remains future work.

## Current State

Historian v1 is implemented as a Python 3.12 service with:

- FastAPI HTTP ingestion and raw-read APIs.
- A2A 1.0 natural-language queries through the official `a2a-sdk`.
- SQLite storage with WAL, foreign keys, indexed fields, and non-destructive migrations.
- Opaque bearer tokens, application identity, token scopes, rotation, and revocation.
- Immutable JSON Schema Draft 2020-12 application manifests.
- Idempotent single-event ingestion and atomic batch ingestion.
- Distinct provenance for literal user messages, assistant messages, and runtime/sidecar events.
- Structured record families for events, transcripts, summaries, user facts, application preferences, errors, statuses, and internal records.
- OpenAI-compatible local-model calls using strict JSON-schema output.
- A fixed query pipeline: the local model selects registered applications and record types with optional local-time bounds and per-type literal text, Historian searches SQLite, and the model synthesizes an answer from compact matching records.
- Bounded correction retries for transport failures, malformed structured output, invalid timestamps, and invented applications or record types.
- Private self-logging of Historian queries without model reasoning.
- Unified debug mode with a startup-scoped operational log and a complete last-query local-model transcript.
- CLI administration, querying, raw inspection, and event emission.
- A synchronous Python client with bounded retries.

There is intentionally no vector search, embedding generation, semantic index, or semantic-search fallback.

The automated suite currently covers configuration, migrations, authentication, schema installation, ingestion, redaction, idempotency, atomic batches, transcript provenance, literal search, regex bounds, fixed-plan queries, A2A transport, HTTP transport, CLI behavior, and resolver wire format.

## Required To Operate Historian

These steps are needed before Historian is a continuously running, useful service:

1. Install dependencies with uv:

   ```console
   uv sync
   ```

2. Create `config.json` from `config.example.json`.

3. Confirm the live local-model configuration:

   - `resolver_base_url`, normally `http://localhost:11434/v1`
   - `resolver_model`, currently expected to be `gemma4:latest`
   - `resolver_api_key`, if the endpoint requires one
   - reasoning and unified debug logging settings

4. Run `uv run historian doctor --live` and verify the model endpoint responds.

5. Start Historian and verify:

   - `GET /healthz`
   - both Agent Card paths
   - authenticated ingestion
   - authenticated raw reads
   - an authenticated A2A question using a real local model
   - operational debug output and the exact last-query prompt/response transcript

6. Exercise the real model against representative records. The fixed planner and structured output are implemented, but prompt behavior should be tested whenever models change. Tune the planner if the model selects irrelevant record types, malformed local timestamps, or overly restrictive literal text.

7. Install application manifests and retain the one-time tokens in each application's secret configuration.

8. Integrate real event producers. Until Vesper, Magpie, Luke, or another app emits events, Historian has no useful history to query.

9. Run Historian persistently. A user-level systemd unit is the expected Linux deployment shape, with:

   - a fixed config path
   - restart-on-failure
   - explicit working directory
   - environment or credential-file handling for model secrets
   - logs available through `journalctl`

10. Add backup and restore instructions for the SQLite database before it contains important history.

## Validation Still Needed

- A real listening-server smoke test outside the Codex sandbox. The sandbox rejected localhost port binding, although the complete HTTP/A2A stack passed through in-process ASGI tests.
- A real Ollama `/chat/completions` query using `gemma4:latest`, inspected through the last-query transcript.
- A clean `uv sync` environment is now installed and the suite exercises the required timeout-capable `regex` package. A packaged installation on another machine still needs a release-time smoke test.
- Long-running concurrency behavior under simultaneous ingestion and queries.
- Database growth measurements with realistic Magpie and Vesper event volumes.
- Recovery behavior after abrupt termination during SQLite writes.
- Token provisioning and rotation in the real application configuration workflow.

## Integration Order

Recommended order:

1. Vesper, because its events are bounded and easy to inspect: playback, sessions, preferences, RPC failures, and worker status.
2. Magpie, including research runs, route decisions, fetch/source outcomes, synthesis completion, cache use, and failures.
3. Luke transcript ingestion, preserving literal user, assistant, and internal/runtime provenance.
4. Luke and sidecar A2A query access with a token containing `query:nlp`.
5. Other channel workers such as Bluesky, mail, reminders, and iMessage.

Each producer integration should follow `HISTORIAN_INTEGRATION.md`.

## Deferred Work

These are not required for initial operation:

- Refactor the local-model query implementation without changing its external behavior:
  - Move generic OpenAI-compatible HTTP, structured-output parsing, retry handling, and transcript recording into `llm.py`.
  - Move search-plan prompt construction, dynamic examples, catalog validation, and timestamp validation into `planner.py`.
  - Move answer prompt construction, evidence shaping, and answer validation into `synthesizer.py`.
  - Replace loosely shaped dictionaries at module boundaries with small typed models such as `SearchPlan`, `PlannedSearch`, `PlannedRecordType`, and `AnswerResult`.
  - Keep retries centralized and policy-driven rather than duplicating correction logic in planner and synthesizer code.
  - Separate wire-format JSON Schema from semantic validation: schemas enforce shape, while domain validators enforce registered apps/types and valid time ranges.
  - Keep prompt examples deterministic and generated from the active catalog, but isolate example selection from prompt prose.
  - Add focused unit tests per layer plus a small end-to-end resolver contract suite.
  - Preserve the fixed plan/search/summarize pipeline, literal-only retrieval, readable debug transcript, and `resolver_max_retries` behavior.
  - Do not introduce a general agent loop, tool-calling framework, semantic search, or a new dependency-heavy orchestration framework as part of this cleanup.
- Automatic conversation summarization.
- Automatic durable-user-fact extraction.
- Automatic application-preference extraction.
- Summary/fact supersession and correction workflows.
- Client-side durable disk spooling.
- Long-term data retention and lifecycle management:
  - Measure event volume and database growth by producer and record type.
  - Define per-record-type retention periods, keeping durable preferences, facts, and summaries longer than high-volume playback and status events.
  - Create rollups or summaries before pruning raw events where historical trends still matter.
  - Add scheduled, bounded pruning with dry-run reporting and explicit exemptions.
  - Document backup, archival, restore, and SQLite compaction (`VACUUM`) behavior.
- PostgreSQL storage.
- MCP.
- Multi-user or multi-tenant authorization.
- TLS termination and remote-network deployment.
- Push notifications or streaming A2A results.
- A web administration interface.
- Metrics and dashboards beyond Historian's own query/event history.

Vector or embedding retrieval is not deferred work. It is outside the design.
