"""Local-model search planning and evidence synthesis."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from difflib import get_close_matches
from time import perf_counter
from typing import Any, Callable, Protocol

import httpx
from jsonschema import Draft202012Validator

from .config import Settings
from .debug import QueryTranscript, get_logger
from .errors import ResolverError


_LOG = get_logger("resolver")


class QueryResolver(Protocol):
    def plan_searches(
        self,
        *,
        question: str,
        current_time: str,
        catalog: list[dict[str, Any]],
        query_id: str,
        step: int,
    ) -> dict[str, Any]: ...

    def synthesize_answer(
        self,
        *,
        question: str,
        current_time: str,
        evidence: str,
        hard_cap_reached: bool = False,
        query_id: str,
        step: int,
    ) -> dict[str, Any]: ...

    def summarize_evidence_chunk(
        self,
        *,
        question: str,
        current_time: str,
        evidence: str,
        chunk_index: int,
        total_chunks: int,
        record_start: int,
        record_end: int,
        total_records: int,
        query_id: str,
        step: int,
    ) -> dict[str, Any]: ...

    def synthesize_summaries(
        self,
        *,
        question: str,
        current_time: str,
        summaries: list[str],
        hard_cap_reached: bool = False,
        query_id: str,
        step: int,
    ) -> dict[str, Any]: ...


def reasoning_options(enabled: bool) -> dict[str, Any]:
    effort = "medium" if enabled else "none"
    return {"think": enabled, "reasoning_effort": effort, "reasoning": {"effort": effort}}


@dataclass(slots=True)
class OpenAICompatibleQueryResolver:
    settings: Settings
    transcript: QueryTranscript
    transport: httpx.BaseTransport | None = None
    _call_counts: dict[str, int] = field(default_factory=dict)
    _call_lock: threading.Lock = field(default_factory=threading.Lock)

    def plan_searches(
        self,
        *,
        question: str,
        current_time: str,
        catalog: list[dict[str, Any]],
        query_id: str,
        step: int,
    ) -> dict[str, Any]:
        local_date = current_time[:10]
        available = list(catalog)
        if not available:
            available = [
                {"app": "example_app", "record_types": ["example.event"]},
                {"app": "another_app", "record_types": ["another.event"]},
            ]
        example_apps = [
            available[index % len(available)]
            for index in range(3)
        ]

        def example_types(app: dict[str, Any]) -> list[str]:
            return app["record_types"][:2] or ["example.event"]

        broad_app, literal_app, unbounded_app = example_apps
        broad_types = example_types(broad_app)
        literal_types = example_types(literal_app)
        unbounded_types = example_types(unbounded_app)
        recent_example = {
            "limit": 5,
            "sort": "newest",
            "searches": [
                {
                    "app": broad_app["app"],
                    "begin": f"{local_date}T00:00:00{current_time[-6:]}",
                    "end": current_time,
                    "record_types": [
                        {"record_type": record_type}
                        for record_type in broad_types
                    ],
                }
            ]
        }
        latest_example = {
            "limit": 1,
            "sort": "newest",
            "searches": [
                {
                    "app": literal_app["app"],
                    "record_types": [
                        {
                            "record_type": literal_types[0],
                            "search": "exact words",
                        }
                    ],
                }
            ]
        }
        unbounded_example = {
            "searches": [
                {
                    "app": unbounded_app["app"],
                    "record_types": [
                        {"record_type": unbounded_types[-1]}
                    ],
                }
            ]
        }
        system = (
            "Plan literal searches of Historian records. Return exactly one JSON object containing searches. "
            "The optional top-level limit applies globally after all searches are merged and deduplicated. "
            "The optional top-level sort is oldest or newest and defaults to oldest. "
            "Each search selects one listed application and one or more listed record types. "
            "begin and end are optional on each application search. search is optional on each record type. "
            "Use begin/end only when the question implies a time range; "
            f"the current system time is {current_time}. Timestamps must use that system time's UTC offset, "
            "not Z. For 'today', begin is local midnight today and end is the current system time. "
            "For broad questions such as what an app did, select all relevant activity record types and omit "
            "search rather than guessing words that records might contain. search is a literal case-insensitive substring that "
            "must occur in that record type, not a semantic query. Omit search for broad activity questions. "
            "Never invent applications or record types. Do not answer the question and do not include reasoning.\n\n"
            "Format examples generated from the current catalog. The short record-type lists demonstrate shape "
            "only; choose every record type relevant to the real question.\n"
            f'Example: five recent records\n{json.dumps(recent_example, ensure_ascii=True)}\n'
            f'Example: the latest record\n{json.dumps(latest_example, ensure_ascii=True)}\n'
            f'Example: unlimited chronological records\n{json.dumps(unbounded_example, ensure_ascii=True)}'
        )
        user = {
            "question": question,
            "current_system_time": current_time,
            "applications": catalog,
        }
        schema = {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1},
                "sort": {"type": "string", "enum": ["oldest", "newest"]},
                "searches": {
                    "type": "array",
                    "maxItems": 50,
                    "items": {
                        "type": "object",
                        "properties": {
                            "app": {"type": "string"},
                            "begin": {"type": "string"},
                            "end": {"type": "string"},
                            "record_types": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 50,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "record_type": {"type": "string"},
                                        "search": {"type": "string"},
                                    },
                                    "required": ["record_type"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["app", "record_types"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["searches"],
            "additionalProperties": False,
        }
        return self._ask_json(
            system,
            user,
            schema_name="historian_search_plan",
            schema=schema,
            query_id=query_id,
            step=step,
            semantic_validator=lambda result: self._validate_plan(result, catalog),
        )

    def synthesize_answer(
        self,
        *,
        question: str,
        current_time: str,
        evidence: str,
        hard_cap_reached: bool = False,
        query_id: str,
        step: int,
    ) -> dict[str, Any]:
        return self._synthesize(
            question=question,
            current_time=current_time,
            evidence=evidence,
            summaries=None,
            hard_cap_reached=hard_cap_reached,
            query_id=query_id,
            step=step,
        )

    def summarize_evidence_chunk(
        self,
        *,
        question: str,
        current_time: str,
        evidence: str,
        chunk_index: int,
        total_chunks: int,
        record_start: int,
        record_end: int,
        total_records: int,
        query_id: str,
        step: int,
    ) -> dict[str, Any]:
        system = (
            "Summarize this one evidence chunk for a later final synthesis. Return exactly one JSON object. "
            "Focus only on facts relevant to the original question. Preserve relevant names, timestamps, "
            "outcomes, and within-chunk counts. This is partial evidence: do not claim to answer from the "
            "complete record set, do not request another search, and do not guess."
        )
        user = {
            "original_question": question,
            "current_system_time": current_time,
            "chunk": f"{chunk_index} of {total_chunks}",
            "record_range": f"{record_start}-{record_end} of {total_records}",
            "notice": "This chunk contains only part of the evidence.",
            "records": evidence,
        }
        schema = {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        }
        return self._ask_json(
            system,
            user,
            schema_name="historian_chunk_summary",
            schema=schema,
            query_id=query_id,
            step=step,
            semantic_validator=self._validate_summary,
        )

    def synthesize_summaries(
        self,
        *,
        question: str,
        current_time: str,
        summaries: list[str],
        hard_cap_reached: bool,
        query_id: str,
        step: int,
    ) -> dict[str, Any]:
        return self._synthesize(
            question=question,
            current_time=current_time,
            evidence=None,
            summaries=summaries,
            hard_cap_reached=hard_cap_reached,
            query_id=query_id,
            step=step,
        )

    def _synthesize(
        self,
        *,
        question: str,
        current_time: str,
        evidence: str | None,
        summaries: list[str] | None,
        hard_cap_reached: bool,
        query_id: str,
        step: int,
    ) -> dict[str, Any]:
        system = (
            "Answer the question using only the supplied Historian records or chunk summaries. Return exactly one JSON object. "
            "Use status=ok when the records answer the question, partial when they support only part of it, "
            "or insufficient_evidence when they do not establish an "
            "answer. For broad activity questions, write a concise high-level summary of at most 120 words. "
            "Combine repeated events into one trend or outcome instead of narrating every record. Focus on major "
            "requests, actions, outcomes, preferences, and errors; omit routine worker/status noise unless relevant. "
            "Never request another search, never guess, and do not include hidden reasoning.\n\n"
            "Example records: a session started for sleepy lofi; the first track was selected; "
            "another track was selected automatically; playback stopped and the session ended.\n"
            'Example output: {"status":"ok","answer":"Vesper ran a sleepy-lofi session, '
            'played multiple tracks automatically, and then stopped playback."}'
        )
        user: dict[str, Any] = {
            "original_question": question,
            "current_system_time": current_time,
            "hard_query_cap_reached": hard_cap_reached,
        }
        if evidence is not None:
            user["records"] = evidence
        else:
            user["chunk_summaries"] = [
                f"Chunk {index}: {summary}"
                for index, summary in enumerate(summaries or [], start=1)
            ]
            user["notice"] = "Each summary covers a sequential portion of the selected records."
        schema = {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["ok", "partial", "insufficient_evidence"],
                },
                "answer": {"type": "string"},
            },
            "required": ["status", "answer"],
            "additionalProperties": False,
        }
        return self._ask_json(
            system,
            user,
            schema_name="historian_answer",
            schema=schema,
            query_id=query_id,
            step=step,
            semantic_validator=self._validate_answer,
        )

    def _ask_json(
        self,
        system: str,
        user: dict[str, Any],
        *,
        schema_name: str,
        schema: dict[str, Any],
        query_id: str,
        step: int,
        semantic_validator: Callable[[dict[str, Any]], list[str]] | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        retry_user = dict(user)
        for attempt in range(self.settings.resolver_max_retries + 1):
            if attempt:
                retry_user = {
                    **user,
                    "retry_correction": {
                        "attempt": attempt + 1,
                        "previous_error": str(last_error),
                        "instruction": "Return a corrected object only. Follow the schema and use only listed values.",
                    },
                }
            try:
                return self._ask_json_once(
                    system,
                    retry_user,
                    schema_name=schema_name,
                    schema=schema,
                    query_id=query_id,
                    semantic_validator=semantic_validator,
                )
            except ResolverError as exc:
                last_error = exc
                if attempt >= self.settings.resolver_max_retries:
                    raise
                _LOG.warning(
                    "query_id=%s resolver_retry attempt=%s max_retries=%s error=%s",
                    query_id,
                    attempt + 1,
                    self.settings.resolver_max_retries,
                    exc,
                )
        raise ResolverError(f"Historian resolver failed: {last_error}")

    def _ask_json_once(
        self,
        system: str,
        user: dict[str, Any],
        *,
        schema_name: str,
        schema: dict[str, Any],
        query_id: str,
        semantic_validator: Callable[[dict[str, Any]], list[str]] | None,
    ) -> dict[str, Any]:
        call_number = self._next_call_number(query_id)
        payload = {
            "model": self.settings.resolver_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=True)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
            **reasoning_options(self.settings.resolver_include_reasoning),
        }
        headers = {"Content-Type": "application/json"}
        if self.settings.resolver_api_key:
            headers["Authorization"] = f"Bearer {self.settings.resolver_api_key}"
        started = perf_counter()
        endpoint = self.settings.resolver_base_url.rstrip("/") + "/chat/completions"
        user_message = json.dumps(user, ensure_ascii=True)
        http_status: int | None = None
        response_content: str | None = None
        reasoning_content: str | None = None
        try:
            with httpx.Client(
                timeout=self.settings.request_timeout_seconds,
                verify=self.settings.verify_tls,
                transport=self.transport,
            ) as client:
                response = client.post(endpoint, json=payload, headers=headers)
            http_status = response.status_code
            response_content = response.text
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            response_content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=True)
            reasoning_content = self._extract_reasoning(body)
            result = self._decode_object(content) if isinstance(content, str) else content
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            elapsed_ms = round((perf_counter() - started) * 1000, 2)
            self.transcript.append_call(
                query_id=query_id,
                step=call_number,
                model=self.settings.resolver_model,
                endpoint=endpoint,
                system_prompt=system,
                user_message=user_message,
                elapsed_ms=elapsed_ms,
                http_status=http_status,
                response_content=response_content,
                reasoning_content=reasoning_content,
                error=f"{type(exc).__name__}: {exc}",
            )
            _LOG.exception(
                "query_id=%s call=%s model_call_failed elapsed_ms=%s http_status=%s",
                query_id,
                call_number,
                elapsed_ms,
                http_status,
            )
            raise ResolverError(f"{type(exc).__name__}: {exc}") from exc
        if not isinstance(result, dict):
            validation_errors = ["response must be a JSON object"]
        else:
            validation_errors = [
                error.message
                for error in Draft202012Validator(schema).iter_errors(result)
            ]
        if semantic_validator and isinstance(result, dict):
            validation_errors.extend(semantic_validator(result))
        if validation_errors:
            elapsed_ms = round((perf_counter() - started) * 1000, 2)
            error = "Invalid model output: " + "; ".join(validation_errors[:8])
            self.transcript.append_call(
                query_id=query_id,
                step=call_number,
                model=self.settings.resolver_model,
                endpoint=endpoint,
                system_prompt=system,
                user_message=user_message,
                elapsed_ms=elapsed_ms,
                http_status=http_status,
                response_content=response_content,
                reasoning_content=reasoning_content,
                error=error,
            )
            raise ResolverError(error)
        elapsed_ms = round((perf_counter() - started) * 1000, 2)
        self.transcript.append_call(
            query_id=query_id,
            step=call_number,
            model=self.settings.resolver_model,
            endpoint=endpoint,
            system_prompt=system,
            user_message=user_message,
            elapsed_ms=elapsed_ms,
            http_status=http_status,
            response_content=response_content,
            reasoning_content=reasoning_content,
            error=None,
        )
        _LOG.debug(
            "query_id=%s call=%s model_call_complete elapsed_ms=%s http_status=%s response_chars=%s",
            query_id,
            call_number,
            elapsed_ms,
            http_status,
            len(response_content or ""),
        )
        return result

    def _next_call_number(self, query_id: str) -> int:
        with self._call_lock:
            if len(self._call_counts) >= 1000 and query_id not in self._call_counts:
                self._call_counts.clear()
            number = self._call_counts.get(query_id, 0) + 1
            self._call_counts[query_id] = number
            return number

    @staticmethod
    def _validate_plan(
        result: dict[str, Any], catalog: list[dict[str, Any]]
    ) -> list[str]:
        allowed = {
            app["app"]: set(app["record_types"])
            for app in catalog
        }
        errors: list[str] = []
        for search_index, search in enumerate(result.get("searches", [])):
            if not isinstance(search, dict):
                continue
            app = search.get("app")
            if app not in allowed:
                errors.append(
                    f"searches[{search_index}].app {app!r} is not registered; "
                    f"choose from {sorted(allowed)}"
                )
                continue
            parsed_timestamps: dict[str, datetime] = {}
            for timestamp_name in ("begin", "end"):
                timestamp = search.get(timestamp_name)
                if timestamp is None:
                    continue
                try:
                    parsed_timestamps[timestamp_name] = datetime.fromisoformat(
                        str(timestamp).replace("Z", "+00:00")
                    )
                except ValueError:
                    errors.append(
                        f"searches[{search_index}].{timestamp_name} is not a valid ISO-8601 timestamp"
                    )
            if (
                "begin" in parsed_timestamps
                and "end" in parsed_timestamps
                and parsed_timestamps["begin"] > parsed_timestamps["end"]
            ):
                errors.append(
                    f"searches[{search_index}].begin must not be later than end"
                )
            for type_index, record in enumerate(search.get("record_types", [])):
                if not isinstance(record, dict):
                    continue
                record_type = record.get("record_type")
                if record_type in allowed[app]:
                    continue
                close = get_close_matches(
                    str(record_type), sorted(allowed[app]), n=3, cutoff=0.45
                )
                suggestion = f"; closest registered types: {close}" if close else ""
                errors.append(
                    f"searches[{search_index}].record_types[{type_index}].record_type "
                    f"{record_type!r} is not registered for {app}{suggestion}"
                )
        return errors

    @staticmethod
    def _validate_answer(result: dict[str, Any]) -> list[str]:
        answer = result.get("answer")
        if isinstance(answer, str) and not answer.strip():
            return ["answer must not be empty"]
        return []

    @staticmethod
    def _validate_summary(result: dict[str, Any]) -> list[str]:
        summary = result.get("summary")
        if isinstance(summary, str) and not summary.strip():
            return ["summary must not be empty"]
        return []

    @staticmethod
    def _decode_object(content: str) -> dict[str, Any]:
        """Accept the first JSON object when a local model appends a control token."""
        stripped = content.lstrip()
        result, _ = json.JSONDecoder().raw_decode(stripped)
        if not isinstance(result, dict):
            raise ValueError("Historian resolver did not return a JSON object.")
        return result

    def _extract_reasoning(self, body: dict[str, Any]) -> str | None:
        if not self.settings.resolver_include_reasoning:
            return None
        message = body.get("choices", [{}])[0].get("message", {})
        for key in ("reasoning", "reasoning_content", "thinking"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None


@dataclass(slots=True)
class FakeQueryResolver:
    plans: list[dict[str, Any]] = field(default_factory=list)
    answers: list[dict[str, Any]] = field(default_factory=list)
    chunk_summaries: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def plan_searches(
        self,
        *,
        question: str,
        current_time: str,
        catalog: list[dict[str, Any]],
        query_id: str,
        step: int,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "kind": "plan",
                "question": question,
                "current_time": current_time,
                "catalog": catalog,
                "query_id": query_id,
                "step": step,
            }
        )
        return self.plans.pop(0) if self.plans else {"searches": []}

    def synthesize_answer(
        self,
        *,
        question: str,
        current_time: str,
        evidence: str,
        hard_cap_reached: bool = False,
        query_id: str,
        step: int,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "kind": "answer",
                "question": question,
                "current_time": current_time,
                "evidence": evidence,
                "hard_cap_reached": hard_cap_reached,
                "query_id": query_id,
                "step": step,
            }
        )
        if self.answers:
            return self.answers.pop(0)
        return {
            "status": "insufficient_evidence",
            "answer": "No configured fake answer.",
        }

    def summarize_evidence_chunk(
        self,
        *,
        question: str,
        current_time: str,
        evidence: str,
        chunk_index: int,
        total_chunks: int,
        record_start: int,
        record_end: int,
        total_records: int,
        query_id: str,
        step: int,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "kind": "chunk_summary",
                "question": question,
                "current_time": current_time,
                "evidence": evidence,
                "chunk_index": chunk_index,
                "total_chunks": total_chunks,
                "record_start": record_start,
                "record_end": record_end,
                "total_records": total_records,
                "query_id": query_id,
                "step": step,
            }
        )
        if self.chunk_summaries:
            return self.chunk_summaries.pop(0)
        return {"summary": f"Records {record_start}-{record_end} summarized."}

    def synthesize_summaries(
        self,
        *,
        question: str,
        current_time: str,
        summaries: list[str],
        hard_cap_reached: bool = False,
        query_id: str,
        step: int,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "kind": "final_answer",
                "question": question,
                "current_time": current_time,
                "summaries": summaries,
                "hard_cap_reached": hard_cap_reached,
                "query_id": query_id,
                "step": step,
            }
        )
        if self.answers:
            return self.answers.pop(0)
        return {
            "status": "insufficient_evidence",
            "answer": "No configured fake answer.",
        }
