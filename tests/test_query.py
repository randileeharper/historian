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
            "record_types": [
                "music.playback.started",
                "music.playback.stopped",
            ],
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


def test_model_plan_is_executed_faithfully(
    context, resolver, vesper_token, monkeypatch
) -> None:
    monkeypatch.setattr(
        context.service,
        "_local_time",
        lambda: "2026-06-17T12:00:00-07:00",
    )
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
    assert len(result.searches) == 2
    assert result.searches[0]["app"] == "vesper"
    assert result.searches[1]["app"] == "historian"
    assert result.searches[0]["begin"] is None
    assert result.searches[0]["end"] is None


def test_synthesis_evidence_omits_trace_metadata(
    context, resolver, vesper_token
) -> None:
    principal = context.store.authenticate(vesper_token)
    context.service.ingest(principal, event())
    resolver.plans.append(_search_action())
    resolver.answers.append(_answer())
    result = context.service.query(principal, "What did Vesper do?")
    assert result.status == "ok"
    evidence = resolver.calls[-1]["evidence"]
    assert evidence.startswith("Original question: What did Vesper do?")
    assert "vesper | music.playback.started" in evidence
    assert "track=Morning Song; artist=Test Artist" in evidence
    assert '"app":' not in evidence
    assert "correlation_id:" not in evidence
    assert "causation_id:" not in evidence
    assert "source:" not in evidence
    assert "family:" not in evidence


def _plan(*record_types: str, limit: int | None = None, sort: str | None = None) -> dict:
    plan: dict = {
        "searches": [
            {
                "app": "vesper",
                "record_types": [
                    {"record_type": record_type}
                    for record_type in record_types
                ],
            }
        ]
    }
    if limit is not None:
        plan["limit"] = limit
    if sort is not None:
        plan["sort"] = sort
    return plan


def _record_lines(evidence: str) -> list[str]:
    return [line for line in evidence.splitlines() if line.startswith("[")]


def test_latest_limit_is_global_across_record_types(
    context, resolver, vesper_token
) -> None:
    principal = context.store.authenticate(vesper_token)
    context.service.ingest(
        principal,
        event(event_id="old", occurred_at="2026-06-17T08:00:00-07:00"),
    )
    context.service.ingest(
        principal,
        event(
            event_id="new",
            event_type="music.playback.stopped",
            occurred_at="2026-06-17T09:00:00-07:00",
            track="Last Song",
        ),
    )
    resolver.plans.append(
        _plan(
            "music.playback.started",
            "music.playback.stopped",
            limit=1,
            sort="newest",
        )
    )
    resolver.answers.append(_answer())
    result = context.service.query(principal, "What was the latest record?")
    assert result.status == "ok"
    evidence = resolver.calls[-1]["evidence"]
    assert len(_record_lines(evidence)) == 1
    assert "music.playback.stopped" in evidence
    assert "Last Song" in evidence
    assert "newest first" in evidence


def test_default_order_is_oldest_first_and_limits_follow_deduplication(
    context, resolver, vesper_token
) -> None:
    principal = context.store.authenticate(vesper_token)
    for index in range(3):
        context.service.ingest(
            principal,
            event(
                event_id=f"event-{index}",
                occurred_at=f"2026-06-17T0{index + 7}:00:00-07:00",
                track=f"Track {index}",
            ),
        )
    plan = _plan("music.playback.started", limit=2)
    plan["searches"].append(plan["searches"][0].copy())
    resolver.plans.append(plan)
    resolver.answers.append(_answer())
    result = context.service.query(principal, "What played?")
    assert result.status == "ok"
    evidence = resolver.calls[-1]["evidence"]
    lines = _record_lines(evidence)
    assert len(lines) == 2
    assert "Track 0" in lines[0]
    assert "Track 1" in lines[1]
    assert evidence.count("Track 0") == 1
    assert "oldest first" in evidence


def test_more_than_record_call_limit_uses_sequential_chunk_summaries(
    context, resolver, vesper_token
) -> None:
    principal = context.store.authenticate(vesper_token)
    context.settings.max_records_per_model_call = 2
    for index in range(5):
        context.service.ingest(
            principal,
            event(
                event_id=f"chunk-{index}",
                occurred_at=f"2026-06-17T{index + 7:02d}:00:00-07:00",
                track=f"Chunk Track {index}",
            ),
        )
    resolver.plans.append(_plan("music.playback.started"))
    resolver.answers.append(_answer())
    result = context.service.query(principal, "What played?")
    assert result.status == "ok"
    chunk_calls = [call for call in resolver.calls if call["kind"] == "chunk_summary"]
    assert [(call["record_start"], call["record_end"]) for call in chunk_calls] == [
        (1, 2),
        (3, 4),
        (5, 5),
    ]
    assert all(len(_record_lines(call["evidence"])) <= 2 for call in chunk_calls)
    assert resolver.calls[-1]["kind"] == "final_answer"
    assert len(resolver.calls[-1]["summaries"]) == 3


