"""Small synchronous HTTP client for Historian applications."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
from a2a.client import ClientConfig
from a2a.client.client_factory import ClientFactory
from a2a.helpers import new_text_part
from a2a.types import Message, Role, SendMessageRequest, Task, TaskState
from google.protobuf.json_format import MessageToDict

from .errors import HistorianError


@dataclass(slots=True)
class HistorianClient:
    base_url: str
    token: str
    timeout_seconds: float = 30.0
    verify_tls: bool = True
    retry_count: int = 2

    def emit(self, event: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/events", json=event)

    def emit_batch(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request("POST", "/v1/events:batch", json={"events": events})

    def search(self, search: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/search", json=search)

    def get_event(self, event_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/events/{event_id}")

    def query(self, question: str) -> dict[str, Any]:
        return asyncio.run(self._query_a2a(question))

    async def _query_a2a(self, question: str) -> dict[str, Any]:
        http = httpx.AsyncClient(
            timeout=self.timeout_seconds,
            verify=self.verify_tls,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        client = None
        try:
            client = await ClientFactory(
                ClientConfig(streaming=False, polling=False, httpx_client=http)
            ).create_from_url(self.base_url)
            message = Message(
                role=Role.ROLE_USER,
                message_id=str(uuid4()),
                parts=[new_text_part(question, media_type="text/plain")],
            )
            async for response in client.send_message(SendMessageRequest(message=message)):
                if response.HasField("task"):
                    return self._task_payload(response.task)
                if response.HasField("message"):
                    payload = self._parts_payload(response.message.parts)
                    if payload is not None:
                        return payload
            raise HistorianError("A2A query returned no result.")
        except (httpx.HTTPError, ValueError) as exc:
            raise HistorianError(f"A2A query failed: {exc}") from exc
        finally:
            if client is not None:
                await client.close()
            if not http.is_closed:
                await http.aclose()

    def _task_payload(self, task: Task) -> dict[str, Any]:
        terminal = {
            TaskState.TASK_STATE_COMPLETED,
            TaskState.TASK_STATE_FAILED,
            TaskState.TASK_STATE_REJECTED,
            TaskState.TASK_STATE_CANCELED,
        }
        if task.status.state not in terminal:
            raise HistorianError("A2A query returned a non-terminal task.")
        for artifact in task.artifacts:
            payload = self._parts_payload(artifact.parts)
            if payload is not None:
                return payload
        if task.status.HasField("message"):
            payload = self._parts_payload(task.status.message.parts)
            if payload is not None:
                return payload
        raise HistorianError("A2A query task contained no result payload.")

    @staticmethod
    def _parts_payload(parts: Any) -> dict[str, Any] | None:
        for part in parts:
            if part.HasField("data"):
                payload = MessageToDict(part.data)
                if isinstance(payload, dict):
                    return payload
            if part.HasField("text") and part.text.strip().startswith("{"):
                payload = json.loads(part.text)
                if isinstance(payload, dict):
                    return payload
        return None

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"}
        last_error: Exception | None = None
        for attempt in range(self.retry_count + 1):
            try:
                with httpx.Client(
                    base_url=self.base_url,
                    timeout=self.timeout_seconds,
                    verify=self.verify_tls,
                ) as client:
                    response = client.request(method, path, headers=headers, **kwargs)
            except (httpx.HTTPError, ValueError) as exc:
                # Transport/connectivity failure or malformed response: retryable.
                last_error = exc
                if attempt < self.retry_count:
                    time.sleep(0.1 * (2**attempt))
                    continue
                raise HistorianError(f"Historian request failed: {last_error}") from exc

            # 4xx (except 429 Too Many Requests) are not retryable: validation, auth,
            # authorization, and conflict failures must surface immediately rather than
            # be retried against the same server. See docs/integration.md.
            if 400 <= response.status_code < 500 and response.status_code != 429:
                raise HistorianError(
                    f"Historian returned {response.status_code}: {response.text}"
                )
            # 5xx (and 429) are transient: retry when attempts remain.
            if response.status_code >= 500 and attempt < self.retry_count:
                last_error = HistorianError(
                    f"Historian returned {response.status_code}: {response.text}"
                )
                time.sleep(0.1 * (2**attempt))
                continue
            if response.status_code >= 500:
                # Final attempt on a persistent server error: surface it, do not raise
                # the raw HTTPStatusError (which would bypass Historian's error contract).
                raise HistorianError(
                    f"Historian returned {response.status_code}: {response.text}"
                )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise HistorianError("Historian returned a non-object response.")
            return payload
        raise HistorianError(f"Historian request failed: {last_error}")
