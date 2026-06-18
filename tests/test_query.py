from __future__ import annotations

from conftest import event


def _search_action() -> dict:
    return {
        "searches": [
            {
                "app": "vesper",
                "begin": "2026-06-17T00:00:00-07:00",
                "end": "2026-06-17T12:00:00-07:00",
                "record_types": [
                    {
                        "record_type": "music.playback.started",
                        "search": "Morning Song",
                    }
                ],
            }
        ]
    }


def _answer() -> dict:
    return {
        "status": "ok",
        "answer": "Vesper started Morning Song.",
    }


def test_iterative_query_searches_and_cites(context, resolver, vesper_token) -> None:
    principal = context.store.authenticate(vesper_token)
    context.service.ingest(principal, event())
    resolver.plans.append(_search_action())
    resolver.answers.append(_answer())
    result = context.service.query(principal, "What did Vesper do this morning?")
    assert result.status == "ok"
    assert len(resolver.calls) == 2
    query_event = context.store.get_event(result.query_id)
    assert query_event is not None
    assert query_event.event_type == "historian.query.completed"
    assert query_event.data["question"] == "What did Vesper do this morning?"


def test_no_evidence_is_explicit(context, resolver, vesper_token) -> None:
    principal = context.store.authenticate(vesper_token)
    resolver.plans.append(
        {
            "searches": [
                {
                    "app": "vesper",
                    "record_types": [
                        {"record_type": "music.playback.started"}
                    ],
                }
            ]
        }
    )
    result = context.service.query(principal, "Is Vesper running?")
    assert result.status == "insufficient_evidence"
    assert len(resolver.calls) == 1


def test_search_timestamps_and_text_are_optional(context, resolver, vesper_token) -> None:
    principal = context.store.authenticate(vesper_token)
    context.service.ingest(principal, event())
    resolver.plans.append(
        {
            "searches": [
                {
                    "app": "vesper",
                    "record_types": [
                        {"record_type": "music.playback.started"}
                    ],
                }
            ]
        }
    )
    resolver.answers.append(_answer())
    result = context.service.query(principal, "What has Vesper played?")
    assert result.status == "ok"
    assert result.searches[0]["begin"] is None
    assert result.searches[0]["end"] is None
    assert result.searches[0]["search"] is None
    assert resolver.calls[0]["catalog"] == [
        {
            "app": "historian",
            "description": "Historian internal records and shared core continuity schemas.",
            "record_types": [
                "core.application.preference",
                "core.conversation.summary",
                "core.operation.error",
                "core.operation.status",
                "core.runtime.internal_event",
                "core.transcript.assistant_message",
                "core.transcript.user_message",
                "core.user.fact",
                "historian.query.completed",
            ],
        },
        {
            "app": "vesper",
            "description": "Music agent.",
            "record_types": ["music.playback.started"],
        },
    ]


def test_malformed_optional_timestamp_does_not_abort_search(
    context, resolver, vesper_token
) -> None:
    principal = context.store.authenticate(vesper_token)
    context.service.ingest(principal, event())
    resolver.plans.append(
        {
            "searches": [
                {
                    "app": "vesper",
                    "begin": "not-a-timestamp",
                    "end": "also-not-a-timestamp",
                    "record_types": [
                        {"record_type": "music.playback.started"}
                    ],
                }
            ]
        }
    )
    resolver.answers.append(_answer())
    result = context.service.query(principal, "What has Vesper played?")
    assert result.status == "ok"
    assert result.searches[0]["begin"] is None
    assert result.searches[0]["end"] is None


def test_named_app_and_today_are_enforced_when_model_omits_them(
    context, resolver, vesper_token
) -> None:
    principal = context.store.authenticate(vesper_token)
    context.service.ingest(principal, event())
    resolver.plans.append(
        {
            "searches": [
                {
                    "app": "vesper",
                    "record_types": [
                        {"record_type": "music.playback.started"}
                    ],
                },
                {
                    "app": "historian",
                    "record_types": [
                        {"record_type": "historian.query.completed"}
                    ],
                },
            ]
        }
    )
    resolver.answers.append(_answer())
    result = context.service.query(principal, "What did Vesper do today?")
    assert result.status == "ok"
    assert len(result.searches) == 1
    assert result.searches[0]["app"] == "vesper"
    assert result.searches[0]["begin"] is not None
    assert result.searches[0]["begin"].endswith("T00:00:00-07:00")
    assert result.searches[0]["end"] is not None


def test_synthesis_evidence_omits_trace_metadata(
    context, resolver, vesper_token
) -> None:
    principal = context.store.authenticate(vesper_token)
    context.service.ingest(principal, event())
    resolver.plans.append(_search_action())
    resolver.answers.append(_answer())
    result = context.service.query(principal, "What did Vesper do?")
    assert result.status == "ok"
    evidence = resolver.calls[-1]["evidence"][0]
    assert set(evidence) == {"app", "type", "occurred_at", "details"}
    assert "correlation_id:" not in evidence["details"]
    assert "causation_id:" not in evidence["details"]
    assert "source:" not in evidence["details"]
    assert "family:" not in evidence["details"]
