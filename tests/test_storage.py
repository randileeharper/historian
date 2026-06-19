from __future__ import annotations

import sqlite3

import pytest

from historian.errors import AuthenticationError, ConflictError, ValidationError
from historian.models import SearchSpec

from conftest import event


def test_install_authenticate_ingest_redact_and_deduplicate(context, vesper_token) -> None:
    principal = context.store.authenticate(vesper_token)
    stored, duplicate = context.service.ingest(principal, event())
    assert duplicate is False
    assert stored.data["secret"] == "[REDACTED]"
    assert "do-not-store" not in stored.canonical_text

    retry, duplicate = context.service.ingest(principal, event())
    assert duplicate is True
    assert retry.event_id == stored.event_id


def test_conflicting_event_id_is_rejected(context, vesper_token) -> None:
    principal = context.store.authenticate(vesper_token)
    context.service.ingest(principal, event())
    with pytest.raises(ConflictError):
        context.service.ingest(principal, event(track="Different Song"))


def test_batch_is_atomic(context, vesper_token) -> None:
    principal = context.store.authenticate(vesper_token)
    first = event("batch-first")
    invalid = event("batch-invalid")
    invalid["data"].pop("artist")
    with pytest.raises(ValidationError):
        context.service.ingest_batch(principal, [first, invalid])
    assert context.store.get_event("batch-first") is None


def test_identity_owns_source_and_schema_is_required(context, vesper_token) -> None:
    principal = context.store.authenticate(vesper_token)
    wrong_source = event()
    wrong_source["source"] = "app://magpie"
    with pytest.raises(ValidationError, match="source"):
        context.service.ingest(principal, wrong_source)
    unknown = event()
    unknown["type"] = "music.unknown"
    with pytest.raises(ValidationError, match="not registered"):
        context.service.ingest(principal, unknown)


def test_literal_field_time_phrase_and_regex_search(context, vesper_token) -> None:
    principal = context.store.authenticate(vesper_token)
    context.service.ingest(principal, event("one", track="Morning Song"))
    context.service.ingest(
        principal,
        event("two", occurred_at="2026-06-17T12:00:00-07:00", track="Lunch Song"),
    )
    matches = context.service.raw_search(
        principal,
        SearchSpec(
            apps=["vesper"],
            occurred_before="2026-06-17T10:00:00-07:00",
            exact_phrases=["Morning Song"],
            field_predicates={"artist": "Test Artist"},
            regex_patterns=["track: Morning\\s+Song"],
        ),
    )
    assert [item.event_id for item in matches] == ["one"]


def test_regex_requires_a_non_regex_bound(context, vesper_token) -> None:
    principal = context.store.authenticate(vesper_token)
    with pytest.raises(ValidationError, match="bounding"):
        context.service.raw_search(principal, SearchSpec(regex_patterns=["song"]))


def test_token_rotation_revokes_previous_default(context, vesper_manifest) -> None:
    first = context.store.install_app(vesper_manifest)
    second = context.store.install_app(vesper_manifest)
    with pytest.raises(AuthenticationError):
        context.store.authenticate(first)
    assert context.store.authenticate(second).app_id == "vesper"


def test_schema_versions_are_immutable(context, vesper_manifest) -> None:
    context.store.install_app(vesper_manifest)
    vesper_manifest.schemas[0].description = "Changed meaning."
    with pytest.raises(ConflictError, match="immutable"):
        context.store.install_app(vesper_manifest)


def test_schema_replacement_preserves_credentials(context, vesper_manifest) -> None:
    token = context.store.install_app(vesper_manifest)
    vesper_manifest.schemas[0].description = "Changed meaning during development."

    context.store.replace_app_schemas(vesper_manifest)

    assert context.store.authenticate(token).app_id == "vesper"
    schema_app_id, schema = context.store.get_schema("vesper", "music.playback.started", 1)
    assert schema_app_id == "vesper"
    assert schema.description == "Changed meaning during development."


def test_schema_replacement_requires_installed_app(context, vesper_manifest) -> None:
    with pytest.raises(ValidationError, match="not installed"):
        context.store.replace_app_schemas(vesper_manifest)


def test_builtin_transcript_provenance_is_distinct(context, vesper_token) -> None:
    principal = context.store.authenticate(vesper_token)
    user_message = {
        "specversion": "1.0",
        "id": "user-message",
        "source": "app://vesper/channel",
        "type": "core.transcript.user_message",
        "time": "2026-06-17T08:00:00Z",
        "schemaversion": 1,
        "data": {
            "content": "literal user words",
            "author_id": "randi",
            "channel": "test",
            "conversation_id": "c1",
        },
    }
    internal = {
        "specversion": "1.0",
        "id": "internal-message",
        "source": "app://vesper/runtime",
        "type": "core.runtime.internal_event",
        "time": "2026-06-17T08:01:00Z",
        "schemaversion": 1,
        "data": {
            "kind": "action_requested",
            "source_component": "sidecar",
            "content": "user-shaped only at render time",
        },
    }
    stored_user, _ = context.service.ingest(principal, user_message)
    stored_internal, _ = context.service.ingest(principal, internal)
    assert stored_user.record_family == "transcript"
    assert stored_internal.record_family == "internal"
    assert stored_user.event_type != stored_internal.event_type


def test_schema_migration_does_not_delete_existing_database(context) -> None:
    database = context.settings.expanded_database_path
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES ('alive')")
    context.store.initialize()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == "alive"
