"""SQLite persistence, identity, ingestion, and literal search."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Protocol

from .errors import AuthenticationError, ConflictError, StorageError, ValidationError
from .debug import get_logger
from .models import (
    AppManifest,
    AuthPrincipal,
    EventEnvelope,
    SchemaDefinition,
    SearchSpec,
    StoredEvent,
    utc_now,
)

try:
    import regex as timeout_regex
except ImportError:  # Development fallback; regex is a required package in installed builds.
    timeout_regex = None


SCHEMA_VERSION = 1
_LOG = get_logger("storage")


class HistorianStore(Protocol):
    def install_app(self, manifest: AppManifest) -> str: ...
    def authenticate(self, token: str) -> AuthPrincipal: ...
    def ingest(self, principal: AuthPrincipal, event: EventEnvelope) -> tuple[StoredEvent, bool]: ...
    def search(self, spec: SearchSpec) -> list[StoredEvent]: ...
    def get_event(self, event_id: str) -> StoredEvent | None: ...


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return f"hist_{secrets.token_urlsafe(32)}"


def _parse_timestamp(value: str, name: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{name} must include a timezone.")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _path_get(payload: Any, path: str) -> Any:
    current = payload
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


def _path_redact(payload: dict[str, Any], path: str) -> None:
    segments = path.split(".")
    current: Any = payload
    for segment in segments[:-1]:
        if not isinstance(current, dict) or segment not in current:
            return
        current = current[segment]
    if isinstance(current, dict) and segments[-1] in current:
        current[segments[-1]] = "[REDACTED]"


def _field_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return _canonical_json(value)


def _render_canonical_text(event: EventEnvelope, schema: SchemaDefinition, data: dict[str, Any]) -> str:
    lines = [
        f"source: {event.source}",
        f"type: {event.event_type}",
        f"time: {event.occurred_at}",
        f"family: {schema.record_family}",
    ]
    for name, value in (
        ("subject", event.subject),
        ("correlation_id", event.correlation_id),
        ("causation_id", event.causation_id),
        ("session_id", event.session_id),
    ):
        if value:
            lines.append(f"{name}: {value}")
    for path in schema.searchable_fields:
        value = _path_get(data, path)
        if value is not None:
            lines.append(f"{path}: {_field_text(value)}")
    return "\n".join(lines)


class SQLiteHistorianStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_lock = threading.Lock()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._initialize_lock:
            try:
                with self._connect() as connection:
                    connection.execute("PRAGMA journal_mode = WAL")
                    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if current > SCHEMA_VERSION:
                        raise StorageError(
                            f"Database schema version {current} is newer than supported version {SCHEMA_VERSION}."
                        )
                    if current < 1:
                        connection.executescript(_MIGRATION_1)
                        connection.execute("PRAGMA user_version = 1")
                    _LOG.debug(
                        "database=%s storage_initialized schema_version=%s wal=true",
                        self.database_path,
                        SCHEMA_VERSION,
                    )
            except sqlite3.Error as exc:
                _LOG.exception("database=%s storage_initialization_failed", self.database_path)
                raise StorageError(f"Could not initialize SQLite database {self.database_path}: {exc}") from exc

    def install_app(self, manifest: AppManifest) -> str:
        token = _new_token()
        now = utc_now()
        try:
            with self._connect() as connection:
                self._ensure_manifest_connection(connection, manifest, now)
                connection.execute(
                    "UPDATE tokens SET revoked_at=? WHERE app_id=? AND name='default' AND revoked_at IS NULL",
                    (now, manifest.app_id),
                )
                connection.execute(
                    """INSERT INTO tokens(token_id, app_id, name, token_hash, scopes_json, created_at)
                       VALUES (?, ?, 'default', ?, ?, ?)""",
                    (
                        secrets.token_hex(16),
                        manifest.app_id,
                        _token_hash(token),
                        _canonical_json(manifest.default_scopes),
                        now,
                    ),
                )
        except sqlite3.Error as exc:
            _LOG.exception("app_id=%s app_install_failed", manifest.app_id)
            raise StorageError(f"Could not install app {manifest.app_id}: {exc}") from exc
        _LOG.info(
            "app_id=%s schemas=%s scopes=%s app_installed",
            manifest.app_id,
            len(manifest.schemas),
            manifest.default_scopes,
        )
        return token

    def ensure_manifest(self, manifest: AppManifest) -> None:
        now = utc_now()
        try:
            with self._connect() as connection:
                self._ensure_manifest_connection(connection, manifest, now)
        except sqlite3.Error as exc:
            _LOG.exception("app_id=%s schema_install_failed", manifest.app_id)
            raise StorageError(f"Could not install schemas for {manifest.app_id}: {exc}") from exc
        _LOG.debug("app_id=%s schemas=%s schemas_ensured", manifest.app_id, len(manifest.schemas))

    @staticmethod
    def _ensure_manifest_connection(
        connection: sqlite3.Connection, manifest: AppManifest, now: str
    ) -> None:
        connection.execute(
            """INSERT INTO apps(app_id, description, created_at, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(app_id) DO UPDATE SET description=excluded.description, updated_at=excluded.updated_at""",
            (manifest.app_id, manifest.description, now, now),
        )
        for schema in manifest.schemas:
            payload = _canonical_json(asdict(schema))
            existing = connection.execute(
                "SELECT definition_json FROM schemas WHERE app_id=? AND event_type=? AND version=?",
                (manifest.app_id, schema.event_type, schema.version),
            ).fetchone()
            if existing and existing["definition_json"] != payload:
                raise ConflictError(
                    f"Schema {schema.event_type} v{schema.version} is immutable and differs from the installed definition."
                )
            connection.execute(
                """INSERT OR IGNORE INTO schemas
                   (app_id, event_type, version, record_family, description, definition_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    manifest.app_id,
                    schema.event_type,
                    schema.version,
                    schema.record_family,
                    schema.description,
                    payload,
                    now,
                ),
            )

    def create_token(self, app_id: str, scopes: list[str], name: str = "manual") -> str:
        token = _new_token()
        with self._connect() as connection:
            exists = connection.execute("SELECT 1 FROM apps WHERE app_id=?", (app_id,)).fetchone()
            if not exists:
                raise ValidationError(f"Unknown app {app_id}.")
            connection.execute(
                "INSERT INTO tokens(token_id, app_id, name, token_hash, scopes_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (secrets.token_hex(16), app_id, name, _token_hash(token), _canonical_json(scopes), utc_now()),
            )
        return token

    def revoke_tokens(self, app_id: str, name: str | None = None) -> int:
        parameters: list[Any] = [utc_now(), app_id]
        sql = "UPDATE tokens SET revoked_at=? WHERE app_id=? AND revoked_at IS NULL"
        if name:
            sql += " AND name=?"
            parameters.append(name)
        with self._connect() as connection:
            cursor = connection.execute(sql, parameters)
            return cursor.rowcount

    def list_apps(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT a.app_id, a.description, a.created_at, a.updated_at,
                          COUNT(DISTINCT s.event_type || ':' || s.version) AS schema_count,
                          COUNT(DISTINCT CASE WHEN t.revoked_at IS NULL THEN t.token_id END) AS active_token_count
                   FROM apps a
                   LEFT JOIN schemas s ON s.app_id=a.app_id
                   LEFT JOIN tokens t ON t.app_id=a.app_id
                   GROUP BY a.app_id ORDER BY a.app_id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def list_schemas(self, app_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT app_id, event_type, version, record_family, description, created_at FROM schemas"
        parameters: list[Any] = []
        if app_id:
            sql += " WHERE app_id=?"
            parameters.append(app_id)
        sql += " ORDER BY app_id, event_type, version"
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(sql, parameters).fetchall()]

    def search_catalog(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT app_id, event_type, version, record_family, description, definition_json FROM schemas ORDER BY app_id, event_type, version"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            definition = json.loads(row["definition_json"])
            result.append(
                {
                    "app_id": row["app_id"],
                    "event_type": row["event_type"],
                    "version": row["version"],
                    "record_family": row["record_family"],
                    "description": row["description"],
                    "searchable_fields": definition.get("searchable_fields", []),
                }
            )
        return result

    def query_catalog(self) -> list[dict[str, Any]]:
        """Return only the app descriptions and record-type names needed by the planner."""
        with self._connect() as connection:
            apps = connection.execute(
                "SELECT app_id, description FROM apps ORDER BY app_id"
            ).fetchall()
            schemas = connection.execute(
                "SELECT DISTINCT app_id, event_type FROM schemas ORDER BY app_id, event_type"
            ).fetchall()
        record_types: dict[str, list[str]] = {}
        for row in schemas:
            record_types.setdefault(row["app_id"], []).append(row["event_type"])
        return [
            {
                "app": row["app_id"],
                "description": row["description"],
                "record_types": record_types.get(row["app_id"], []),
            }
            for row in apps
        ]

    def authenticate(self, token: str) -> AuthPrincipal:
        if not token:
            raise AuthenticationError("Bearer token is required.")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT token_id, app_id, scopes_json FROM tokens WHERE token_hash=? AND revoked_at IS NULL",
                (_token_hash(token),),
            ).fetchone()
        if not row:
            _LOG.warning("authentication_failed token_present=%s", bool(token))
            raise AuthenticationError("Bearer token is invalid or revoked.")
        _LOG.debug("app_id=%s token_id=%s authentication_succeeded", row["app_id"], row["token_id"])
        return AuthPrincipal(row["app_id"], row["token_id"], frozenset(json.loads(row["scopes_json"])))

    def get_schema(
        self, app_id: str, event_type: str, version: int
    ) -> tuple[str, SchemaDefinition] | None:
        candidates = [app_id]
        if event_type.startswith("core.") and app_id != "historian":
            candidates.append("historian")
        placeholders = ",".join("?" for _ in candidates)
        with self._connect() as connection:
            row = connection.execute(
                f"""SELECT app_id, definition_json FROM schemas
                    WHERE event_type=? AND version=? AND app_id IN ({placeholders})
                    ORDER BY CASE WHEN app_id=? THEN 0 ELSE 1 END LIMIT 1""",
                (event_type, version, *candidates, app_id),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["definition_json"])
        return row["app_id"], SchemaDefinition(**payload)

    def ingest(self, principal: AuthPrincipal, event: EventEnvelope) -> tuple[StoredEvent, bool]:
        prepared = self._prepare_event(principal, event)
        try:
            with self._connect() as connection:
                return self._insert_prepared(connection, principal, event, prepared)
        except sqlite3.Error as exc:
            raise StorageError(f"Could not ingest event: {exc}") from exc

    def ingest_batch(
        self, principal: AuthPrincipal, events: list[EventEnvelope]
    ) -> list[tuple[StoredEvent, bool]]:
        prepared = [(event, self._prepare_event(principal, event)) for event in events]
        try:
            with self._connect() as connection:
                return [
                    self._insert_prepared(connection, principal, event, item)
                    for event, item in prepared
                ]
        except sqlite3.Error as exc:
            raise StorageError(f"Could not ingest event batch: {exc}") from exc

    def _prepare_event(
        self, principal: AuthPrincipal, event: EventEnvelope
    ) -> tuple[str, SchemaDefinition, dict[str, Any], str, str]:
        event.occurred_at = _parse_timestamp(event.occurred_at, "time")
        expected_source = f"app://{principal.app_id}"
        if event.source != expected_source and not event.source.startswith(expected_source + "/"):
            raise ValidationError(f"source must equal or descend from {expected_source}.")
        schema_result = self.get_schema(principal.app_id, event.event_type, event.schema_version)
        if schema_result is None:
            raise ValidationError(
                f"Schema {event.event_type} v{event.schema_version} is not registered for {principal.app_id}."
            )
        schema_app_id, schema = schema_result

        from jsonschema import Draft202012Validator

        errors = sorted(Draft202012Validator(schema.json_schema).iter_errors(event.data), key=lambda item: list(item.path))
        if errors:
            first = errors[0]
            location = ".".join(str(item) for item in first.path) or "$"
            raise ValidationError(f"Event data failed schema validation at {location}: {first.message}")

        stored_data = json.loads(_canonical_json(event.data))
        for path in schema.redacted_fields:
            _path_redact(stored_data, path)
        canonical_text = _render_canonical_text(event, schema, stored_data)
        envelope_hash = hashlib.sha256(
            _canonical_json(
                {
                    "specversion": event.specversion,
                    "id": event.event_id,
                    "source": event.source,
                    "type": event.event_type,
                    "time": event.occurred_at,
                    "schemaversion": event.schema_version,
                    "subject": event.subject,
                    "correlationid": event.correlation_id,
                    "causationid": event.causation_id,
                    "sessionid": event.session_id,
                    "visibility": event.visibility,
                    "data": stored_data,
                }
            ).encode("utf-8")
        ).hexdigest()
        return schema_app_id, schema, stored_data, canonical_text, envelope_hash

    def _insert_prepared(
        self,
        connection: sqlite3.Connection,
        principal: AuthPrincipal,
        event: EventEnvelope,
        prepared: tuple[str, SchemaDefinition, dict[str, Any], str, str],
    ) -> tuple[StoredEvent, bool]:
        schema_app_id, schema, stored_data, canonical_text, envelope_hash = prepared
        recorded_at = utc_now()
        existing = connection.execute(
            "SELECT envelope_hash FROM events WHERE producer_app_id=? AND source=? AND event_id=?",
            (principal.app_id, event.source, event.event_id),
        ).fetchone()
        if existing:
            if existing["envelope_hash"] != envelope_hash:
                raise ConflictError("Event id was reused with different content.")
            stored = self._get_event_with_connection(connection, event.event_id, principal.app_id, event.source)
            if stored is None:
                raise StorageError("Duplicate event could not be reloaded.")
            return stored, True
        cursor = connection.execute(
            """INSERT INTO events
               (event_id, producer_app_id, schema_app_id, source, event_type, schema_version, record_family,
                occurred_at, recorded_at, subject, correlation_id, causation_id, session_id,
                visibility, data_json, canonical_text, envelope_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id,
                principal.app_id,
                schema_app_id,
                event.source,
                event.event_type,
                event.schema_version,
                schema.record_family,
                event.occurred_at,
                recorded_at,
                event.subject,
                event.correlation_id,
                event.causation_id,
                event.session_id,
                event.visibility,
                _canonical_json(stored_data),
                canonical_text,
                envelope_hash,
            ),
        )
        event_rowid = int(cursor.lastrowid)
        for path in schema.searchable_fields:
            value = _path_get(stored_data, path)
            if value is not None:
                connection.execute(
                    "INSERT INTO event_fields(event_rowid, field_path, value_text) VALUES (?, ?, ?)",
                    (event_rowid, path, _field_text(value)),
                )
        stored = self._get_event_with_connection(connection, event.event_id, principal.app_id, event.source)
        if stored is None:
            raise StorageError("Stored event could not be reloaded.")
        return stored, False

    def ingest_internal(self, event: EventEnvelope) -> tuple[StoredEvent, bool]:
        principal = AuthPrincipal("historian", "internal", frozenset({"events:write", "events:read", "query:nlp"}))
        return self.ingest(principal, event)

    def get_event(
        self,
        event_id: str,
        producer_app_id: str | None = None,
        source: str | None = None,
    ) -> StoredEvent | None:
        with self._connect() as connection:
            return self._get_event_with_connection(connection, event_id, producer_app_id, source)

    def _get_event_with_connection(
        self,
        connection: sqlite3.Connection,
        event_id: str,
        producer_app_id: str | None,
        source: str | None,
    ) -> StoredEvent | None:
        sql = "SELECT * FROM events WHERE event_id=?"
        parameters: list[Any] = [event_id]
        if producer_app_id:
            sql += " AND producer_app_id=?"
            parameters.append(producer_app_id)
        if source:
            sql += " AND source=?"
            parameters.append(source)
        sql += " ORDER BY recorded_at DESC LIMIT 1"
        row = connection.execute(sql, parameters).fetchone()
        return self._row_to_event(row) if row else None

    def search(
        self,
        spec: SearchSpec,
        *,
        max_regex_candidates: int = 5000,
        regex_timeout_seconds: float = 0.05,
    ) -> list[StoredEvent]:
        where: list[str] = []
        parameters: list[Any] = []
        if spec.record_families:
            where.append(f"e.record_family IN ({','.join('?' for _ in spec.record_families)})")
            parameters.extend(spec.record_families)
        if spec.apps:
            where.append(f"e.producer_app_id IN ({','.join('?' for _ in spec.apps)})")
            parameters.extend(spec.apps)
        if spec.event_types:
            where.append(f"e.event_type IN ({','.join('?' for _ in spec.event_types)})")
            parameters.extend(spec.event_types)
        if spec.occurred_after:
            where.append("e.occurred_at >= ?")
            parameters.append(_parse_timestamp(spec.occurred_after, "occurred_after"))
        if spec.occurred_before:
            where.append("e.occurred_at <= ?")
            parameters.append(_parse_timestamp(spec.occurred_before, "occurred_before"))
        for term in spec.required_terms:
            where.append("lower(e.canonical_text) LIKE ? ESCAPE '\\'")
            parameters.append(f"%{_escape_like(term.lower())}%")
        for phrase in spec.exact_phrases:
            where.append("lower(e.canonical_text) LIKE ? ESCAPE '\\'")
            parameters.append(f"%{_escape_like(phrase.lower())}%")
        for path, value in spec.field_predicates.items():
            where.append(
                """EXISTS (
                       SELECT 1 FROM event_fields ef
                       WHERE ef.event_rowid=e.rowid AND ef.field_path=? AND ef.value_text=?
                   )"""
            )
            parameters.extend([path, _field_text(value)])
        sql = "SELECT e.* FROM events e"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY e.occurred_at {'ASC' if spec.order == 'asc' else 'DESC'}, e.rowid {'ASC' if spec.order == 'asc' else 'DESC'}"
        candidate_limit = max_regex_candidates if spec.regex_patterns else spec.limit
        sql += " LIMIT ?"
        parameters.append(candidate_limit)
        with self._connect() as connection:
            events = [self._row_to_event(row) for row in connection.execute(sql, parameters).fetchall()]
        if spec.regex_patterns:
            compiled = [_compile_safe_regex(pattern) for pattern in spec.regex_patterns]
            filtered: list[StoredEvent] = []
            try:
                for event in events:
                    if all(_regex_search(pattern, event.canonical_text, regex_timeout_seconds) for pattern in compiled):
                        filtered.append(event)
            except TimeoutError as exc:
                raise ValidationError("Regex search exceeded its execution timeout.") from exc
            events = filtered
        _LOG.debug(
            "search apps=%s types=%s families=%s after=%s before=%s terms=%s phrases=%s fields=%s regex_count=%s candidates=%s results=%s",
            spec.apps,
            spec.event_types,
            spec.record_families,
            spec.occurred_after,
            spec.occurred_before,
            spec.required_terms,
            spec.exact_phrases,
            sorted(spec.field_predicates),
            len(spec.regex_patterns),
            candidate_limit,
            min(len(events), spec.limit),
        )
        return events[: spec.limit]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> StoredEvent:
        return StoredEvent(
            event_id=row["event_id"],
            producer_app_id=row["producer_app_id"],
            source=row["source"],
            event_type=row["event_type"],
            schema_version=row["schema_version"],
            record_family=row["record_family"],
            occurred_at=row["occurred_at"],
            recorded_at=row["recorded_at"],
            subject=row["subject"],
            correlation_id=row["correlation_id"],
            causation_id=row["causation_id"],
            session_id=row["session_id"],
            visibility=row["visibility"],
            data=json.loads(row["data_json"]),
            canonical_text=row["canonical_text"],
        )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _compile_safe_regex(pattern: str) -> Any:
    # Reject common catastrophic nested-quantifier shapes before applying regex to bounded text.
    if re.search(r"\([^)]*[+*][^)]*\)[+*{]", pattern):
        raise ValidationError("Regex contains a nested quantifier.")
    try:
        engine = timeout_regex if timeout_regex is not None else re
        return engine.compile(pattern, engine.IGNORECASE)
    except Exception as exc:
        raise ValidationError(f"Invalid regex {pattern!r}: {exc}") from exc


def _regex_search(pattern: Any, text: str, timeout_seconds: float) -> Any:
    if timeout_regex is not None:
        return pattern.search(text, timeout=timeout_seconds)
    return pattern.search(text)


_MIGRATION_1 = """
CREATE TABLE apps (
    app_id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE tokens (
    token_id TEXT PRIMARY KEY,
    app_id TEXT NOT NULL REFERENCES apps(app_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    scopes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX tokens_app_active_idx ON tokens(app_id, revoked_at);
CREATE TABLE schemas (
    app_id TEXT NOT NULL REFERENCES apps(app_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    version INTEGER NOT NULL,
    record_family TEXT NOT NULL,
    description TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(app_id, event_type, version)
);
CREATE TABLE events (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    producer_app_id TEXT NOT NULL REFERENCES apps(app_id),
    schema_app_id TEXT NOT NULL,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    record_family TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    subject TEXT,
    correlation_id TEXT,
    causation_id TEXT,
    session_id TEXT,
    visibility TEXT NOT NULL,
    data_json TEXT NOT NULL,
    canonical_text TEXT NOT NULL,
    envelope_hash TEXT NOT NULL,
    UNIQUE(producer_app_id, source, event_id),
    FOREIGN KEY(schema_app_id, event_type, schema_version)
        REFERENCES schemas(app_id, event_type, version)
);
CREATE INDEX events_time_idx ON events(occurred_at DESC);
CREATE INDEX events_app_time_idx ON events(producer_app_id, occurred_at DESC);
CREATE INDEX events_type_time_idx ON events(event_type, occurred_at DESC);
CREATE INDEX events_family_time_idx ON events(record_family, occurred_at DESC);
CREATE INDEX events_correlation_idx ON events(correlation_id);
CREATE INDEX events_session_idx ON events(session_id);
CREATE TABLE event_fields (
    event_rowid INTEGER NOT NULL REFERENCES events(rowid) ON DELETE CASCADE,
    field_path TEXT NOT NULL,
    value_text TEXT NOT NULL,
    PRIMARY KEY(event_rowid, field_path)
);
CREATE INDEX event_fields_lookup_idx ON event_fields(field_path, value_text);
"""
