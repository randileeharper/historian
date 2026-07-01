from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

import historian.client as client_module
from historian.client import HistorianClient
from historian.errors import HistorianError


def _patch_http(monkeypatch, handler) -> MagicMock:
    """Route HistorianClient's httpx.Client calls through a MockTransport.

    Returns a MagicMock whose ``call_count`` tracks how many HTTP requests were
    made across all retry attempts.
    """
    transport = httpx.MockTransport(handler)
    request_counter = MagicMock()

    real_client = client_module.httpx.Client

    def fake_client(*args: Any, **kwargs: Any) -> httpx.Client:
        request_counter()
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(client_module.httpx, "Client", fake_client)
    monkeypatch.setattr(client_module.time, "sleep", lambda _: None)
    return request_counter


def _make_client(retry_count: int = 2) -> HistorianClient:
    return HistorianClient(
        base_url="http://testserver",
        token="test-token",
        timeout_seconds=1.0,
        retry_count=retry_count,
    )


def test_does_not_retry_4xx(monkeypatch) -> None:
    """A 409 conflict must not be retried; it surfaces immediately (one request)."""
    counter = _patch_http(monkeypatch, lambda request: httpx.Response(409, json={"error": "conflict"}))
    client = _make_client(retry_count=2)

    with pytest.raises(HistorianError) as exc_info:
        client.emit({"event_type": "test"})
    assert "409" in str(exc_info.value)
    assert counter.call_count == 1


def test_does_not_retry_401(monkeypatch) -> None:
    """A 401 auth failure must not be retried."""
    counter = _patch_http(monkeypatch, lambda request: httpx.Response(401, text="unauthorized"))
    client = _make_client(retry_count=2)

    with pytest.raises(HistorianError) as exc_info:
        client.search({"query": "test"})
    assert "401" in str(exc_info.value)
    assert counter.call_count == 1


def test_retries_5xx_then_fails(monkeypatch) -> None:
    """A persistent 500 is retried up to retry_count, then surfaces an error."""
    counter = _patch_http(monkeypatch, lambda request: httpx.Response(500, text="server error"))
    client = _make_client(retry_count=2)

    with pytest.raises(HistorianError) as exc_info:
        client.search({"query": "test"})
    assert "500" in str(exc_info.value)
    # 1 initial + 2 retries = 3 total attempts.
    assert counter.call_count == 3


def test_2xx_returns_payload(monkeypatch) -> None:
    """A 200 response returns the parsed JSON payload without retry."""
    counter = _patch_http(monkeypatch, lambda request: httpx.Response(200, json={"status": "ok", "id": "evt-1"}))
    client = _make_client(retry_count=2)

    payload = client.get_event("evt-1")
    assert payload == {"status": "ok", "id": "evt-1"}
    assert counter.call_count == 1


def test_5xx_retried_then_succeeds(monkeypatch) -> None:
    """A transient 500 followed by 200 succeeds on the second attempt."""
    responses = iter([httpx.Response(500, text="boom"), httpx.Response(200, json={"status": "ok"})])
    counter = _patch_http(monkeypatch, lambda request: next(responses))
    client = _make_client(retry_count=2)

    payload = client.get_event("evt-1")
    assert payload == {"status": "ok"}
    assert counter.call_count == 2


def test_transport_error_is_retried(monkeypatch) -> None:
    """A connection error is a transport failure and is retried like 5xx."""
    counter = _patch_http(
        monkeypatch,
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("connection refused")),
    )
    client = _make_client(retry_count=2)

    with pytest.raises(HistorianError) as exc_info:
        client.get_event("evt-1")
    assert "failed" in str(exc_info.value).lower()
    assert counter.call_count == 3
