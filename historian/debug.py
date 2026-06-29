"""Operational logging and last-query resolver transcripts."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings


LOGGER_NAME = "historian"
_FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def _prepare_private_file(path: Path, *, clear: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_TRUNC if clear else os.O_APPEND)
    descriptor = os.open(path, flags, 0o600)
    os.close(descriptor)
    os.chmod(path, 0o600)


def configure_logging(settings: Settings, *, clear_operational_log: bool) -> logging.Logger:
    """Configure the Historian logger without mutating unrelated application loggers."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG if settings.debug_enabled else getattr(logging, settings.log_level))
    logger.propagate = False
    console = logging.StreamHandler()
    console.setLevel(getattr(logging, settings.log_level))
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger.addHandler(console)
    if settings.debug_enabled:
        path = settings.expanded_debug_log_path
        _prepare_private_file(path, clear=clear_operational_log)
        handler = logging.FileHandler(path, encoding="utf-8", errors="backslashreplace")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter(_FILE_FORMAT))
        logger.addHandler(handler)
    return logger


def get_logger(component: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if component is None else f"{LOGGER_NAME}.{component}")


class QueryTranscript:
    """Keep only the newest top-level NLP query in a readable transcript."""

    def __init__(self, settings: Settings):
        self._enabled = settings.debug_enabled
        self._path = settings.expanded_resolver_debug_log_path
        self._lock = threading.Lock()
        self._active_query_id: str | None = None

    def start(self, *, query_id: str, caller_app_id: str, question: str) -> None:
        if not self._enabled:
            return
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        content = (
            "HISTORIAN LAST NLP QUERY\n"
            f"query_id: {query_id}\n"
            f"caller_app_id: {caller_app_id}\n"
            f"started_at: {timestamp}\n"
            f"question: {question}\n\n"
        )
        with self._lock:
            _prepare_private_file(self._path, clear=True)
            self._path.write_text(content, encoding="utf-8", errors="backslashreplace")
            os.chmod(self._path, 0o600)
            self._active_query_id = query_id

    def append_call(
        self,
        *,
        query_id: str,
        step: int,
        model: str,
        endpoint: str,
        system_prompt: str,
        user_message: str,
        elapsed_ms: float | None,
        http_status: int | None,
        response_content: str | None,
        reasoning_content: str | None,
        error: str | None,
    ) -> None:
        if not self._enabled:
            return
        sections = [
            f"=== MODEL CALL {step} ===",
            f"model: {model}",
            f"endpoint: {endpoint}",
            f"elapsed_ms: {elapsed_ms if elapsed_ms is not None else ''}",
            f"http_status: {http_status if http_status is not None else ''}",
            "",
            "SYSTEM PROMPT",
            system_prompt.strip(),
            "",
            "USER MESSAGE",
            user_message,
            "",
            "RESPONSE",
            response_content if response_content is not None else "[no response content]",
        ]
        if reasoning_content:
            sections.extend(["", "REASONING", reasoning_content])
        if error:
            sections.extend(["", "ERROR", error])
        sections.extend(["", ""])
        self._append(query_id, "\n".join(sections))

    def finish(
        self,
        *,
        query_id: str,
        status: str,
        search_step_count: int,
        elapsed_ms: float,
        error: str | None = None,
    ) -> None:
        if not self._enabled:
            return
        lines = [
            "=== QUERY RESULT ===",
            f"status: {status}",
            f"search_step_count: {search_step_count}",
            f"elapsed_ms: {elapsed_ms}",
        ]
        if error:
            lines.extend(["", "ERROR", error])
        lines.extend(["", ""])
        self._append(query_id, "\n".join(lines))

    def _append(self, query_id: str, content: str) -> None:
        with self._lock:
            if query_id != self._active_query_id:
                return
            with self._path.open("a", encoding="utf-8", errors="backslashreplace") as handle:
                handle.write(content)


def check_debug_path(path: Path) -> dict[str, Any]:
    """Check whether a configured debug file can be safely created and appended."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _prepare_private_file(path, clear=False)
        with path.open("a", encoding="utf-8"):
            pass
        return {"path": str(path), "writable": True, "error": None}
    except OSError as exc:
        return {"path": str(path), "writable": False, "error": str(exc)}
