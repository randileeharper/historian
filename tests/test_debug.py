from __future__ import annotations

import json
import logging
import os

from historian.app import build_app
from historian.cli import main
from historian.config import Settings
from historian.debug import QueryTranscript, configure_logging

from conftest import event


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "debug_enabled": True,
        "debug_log_path": str(tmp_path / "historian-debug.log"),
        "resolver_debug_log_path": str(tmp_path / "historian-resolver.log"),
        "database_path": str(tmp_path / "historian.db"),
        "resolver_backend": "fake",
    }
    values.update(overrides)
    return Settings(**values)


def test_debug_disabled_creates_no_files(tmp_path) -> None:
    settings = _settings(tmp_path, debug_enabled=False)
    configure_logging(settings, clear_operational_log=True)
    transcript = QueryTranscript(settings)
    transcript.start(query_id="q1", caller_app_id="test", question="hello")
    assert not settings.expanded_debug_log_path.exists()
    assert not settings.expanded_resolver_debug_log_path.exists()


def test_operational_log_clears_only_when_requested(tmp_path) -> None:
    settings = _settings(tmp_path)
    settings.expanded_debug_log_path.write_text("old\n", encoding="utf-8")
    logger = configure_logging(settings, clear_operational_log=False)
    logger.debug("preserved")
    for handler in logger.handlers:
        handler.flush()
    assert "old" in settings.expanded_debug_log_path.read_text(encoding="utf-8")

    logger = configure_logging(settings, clear_operational_log=True)
    logger.debug("fresh")
    for handler in logger.handlers:
        handler.flush()
    content = settings.expanded_debug_log_path.read_text(encoding="utf-8")
    assert "old" not in content
    assert "fresh" in content
    assert os.stat(settings.expanded_debug_log_path).st_mode & 0o777 == 0o600


def test_last_query_overwrites_and_ignores_older_query_writes(tmp_path) -> None:
    settings = _settings(tmp_path)
    transcript = QueryTranscript(settings)
    transcript.start(query_id="old", caller_app_id="one", question="old question")
    transcript.append_call(
        query_id="old",
        step=1,
        model="model",
        endpoint="http://model/chat/completions",
        system_prompt="old system",
        user_message="old user",
        elapsed_ms=1.0,
        http_status=200,
        response_content="old response",
        reasoning_content=None,
        error=None,
    )
    transcript.start(query_id="new", caller_app_id="two", question="new question")
    transcript.append_call(
        query_id="old",
        step=2,
        model="model",
        endpoint="http://model/chat/completions",
        system_prompt="stale system",
        user_message="stale user",
        elapsed_ms=1.0,
        http_status=200,
        response_content="stale response",
        reasoning_content=None,
        error=None,
    )
    transcript.append_call(
        query_id="new",
        step=1,
        model="model",
        endpoint="http://model/chat/completions",
        system_prompt="new system",
        user_message="new user",
        elapsed_ms=2.0,
        http_status=200,
        response_content="new response",
        reasoning_content="new reasoning",
        error=None,
    )
    transcript.finish(
        query_id="new",
        status="ok",
        search_step_count=1,
        elapsed_ms=3.0,
    )
    content = settings.expanded_resolver_debug_log_path.read_text(encoding="utf-8")
    assert "new question" in content
    assert "new system" in content
    assert "new response" in content
    assert "new reasoning" in content
    assert "stale response" not in content
    assert "old question" not in content
    assert "QUERY RESULT" in content
    assert os.stat(settings.expanded_resolver_debug_log_path).st_mode & 0o777 == 0o600


def test_doctor_reports_debug_paths(config_path, tmp_path, capsys) -> None:
    config_path.write_text(
        json.dumps(
            {
                "database_path": str(tmp_path / "historian.db"),
                "resolver_backend": "fake",
                "debug_enabled": True,
                "debug_log_path": str(tmp_path / "debug.log"),
                "resolver_debug_log_path": str(tmp_path / "resolver.log"),
            }
        ),
        encoding="utf-8",
    )
    assert main(["--config", str(config_path), "doctor"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["debug"]["enabled"] is True
    assert payload["debug"]["operational_log"]["writable"] is True
    assert payload["debug"]["resolver_log"]["writable"] is True


def test_operational_log_uses_metadata_not_event_payload(tmp_path, vesper_manifest) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "database_path": str(tmp_path / "historian.db"),
                "resolver_backend": "fake",
                "debug_enabled": True,
                "debug_log_path": str(tmp_path / "debug.log"),
                "resolver_debug_log_path": str(tmp_path / "resolver.log"),
            }
        ),
        encoding="utf-8",
    )
    context = build_app(str(config), clear_operational_log=True)
    token = context.store.install_app(vesper_manifest)
    principal = context.store.authenticate(token)
    context.service.ingest(principal, event())
    for handler in logging.getLogger("historian").handlers:
        handler.flush()
    content = (tmp_path / "debug.log").read_text(encoding="utf-8")
    assert "event-1" in content
    assert "music.playback.started" in content
    assert "do-not-store" not in content
