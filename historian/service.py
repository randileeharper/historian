"""Historian domain services."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from typing import Any

from .config import Settings
from .debug import QueryTranscript, get_logger
from .errors import AuthorizationError, QueryError, ValidationError
from .models import AuthPrincipal, EventEnvelope, QueryResult, SearchSpec, StoredEvent, utc_now
from .resolver import QueryResolver
from .storage import SQLiteHistorianStore


_LOG = get_logger("service")


class HistorianService:
    def __init__(
        self,
        store: SQLiteHistorianStore,
        resolver: QueryResolver,
        settings: Settings,
        transcript: QueryTranscript,
    ):
        self.store = store
        self.resolver = resolver
        self.settings = settings
        self.transcript = transcript

    @staticmethod
    def require_scope(principal: AuthPrincipal, scope: str) -> None:
        if scope not in principal.scopes:
            raise AuthorizationError(f"Token for {principal.app_id} lacks scope {scope}.")

    def ingest(self, principal: AuthPrincipal, payload: dict[str, Any]) -> tuple[StoredEvent, bool]:
        self.require_scope(principal, "events:write")
        event = self.parse_event(payload)
        encoded_size = len(json.dumps(payload, ensure_ascii=True).encode("utf-8"))
        if encoded_size > self.settings.max_event_bytes:
            raise ValidationError(f"Event exceeds max_event_bytes ({self.settings.max_event_bytes}).")
        stored, duplicate = self.store.ingest(principal, event)
        _LOG.debug(
            "event_id=%s producer_app=%s type=%s duplicate=%s ingest_complete",
            stored.event_id,
            stored.producer_app_id,
            stored.event_type,
            duplicate,
        )
        return stored, duplicate

    def ingest_batch(
        self, principal: AuthPrincipal, payloads: list[dict[str, Any]]
    ) -> list[tuple[StoredEvent, bool]]:
        self.require_scope(principal, "events:write")
        if not payloads or len(payloads) > self.settings.max_batch_events:
            raise ValidationError(f"Batch must contain 1-{self.settings.max_batch_events} events.")
        events: list[EventEnvelope] = []
        for payload in payloads:
            encoded_size = len(json.dumps(payload, ensure_ascii=True).encode("utf-8"))
            if encoded_size > self.settings.max_event_bytes:
                raise ValidationError(f"Event exceeds max_event_bytes ({self.settings.max_event_bytes}).")
            events.append(self.parse_event(payload))
        results = self.store.ingest_batch(principal, events)
        _LOG.debug(
            "producer_app=%s batch_count=%s duplicate_count=%s batch_ingest_complete",
            principal.app_id,
            len(results),
            sum(1 for _, duplicate in results if duplicate),
        )
        return results

    def raw_search(self, principal: AuthPrincipal, spec: SearchSpec) -> list[StoredEvent]:
        self.require_scope(principal, "events:read")
        normalized = self._normalize_search(spec)
        return self.store.search(
            normalized,
            max_regex_candidates=self.settings.max_regex_candidates,
            regex_timeout_seconds=self.settings.regex_timeout_seconds,
        )

    def get_event(self, principal: AuthPrincipal, event_id: str) -> StoredEvent | None:
        self.require_scope(principal, "events:read")
        return self.store.get_event(event_id)

    def query(self, principal: AuthPrincipal, question: str) -> QueryResult:
        self.require_scope(principal, "query:nlp")
        question = question.strip()
        if not question:
            raise ValidationError("Question cannot be empty.")
        query_id = str(uuid.uuid4())
        started = time.perf_counter()
        try:
            self.transcript.start(query_id=query_id, caller_app_id=principal.app_id, question=question)
        except OSError:
            _LOG.warning("query_id=%s transcript_start_failed", query_id)
        _LOG.info("query_id=%s caller_app=%s query_started question_chars=%s", query_id, principal.app_id, len(question))
        searches: list[dict[str, Any]] = []
        evidence: dict[tuple[str, str, str], StoredEvent] = {}
        result: QueryResult | None = None

        try:
            current_time = self._local_time()
            catalog = self.store.query_catalog()
            plan = self.resolver.plan_searches(
                question=question,
                current_time=current_time,
                catalog=catalog,
                query_id=query_id,
                step=1,
            )
            raw_searches = plan.get("searches")
            if not isinstance(raw_searches, list):
                raise QueryError("Resolver search plan omitted searches.")
            planned_limit = plan.get("limit")
            if (
                not isinstance(planned_limit, int)
                or isinstance(planned_limit, bool)
                or planned_limit < 1
            ):
                planned_limit = None
            sort = plan.get("sort", "oldest")
            if sort not in {"oldest", "newest"}:
                sort = "oldest"
            selection_limit = min(
                planned_limit if planned_limit is not None else self.settings.max_query_records,
                self.settings.max_query_records,
            )
            detect_hard_cap = planned_limit is None or planned_limit > self.settings.max_query_records
            per_search_limit = selection_limit + (1 if detect_hard_cap else 0)
            allowed = {
                (app["app"], record_type)
                for app in catalog
                for record_type in app["record_types"]
            }
            for raw in raw_searches[:50]:
                if not isinstance(raw, dict):
                    _LOG.warning("query_id=%s skipped_non_object_search", query_id)
                    continue
                app = str(raw.get("app", "")).strip()
                record_types = raw.get("record_types")
                if not isinstance(record_types, list):
                    _LOG.warning("query_id=%s app=%s skipped_missing_record_types", query_id, app)
                    continue
                begin = self._valid_optional_timestamp(raw.get("begin"))
                end = self._valid_optional_timestamp(raw.get("end"))
                for record in record_types[:50]:
                    if not isinstance(record, dict):
                        continue
                    record_type = str(record.get("record_type", "")).strip()
                    if (app, record_type) not in allowed:
                        _LOG.warning(
                            "query_id=%s app=%s type=%s skipped_unknown_record_type",
                            query_id,
                            app,
                            record_type,
                        )
                        continue
                    search_text = str(record.get("search", "")).strip()
                    spec = self._normalize_search(
                        SearchSpec(
                            apps=[app],
                            event_types=[record_type],
                            occurred_after=begin,
                            occurred_before=end,
                            exact_phrases=[search_text] if search_text else [],
                            order="asc" if sort == "oldest" else "desc",
                            limit=per_search_limit,
                        ),
                        max_results=per_search_limit,
                    )
                    matches = self.store.search(
                        spec,
                        max_regex_candidates=self.settings.max_regex_candidates,
                        regex_timeout_seconds=self.settings.regex_timeout_seconds,
                    )
                    for event in matches:
                        identity = (
                            event.producer_app_id,
                            event.source,
                            event.event_id,
                        )
                        evidence[identity] = event
                    search_summary = {
                        "app": app,
                        "record_type": record_type,
                        "begin": spec.occurred_after,
                        "end": spec.occurred_before,
                        "search": search_text or None,
                        "count": len(matches),
                    }
                    searches.append(search_summary)
                    _LOG.debug(
                        "query_id=%s search app=%s type=%s begin=%s end=%s text=%s results=%s",
                        query_id,
                        app,
                        record_type,
                        spec.occurred_after,
                        spec.occurred_before,
                        bool(search_text),
                        len(matches),
                    )

            if not evidence:
                result = QueryResult(
                    status="insufficient_evidence",
                    answer="No stored records matched the requested applications, record types, and filters.",
                    query_id=query_id,
                    searches=searches,
                )
                return result

            ordered = sorted(
                evidence.values(),
                key=self._event_sort_key,
                reverse=sort == "newest",
            )
            hard_cap_reached = detect_hard_cap and len(ordered) > self.settings.max_query_records
            selected = ordered[:selection_limit]
            chunks = self._evidence_chunks(question, selected, sort)
            if len(chunks) == 1:
                answer = self.resolver.synthesize_answer(
                    question=question,
                    current_time=current_time,
                    evidence=chunks[0][2],
                    hard_cap_reached=hard_cap_reached,
                    query_id=query_id,
                    step=2,
                )
            else:
                summaries: list[str] = []
                for chunk_index, (record_start, record_end, text) in enumerate(
                    chunks, start=1
                ):
                    summary = self.resolver.summarize_evidence_chunk(
                        question=question,
                        current_time=current_time,
                        evidence=text,
                        chunk_index=chunk_index,
                        total_chunks=len(chunks),
                        record_start=record_start,
                        record_end=record_end,
                        total_records=len(selected),
                        query_id=query_id,
                        step=chunk_index + 1,
                    )
                    summary_text = str(summary.get("summary") or "").strip()
                    if not summary_text:
                        raise QueryError("Resolver chunk summary is empty.")
                    summaries.append(summary_text)
                answer = self.resolver.synthesize_summaries(
                    question=question,
                    current_time=current_time,
                    summaries=summaries,
                    hard_cap_reached=hard_cap_reached,
                    query_id=query_id,
                    step=len(chunks) + 2,
                )
            status = answer.get("status")
            answer_text = str(answer.get("answer") or "").strip()
            if status not in {"ok", "partial", "insufficient_evidence"}:
                raise QueryError("Resolver answer status is invalid.")
            if not answer_text:
                raise QueryError("Resolver answer is empty.")
            cap_message = None
            if hard_cap_reached:
                status = "partial"
                cap_message = (
                    f"Result limited to the first {self.settings.max_query_records} "
                    "records because max_query_records was reached."
                )
            result = QueryResult(
                status=status,
                answer=answer_text,
                query_id=query_id,
                searches=searches,
                message=cap_message,
            )
            return result
        except Exception as exc:
            _LOG.exception("query_id=%s query_failed", query_id)
            result = QueryResult(
                status="error",
                answer="Historian could not complete the query.",
                query_id=query_id,
                searches=searches,
                message=str(exc),
            )
            return result
        finally:
            if result is not None:
                elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
                self.transcript.finish(
                    query_id=query_id,
                    status=result.status,
                    search_step_count=len(searches),
                    elapsed_ms=elapsed_ms,
                    error=result.message,
                )
                _LOG.info(
                    "query_id=%s query_finished status=%s searches=%s elapsed_ms=%s",
                    query_id,
                    result.status,
                    len(searches),
                    elapsed_ms,
                )
                self._record_query(principal, question, result, started)

    def _record_query(
        self, principal: AuthPrincipal, question: str, result: QueryResult, started: float
    ) -> None:
        payload = {
            "specversion": "1.0",
            "id": result.query_id,
            "source": "app://historian/query",
            "type": "historian.query.completed",
            "time": utc_now(),
            "schemaversion": 2,
            "visibility": "private",
            "data": {
                "caller_app_id": principal.app_id,
                "question": question,
                "status": result.status,
                "searches": result.searches,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "answer": result.answer,
            },
        }
        try:
            self.store.ingest_internal(self.parse_event(payload))
        except Exception as exc:
            # Query logging must not erase an otherwise valid answer.
            _LOG.warning("query_id=%s query_audit_failed error=%s", result.query_id, exc)

    def _normalize_search(
        self, spec: SearchSpec, *, max_results: int | None = None
    ) -> SearchSpec:
        result_cap = max_results or self.settings.max_search_results
        spec.limit = max(1, min(int(spec.limit), result_cap))
        if spec.order not in {"asc", "desc"}:
            spec.order = "desc"
        spec.required_terms = self._literal_list(spec.required_terms, 12, 128)
        spec.exact_phrases = self._literal_list(spec.exact_phrases, 8, 256)
        spec.regex_patterns = self._literal_list(
            spec.regex_patterns, self.settings.max_regex_patterns, self.settings.max_regex_length
        )
        spec.apps = self._literal_list(spec.apps, 20, 128)
        spec.event_types = self._literal_list(spec.event_types, 20, 256)
        spec.record_families = self._literal_list(spec.record_families, 8, 64)
        if not any(
            (
                spec.record_families,
                spec.apps,
                spec.event_types,
                spec.occurred_after,
                spec.occurred_before,
                spec.required_terms,
                spec.exact_phrases,
                spec.field_predicates,
            )
        ) and spec.regex_patterns:
            raise ValidationError("Regex search requires at least one non-regex bounding constraint.")
        return spec

    @staticmethod
    def _literal_list(values: list[str], max_items: int, max_length: int) -> list[str]:
        result: list[str] = []
        for value in values[:max_items]:
            text = str(value).strip()
            if text and len(text) <= max_length and text not in result:
                result.append(text)
        return result

    @staticmethod
    def _local_time() -> str:
        return datetime.now().astimezone().isoformat()

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @classmethod
    def _valid_optional_timestamp(cls, value: Any) -> str | None:
        text = cls._optional_text(value)
        if text is None:
            return None
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return text

    def _evidence_chunks(
        self,
        question: str,
        events: list[StoredEvent],
        sort: str,
    ) -> list[tuple[int, int, str]]:
        lines = [self._format_event(event) for event in events]
        chunks: list[tuple[int, int, str]] = []
        start = 0
        order_label = "oldest first" if sort == "oldest" else "newest first"
        while start < len(lines):
            selected_lines: list[str] = []
            end = start
            while end < len(lines) and len(selected_lines) < self.settings.max_records_per_model_call:
                candidate_lines = selected_lines + [lines[end]]
                header = (
                    f"Original question: {question}\n"
                    f"Records {start + 1}-{end + 1} of {len(lines)}, {order_label}.\n\n"
                )
                candidate = header + "\n".join(candidate_lines)
                if (
                    selected_lines
                    and len(candidate) > self.settings.max_evidence_characters
                ):
                    break
                selected_lines = candidate_lines
                end += 1
            header = (
                f"Original question: {question}\n"
                f"Records {start + 1}-{end} of {len(lines)}, {order_label}.\n\n"
            )
            chunks.append((start + 1, end, header + "\n".join(selected_lines)))
            start = end
        return chunks

    @staticmethod
    def _event_sort_key(event: StoredEvent) -> tuple[datetime, str, str, str]:
        timestamp = datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
        return timestamp, event.producer_app_id, event.source, event.event_id

    @staticmethod
    def _format_event(event: StoredEvent) -> str:
        metadata_prefixes = (
            "source:",
            "type:",
            "time:",
            "family:",
            "subject:",
            "correlation_id:",
            "causation_id:",
            "session_id:",
        )
        detail_lines: list[str] = []
        for line in event.canonical_text.splitlines():
            stripped = line.strip()
            if not stripped or line.startswith(metadata_prefixes):
                continue
            name, separator, value = stripped.partition(": ")
            detail_lines.append(f"{name}={value}" if separator else stripped)
        details = "; ".join(detail_lines)
        occurred_at = datetime.fromisoformat(
            event.occurred_at.replace("Z", "+00:00")
        ).astimezone()
        timestamp = occurred_at.strftime("%Y-%m-%d %H:%M:%S %Z")
        return (
            f"[{timestamp}] {event.producer_app_id} | {event.event_type}"
            + (f" | {details}" if details else "")
        )

    @staticmethod
    def parse_event(payload: dict[str, Any]) -> EventEnvelope:
        if not isinstance(payload, dict):
            raise ValidationError("Event must be an object.")
        required = {"specversion", "id", "source", "type", "time", "schemaversion", "data"}
        missing = sorted(required - set(payload))
        if missing:
            raise ValidationError(f"Event is missing fields: {', '.join(missing)}")
        if payload["specversion"] != "1.0":
            raise ValidationError("Only CloudEvents specversion 1.0 is supported.")
        if not isinstance(payload["data"], dict):
            raise ValidationError("Event data must be an object.")
        visibility = str(payload.get("visibility", "private"))
        if visibility not in {"private", "shared"}:
            raise ValidationError("visibility must be private or shared.")
        return EventEnvelope(
            specversion="1.0",
            event_id=str(payload["id"]).strip(),
            source=str(payload["source"]).strip(),
            event_type=str(payload["type"]).strip(),
            occurred_at=str(payload["time"]).strip(),
            schema_version=int(payload["schemaversion"]),
            data=payload["data"],
            subject=str(payload["subject"]).strip() if payload.get("subject") is not None else None,
            correlation_id=str(payload["correlationid"]).strip() if payload.get("correlationid") else None,
            causation_id=str(payload["causationid"]).strip() if payload.get("causationid") else None,
            session_id=str(payload["sessionid"]).strip() if payload.get("sessionid") else None,
            visibility=visibility,
        )
