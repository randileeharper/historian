from __future__ import annotations

import json

import httpx

from historian.config import Settings
from historian.debug import QueryTranscript
from historian.resolver import OpenAICompatibleQueryResolver


def _resolver(tmp_path, handler, *, reasoning=False):
    settings = Settings(
        resolver_model="test-model",
        resolver_include_reasoning=reasoning,
        debug_enabled=True,
        resolver_debug_log_path=str(tmp_path / "resolver.log"),
        debug_log_path=str(tmp_path / "debug.log"),
    )
    transcript = QueryTranscript(settings)
    transcript.start(query_id="query-1", caller_app_id="test", question="What happened?")
    return OpenAICompatibleQueryResolver(
        settings, transcript, transport=httpx.MockTransport(handler)
    )


def test_planner_uses_compact_catalog_and_optional_filters(tmp_path) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "searches": [
                                        {
                                            "app": "vesper",
                                            "record_types": [
                                                {
                                                    "record_type": "music.playback.started"
                                                }
                                            ],
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    resolver = _resolver(tmp_path, handler)
    plan = resolver.plan_searches(
        question="What happened?",
        current_time="2026-06-17T12:00:00-07:00",
        catalog=[
            {
                "app": "vesper",
                "description": "Music agent.",
                "record_types": ["music.playback.started"],
            }
        ],
        query_id="query-1",
        step=1,
    )
    assert plan["searches"][0] == {
        "app": "vesper",
        "record_types": [{"record_type": "music.playback.started"}],
    }
    item_schema = captured["response_format"]["json_schema"]["schema"]["properties"][
        "searches"
    ]["items"]
    assert item_schema["required"] == ["app", "record_types"]
    assert captured["response_format"]["json_schema"]["strict"] is True
    assert captured["think"] is False
    assert captured["reasoning_effort"] == "none"
    user = json.loads(captured["messages"][1]["content"])
    assert set(user) == {"question", "current_system_time", "applications"}
    assert "version" not in captured["messages"][1]["content"]
    assert "record_family" not in captured["messages"][1]["content"]
    system = captured["messages"][0]["content"]
    assert "Format examples generated from the current catalog" in system
    assert "five recent records" in system
    assert "the latest record" in system
    assert "unlimited chronological records" in system
    schema_properties = captured["response_format"]["json_schema"]["schema"]["properties"]
    assert schema_properties["limit"] == {"type": "integer", "minimum": 1}
    assert schema_properties["sort"]["enum"] == ["oldest", "newest"]
    assert '"limit": 1, "sort": "newest"' in system
    assert '"limit": 5, "sort": "newest"' in system


def test_planner_examples_do_not_hardcode_apps_or_record_types(tmp_path) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"searches": []}'}}
                ]
            },
        )

    resolver = _resolver(tmp_path, handler)
    resolver.plan_searches(
        question="What did Magpie do today?",
        current_time="2026-06-17T12:00:00-07:00",
        catalog=[
            {
                "app": "magpie",
                "description": "Research agent.",
                "record_types": ["research.started", "research.completed"],
            }
        ],
        query_id="query-1",
        step=1,
    )
    system = captured["messages"][0]["content"]
    assert '"app": "magpie"' in system
    assert '"record_type": "research.started"' in system
    assert "vesper" not in system.lower()
    assert "music." not in system


def test_planner_examples_follow_catalog_order(tmp_path) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"searches": []}'}}]},
        )

    resolver = _resolver(tmp_path, handler)
    resolver.plan_searches(
        question="What happened today?",
        current_time="2026-06-17T12:00:00-07:00",
        catalog=[
            {
                "app": "historian",
                "description": "History service.",
                "record_types": ["core.user.fact"],
            },
            {
                "app": "vesper",
                "description": "Music agent.",
                "record_types": ["music.playback.started"],
            },
            {
                "app": "magpie",
                "description": "Research agent.",
                "record_types": ["research.completed"],
            },
        ],
        query_id="query-1",
        step=1,
    )
    system = captured["messages"][0]["content"]
    examples = system.split("Format examples generated from the current catalog.", 1)[1]
    assert examples.index('"app": "historian"') < examples.index('"app": "vesper"')
    assert examples.index('"app": "vesper"') < examples.index('"app": "magpie"')


def test_synthesizer_cannot_request_more_searches(tmp_path) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "status": "ok",
                                    "answer": "Vesper played a song.",
                                }
                            )
                        }
                    }
                ]
            },
        )

    resolver = _resolver(tmp_path, handler)
    answer = resolver.synthesize_answer(
        question="What happened?",
        current_time="2026-06-17T12:00:00-07:00",
        evidence=(
            "Original question: What happened?\n"
            "Records 1-1 of 1, oldest first.\n\n"
            "[2026-06-17 08:00:00 PDT] vesper | music.playback.started | track: Morning Song"
        ),
        hard_cap_reached=False,
        query_id="query-1",
        step=2,
    )
    assert answer["status"] == "ok"
    properties = captured["response_format"]["json_schema"]["schema"]["properties"]
    assert set(properties) == {"status", "answer"}
    system = captured["messages"][0]["content"]
    assert "at most 120 words" in system
    assert "record_id" not in system
    log = (tmp_path / "resolver.log").read_text(encoding="utf-8")
    assert "SYSTEM PROMPT" in log
    assert "USER MESSAGE" in log
    assert "RESPONSE" in log


