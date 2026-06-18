"""Domain models shared by storage, services, and transports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


RecordFamily = Literal[
    "event",
    "transcript",
    "summary",
    "user_fact",
    "app_preference",
    "error",
    "status",
    "internal",
]


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def to_jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value


@dataclass(slots=True, frozen=True)
class AuthPrincipal:
    app_id: str
    token_id: str
    scopes: frozenset[str]


@dataclass(slots=True)
class SchemaDefinition:
    event_type: str
    version: int
    record_family: RecordFamily
    description: str
    json_schema: dict[str, Any]
    searchable_fields: list[str] = field(default_factory=list)
    redacted_fields: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AppManifest:
    app_id: str
    description: str
    default_scopes: list[str]
    schemas: list[SchemaDefinition]


@dataclass(slots=True)
class EventEnvelope:
    specversion: str
    event_id: str
    source: str
    event_type: str
    occurred_at: str
    schema_version: int
    data: dict[str, Any]
    subject: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    session_id: str | None = None
    visibility: str = "private"


@dataclass(slots=True)
class StoredEvent:
    event_id: str
    producer_app_id: str
    source: str
    event_type: str
    schema_version: int
    record_family: str
    occurred_at: str
    recorded_at: str
    subject: str | None
    correlation_id: str | None
    causation_id: str | None
    session_id: str | None
    visibility: str
    data: dict[str, Any]
    canonical_text: str


@dataclass(slots=True)
class SearchSpec:
    record_families: list[str] = field(default_factory=list)
    apps: list[str] = field(default_factory=list)
    event_types: list[str] = field(default_factory=list)
    occurred_after: str | None = None
    occurred_before: str | None = None
    required_terms: list[str] = field(default_factory=list)
    exact_phrases: list[str] = field(default_factory=list)
    field_predicates: dict[str, Any] = field(default_factory=dict)
    regex_patterns: list[str] = field(default_factory=list)
    order: Literal["asc", "desc"] = "desc"
    limit: int = 50


@dataclass(slots=True)
class QueryResult:
    status: Literal["ok", "partial", "insufficient_evidence", "error"]
    answer: str
    query_id: str
    searches: list[dict[str, Any]]
    message: str | None = None
