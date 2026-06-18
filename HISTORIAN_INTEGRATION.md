# Historian Producer Integration Handoff

Copy this file into an application repository and give it to the implementing Codex session. It contains the complete Historian integration contract for Vesper, Magpie, or another local agent application.

## Objective

Add optional Historian support without moving domain behavior into Historian and without making Historian a broker for normal application execution.

The application continues to own its domain. It performs its normal work, then emits structured records describing what happened. Historian stores and queries those records.

Implement logging once at the service/provider boundary so every transport gets identical behavior. Do not add separate logging logic to CLI, A2A, MCP, HTTP, or background-worker adapters.

Historian event writes use ordinary authenticated HTTP. A2A is for natural-language queries, not event ingestion.

When debugging an integration, enable Historian's `debug_enabled` setting. Inspect the configured operational log for accepted/rejected event metadata and the resolver log for the complete latest A2A query prompt/response chain. Producer applications must still log their own delivery failures locally.

## Runtime Contract

Default Historian URL:

```text
http://127.0.0.1:8768
```

Event endpoint:

```http
POST /v1/events
Authorization: Bearer hist_...
Content-Type: application/json
```

Atomic batch endpoint:

```http
POST /v1/events:batch
Authorization: Bearer hist_...
Content-Type: application/json

{"events": [...]}
```

Health check:

```http
GET /healthz
```

Successful single-event response:

```json
{
  "status": "ok",
  "duplicate": false,
  "event": {}
}
```

Historian uses the bearer token to determine producer identity. Never send or trust an `app` field as identity. A Vesper token can write only sources equal to or below `app://vesper`; a Magpie token can write only below `app://magpie`.

## Application Configuration

Add these settings using the application's existing strict config and environment-variable conventions:

```json
{
  "historian_enabled": false,
  "historian_base_url": "http://127.0.0.1:8768",
  "historian_token": null,
  "historian_timeout_seconds": 5.0,
  "historian_verify_tls": true,
  "historian_retry_count": 2
}
```

Environment mappings should use the current project prefix:

```text
VESPER_HISTORIAN_ENABLED
VESPER_HISTORIAN_BASE_URL
VESPER_HISTORIAN_TOKEN
VESPER_HISTORIAN_TIMEOUT_SECONDS
VESPER_HISTORIAN_VERIFY_TLS
VESPER_HISTORIAN_RETRY_COUNT
```

or:

```text
MAGPIE_HISTORIAN_ENABLED
MAGPIE_HISTORIAN_BASE_URL
MAGPIE_HISTORIAN_TOKEN
MAGPIE_HISTORIAN_TIMEOUT_SECONDS
MAGPIE_HISTORIAN_VERIFY_TLS
MAGPIE_HISTORIAN_RETRY_COUNT
```

Rules:

- Reject unknown config fields as usual.
- Require a token when `historian_enabled=true`.
- Strip trailing `/` from the base URL.
- Require positive timeout and non-negative retry count.
- Sanitized diagnostics must expose `has_historian_token`, never the token itself.
- Keep Historian disabled by default so existing installations continue working.

## Provider Boundary

Add a small application-local provider. Do not copy Historian's storage or query code into the producer.

Recommended interface:

```python
class HistorianSink(Protocol):
    def emit(self, event: dict[str, Any]) -> None: ...
    def emit_batch(self, events: list[dict[str, Any]]) -> None: ...
```

Implement:

- `HttpHistorianSink` using `httpx`.
- `NullHistorianSink` when disabled.
- `FakeHistorianSink` for tests, retaining emitted events in memory.

The producer does not need to depend on the `historian` Python package. A small local `httpx` provider avoids coupling package release/install cycles. If both projects later become installable shared packages, this can be revisited.

Use bounded retries for connection failures and HTTP 5xx responses. Reuse the same event ID for every retry. Do not retry validation/auth failures such as 400, 401, 403, 409, or 422.

### Failure Policy

Historian is observability/continuity infrastructure, not the authority for the domain action.

- A successful music action or research result must not be changed into a domain failure only because Historian is unavailable.
- Failed emission must be visible in the application's normal logs/debug trace.
- Do not silently swallow failures.
- Do not recursively try to report a Historian-delivery failure to Historian.
- Do not add a durable disk spool in this integration; that is deferred.
- For a background worker, catch emission errors at the worker boundary so the worker remains alive.