def test_resolver_ignores_trailing_local_model_control_token(tmp_path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"searches": []}\n<|tool_response>'
                        }
                    }
                ]
            },
        )

    resolver = _resolver(tmp_path, handler)
    assert resolver.plan_searches(
        question="What happened?",
        current_time="2026-06-17T12:00:00-07:00",
        catalog=[],
        query_id="query-1",
        step=1,
    ) == {"searches": []}


def test_planner_retries_invented_record_type_with_correction(tmp_path) -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        record_type = (
            "music.track_selected"
            if len(requests) == 1
            else "music.session.track_selected"
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "searches": [
                                        {
                                            "app": "vesper",
                                            "record_types": [
                                                {"record_type": record_type}
                                            ],
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    resolver = _resolver(tmp_path, handler)
    plan = resolver.plan_searches(
        question="What music did I listen to?",
        current_time="2026-06-17T12:00:00-07:00",
        catalog=[
            {
                "app": "vesper",
                "description": "Music agent.",
                "record_types": ["music.session.track_selected"],
            }
        ],
        query_id="query-1",
        step=1,
    )
    assert plan["searches"][0]["record_types"][0]["record_type"] == (
        "music.session.track_selected"
    )
    correction = json.loads(requests[1]["messages"][1]["content"])[
        "retry_correction"
    ]
    assert "music.track_selected" in correction["previous_error"]
    assert "music.session.track_selected" in correction["previous_error"]
    log = (tmp_path / "resolver.log").read_text(encoding="utf-8")
    assert "=== MODEL CALL 1 ===" in log
    assert "=== MODEL CALL 2 ===" in log


def test_planner_retries_invalid_limit_and_sort(tmp_path) -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        content = (
            '{"limit":"one","sort":"latest","searches":[]}'
            if len(requests) == 1
            else '{"limit":1,"sort":"newest","searches":[]}'
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    resolver = _resolver(tmp_path, handler)
    plan = resolver.plan_searches(
        question="What was latest?",
        current_time="2026-06-17T12:00:00-07:00",
        catalog=[],
        query_id="query-1",
        step=1,
    )
    assert plan == {"limit": 1, "sort": "newest", "searches": []}
    correction = json.loads(requests[1]["messages"][1]["content"])[
        "retry_correction"
    ]
    assert "not of type 'integer'" in correction["previous_error"]
    assert "is not one of" in correction["previous_error"]


def test_synthesizer_retries_schema_violation(tmp_path) -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        content = (
            '{"status":"ok"}'
            if len(requests) == 1
            else '{"status":"ok","answer":"Vesper played music."}'
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    resolver = _resolver(tmp_path, handler)
    answer = resolver.synthesize_answer(
        question="What happened?",
        current_time="2026-06-17T12:00:00-07:00",
        evidence="No records.",
        hard_cap_reached=False,
        query_id="query-1",
        step=2,
    )
    assert answer["answer"] == "Vesper played music."
    correction = json.loads(requests[1]["messages"][1]["content"])[
        "retry_correction"
    ]
    assert "answer" in correction["previous_error"]


def test_chunk_summary_and_final_synthesis_retry_schema_violations(tmp_path) -> None:
    requests: list[dict] = []
    responses = iter(
        [
            '{"summary":""}',
            '{"summary":"Vesper played two tracks."}',
            '{"status":"ok"}',
            '{"status":"ok","answer":"Vesper played two tracks."}',
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": next(responses)}}]},
        )

    resolver = _resolver(tmp_path, handler)
    summary = resolver.summarize_evidence_chunk(
        question="What played?",
        current_time="2026-06-17T12:00:00-07:00",
        evidence="Records 1-2 of 4.",
        chunk_index=1,
        total_chunks=2,
        record_start=1,
        record_end=2,
        total_records=4,
        query_id="query-1",
        step=2,
    )
    answer = resolver.synthesize_summaries(
        question="What played?",
        current_time="2026-06-17T12:00:00-07:00",
        summaries=[summary["summary"]],
        hard_cap_reached=False,
        query_id="query-1",
        step=3,
    )
    assert answer["answer"] == "Vesper played two tracks."
    assert len(requests) == 4


def test_resolver_stops_after_three_retries(tmp_path) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="model exploded")

    resolver = _resolver(tmp_path, handler)
    try:
        resolver.plan_searches(
            question="Break?",
            current_time="2026-06-17T12:00:00-07:00",
            catalog=[],
            query_id="query-1",
            step=1,
        )
    except Exception:
        pass
    assert calls == 4


def test_resolver_transcript_captures_http_error_response(tmp_path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="model exploded")

    resolver = _resolver(tmp_path, handler)
    try:
        resolver.plan_searches(
            question="Break?",
            current_time="2026-06-17T12:00:00-07:00",
            catalog=[],
            query_id="query-1",
            step=1,
        )
    except Exception:
        pass
    log = (tmp_path / "resolver.log").read_text(encoding="utf-8")
    assert "http_status: 500" in log
    assert "model exploded" in log
    assert "ERROR" in log