def test_fifty_records_use_one_call_and_fifty_one_use_chunk_summaries(
    context, resolver, vesper_token
) -> None:
    principal = context.store.authenticate(vesper_token)
    for index in range(51):
        context.service.ingest(
            principal,
            event(
                event_id=f"threshold-{index}",
                occurred_at=f"2026-06-17T08:{index:02d}:00-07:00",
                track=f"Threshold Track {index}",
            ),
        )

    resolver.plans.append(_plan("music.playback.started", limit=50))
    resolver.answers.append(_answer())
    first_result = context.service.query(principal, "What were the first fifty?")
    assert first_result.status == "ok"
    first_query_calls = resolver.calls[1:]
    assert [call["kind"] for call in first_query_calls] == ["answer"]
    assert len(_record_lines(first_query_calls[0]["evidence"])) == 50

    call_count = len(resolver.calls)
    resolver.plans.append(_plan("music.playback.started"))
    resolver.answers.append(_answer())
    second_result = context.service.query(principal, "What were all the records?")
    assert second_result.status == "ok"
    second_query_calls = resolver.calls[call_count + 1 :]
    assert [call["kind"] for call in second_query_calls] == [
        "chunk_summary",
        "chunk_summary",
        "final_answer",
    ]
    assert len(_record_lines(second_query_calls[0]["evidence"])) == 50
    assert len(_record_lines(second_query_calls[1]["evidence"])) == 1


def test_character_limit_creates_chunks_without_dropping_records(
    context, resolver, vesper_token
) -> None:
    principal = context.store.authenticate(vesper_token)
    context.settings.max_evidence_characters = 220
    for index in range(3):
        context.service.ingest(
            principal,
            event(
                event_id=f"chars-{index}",
                occurred_at=f"2026-06-17T{index + 7:02d}:00:00-07:00",
                track=f"Character Bound Track {index}",
            ),
        )
    resolver.plans.append(_plan("music.playback.started"))
    resolver.answers.append(_answer())
    result = context.service.query(principal, "What played?")
    assert result.status == "ok"
    chunk_calls = [call for call in resolver.calls if call["kind"] == "chunk_summary"]
    assert len(chunk_calls) > 1
    assert sum(len(_record_lines(call["evidence"])) for call in chunk_calls) == 3


def test_hard_query_cap_forces_partial_result_with_message(
    context, resolver, vesper_token
) -> None:
    principal = context.store.authenticate(vesper_token)
    context.settings.max_query_records = 2
    for index in range(3):
        context.service.ingest(
            principal,
            event(
                event_id=f"cap-{index}",
                occurred_at=f"2026-06-17T{index + 7:02d}:00:00-07:00",
                track=f"Cap Track {index}",
            ),
        )
    resolver.plans.append(_plan("music.playback.started"))
    resolver.answers.append(_answer())
    result = context.service.query(principal, "What played?")
    assert result.status == "partial"
    assert result.message is not None
    assert "max_query_records" in result.message
    answer_call = resolver.calls[-1]
    assert answer_call["hard_cap_reached"] is True
    assert len(_record_lines(answer_call["evidence"])) == 2


def test_transcript_start_failure_does_not_crash_query(
    context, resolver, vesper_token, monkeypatch
) -> None:
    principal = context.store.authenticate(vesper_token)
    context.service.ingest(principal, event())

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(context.transcript, "start", boom)
    resolver.plans.append(_search_action())
    resolver.answers.append(_answer())
    result = context.service.query(principal, "What did Vesper do?")
    assert result.status == "ok"


def test_query_succeeds_when_audit_recording_fails(
    context, resolver, vesper_token, monkeypatch
) -> None:
    principal = context.store.authenticate(vesper_token)
    context.service.ingest(principal, event())

    def boom(*args, **kwargs):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(context.store, "ingest_internal", boom)
    resolver.plans.append(_search_action())
    resolver.answers.append(_answer())
    result = context.service.query(principal, "What did Vesper do?")
    assert result.status == "ok"