Emit completion records after durable/domain state is committed. Emit error records from the exception boundary before converting the exception into the application's normal domain error.

## Event Envelope

Every event is CloudEvents-shaped JSON:

```json
{
  "specversion": "1.0",
  "id": "stable-unique-event-id",
  "source": "app://vesper/playback",
  "type": "music.playback.started",
  "time": "2026-06-17T15:00:00Z",
  "schemaversion": 1,
  "subject": "optional-domain-subject",
  "correlationid": "optional-operation-or-run-id",
  "causationid": "optional-parent-event-id",
  "sessionid": "optional-session-id",
  "visibility": "private",
  "data": {}
}
```

Required fields:

- `specversion`: always `"1.0"`
- `id`: stable across retries
- `source`: `app://<app-id>` or a descendant path
- `type`: namespaced registered event type
- `time`: timezone-aware ISO-8601 occurrence time
- `schemaversion`: positive integer
- `data`: JSON object matching the registered schema exactly

Optional fields:

- `subject`: stable domain object identifier
- `correlationid`: groups all records for one operation, request, research run, or adaptive session
- `causationid`: event ID that directly caused this event
- `sessionid`: application session identity
- `visibility`: `private` by default; `shared` is accepted but has no distinct read policy in v1

Use UUIDv4 or an existing durable run/event ID. For lifecycle records, each transition gets its own event ID but shares a correlation ID.

Do not put secrets, bearer tokens, API keys, authorization headers, email bodies not intended for history, or unrestricted raw model output into events. `redacted_fields` is defense in depth, not permission to send secrets.

## Manifest

Add an application manifest to the producer repository, conventionally:

```text
historian.manifest.json
```

Top-level shape:

```json
{
  "app_id": "vesper",
  "description": "Music-control agent for Cider and Apple Music.",
  "default_scopes": ["events:write"],
  "schemas": []
}
```

For a pure producer, use only `events:write`. Add `events:read` only if the application needs exact raw reads. Add `query:nlp` only if it will ask Historian natural-language questions through A2A.

Each schema contains:

```json
{
  "event_type": "music.playback.started",
  "version": 1,
  "record_family": "event",
  "description": "Music playback started.",
  "searchable_fields": ["request", "track.title", "track.artist"],
  "redacted_fields": [],
  "json_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": false
  }
}
```

Allowed record families:

```text
event
transcript
summary
user_fact
app_preference
error
status
internal
```

Manifest rules:

- Event types must be namespaced and contain a period.
- Schema versions are immutable. If meaning or shape changes, create version 2.
- Use `additionalProperties: false`.
- Put fields likely to be searched in `searchable_fields`.
- Searchable fields support dotted object paths.
- Do not mark large raw payloads as searchable.
- `redacted_fields` supports dotted object paths.
- Keep event payloads compact and domain-specific.

The administrator installs the manifest from the Historian checkout:

```console
historian app install /path/to/application/historian.manifest.json
```

This prints the default token once and revokes the previous default token for that app. Store the token in the application config or environment.

## Shared Core Schemas

Historian already provides these `core.*` schemas to every registered application:

- `core.transcript.user_message` v1
- `core.transcript.assistant_message` v1
- `core.runtime.internal_event` v1
- `core.conversation.summary` v1
- `core.user.fact` v1
- `core.application.preference` v1
- `core.operation.error` v1
- `core.operation.status` v1

Do not redefine these in the application manifest.

Critical provenance rule:

- Use `core.transcript.user_message` only for words literally authored by the human user.
- Use `core.transcript.assistant_message` only for an assistant message actually emitted to the user.
- Use `core.runtime.internal_event` for sidecar requests, runtime injections, tool instructions, background findings, and worker-to-runtime messages.
- Never store an internal/runtime message as a user message merely because it was rendered as a user-role `/chat/completions` message.

## Instrumentation Pattern

Create event-building helpers in the application, but keep domain payload construction near the domain operation so fields remain accurate.

Recommended lifecycle:

```text
request received
  -> optional *.started event
  -> domain work and durable state change
  -> *.completed / domain-specific success event

exception
  -> core.operation.error or app-specific *.failed event
  -> existing application error conversion
```

Do not emit a noisy event for every internal function call. Prefer events that answer:

- What did the application do?
- Why did it do it?
- What was the result?
- What failed?
- What durable preference or state changed?
- Which request/run/session caused it?

Use one correlation ID throughout an operation. Reuse existing A2A task IDs, research run IDs, adaptive-session IDs, or request IDs where available.

## Recommended Vesper Events

The implementing session should inspect the real Vesper service and storage flows, then create schemas for the events actually available. Recommended minimum:

```text
music.request.received
music.playback.started
music.playback.paused
music.playback.stopped
music.track.skipped
music.session.started
music.session.steered
music.session.track_selected
music.session.ended
music.preference.recorded
music.preference.forgotten
music.rpc.failed
music.worker.started
music.worker.stopped
```

Important Vesper payload fields:

- original natural-language request
- resolved action
- track ID, title, artist, album, and source kind
- playlist ID/name where applicable
- adaptive session ID and request
- steering text
- preference type, target, polarity, and reason
- RPC operation and sanitized error
- whether playback was initiated by Luke, sidecar, CLI, or another caller when known

Do not emit Cider API tokens, request authorization headers, or unrestricted RPC payloads.

Instrument the service and background session worker rather than only A2A handlers. Playback caused by background refill must be recorded too.

## Recommended Magpie Events

Recommended minimum:

```text
research.run.started
research.route.selected
research.query.executed
research.source.discovered
research.source.fetched
research.source.rejected
research.cache.hit
research.synthesis.completed
research.run.completed
research.run.partial
research.run.failed
research.run.canceled
```

Important Magpie payload fields:

- research run ID
- original question
- selected route
- normalized search query
- provider
- source ID, canonical URL, title, and published/fetched timestamps
- cache freshness class
- source rejection reason
- final status and stop reason
- reference/source IDs used in the answer
- counts and timing summaries
- sanitized error type and message

Do not emit provider API keys, authorization headers, full fetched documents, unrestricted raw provider payloads, or hidden model reasoning. Historian needs operational history, not a duplicate of Magpie's entire source cache.

Instrument `ResearchService` stages and existing durable run transitions. Reuse the Magpie run ID as `correlationid`.

## A2A Queries From An Application

Most Vesper/Magpie integration work is event production. If an application also needs to query Historian:

- Provision `query:nlp`.
- Resolve Historian's public Agent Card.
- Send a text/plain A2A message.
- Include `Authorization: Bearer <token>` on Agent Card resolution and A2A requests.
- Treat statuses `ok`, `partial`, `insufficient_evidence`, and `error` explicitly.
- Natural-language query responses contain the synthesized answer and search metadata. Use the raw event APIs when exact underlying records are required.

Historian's Python `HistorianClient.query()` demonstrates the official SDK pattern, but copying that client is optional.

Do not use A2A to write events.

## Tests Required In The Producer

Add tests for:

- Config defaults, environment overrides, token redaction, and enabled-without-token rejection.
- Disabled mode using `NullHistorianSink` with no network calls.
- Correct bearer header, URL, timeout, TLS, and retry behavior.
- Stable event IDs across retries.
- No retry on authentication, authorization, conflict, or validation responses.
- Fake sink capture from direct service calls.
- The same service action invoked through CLI/A2A producing one event, not duplicated transport-specific events.
- Success events after domain state changes.
- Error events on provider/RPC/fetch failures.
- Correlation IDs shared across lifecycle events.
- Manifest validation and exact agreement between emitted payloads and schemas.
- No secrets in representative events.
- Historian unavailability not changing an otherwise successful domain result.

## Acceptance Checklist

The integration is complete when:

1. `historian.manifest.json` exists and installs successfully.
2. The generated token is represented in config only as a secret.
3. Historian support is optional and disabled by default.
4. Direct service tests emit expected records through a fake sink.
5. A running application emits a real event accepted by Historian.
6. Repeating the same event ID is reported as a duplicate, not stored twice.
7. `historian events --token ... list --app <app-id>` shows the record.
8. An A2A question such as “What did Vesper do this morning?” or “Why did Magpie fail?” returns an answer citing the emitted event ID.
9. Stopping Historian produces visible delivery warnings but does not break successful domain operations.
10. No event contains secrets or confuses internal runtime activity with literal user speech.
