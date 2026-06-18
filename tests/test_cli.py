from __future__ import annotations

import json
import os

from historian.app import build_app
from historian.cli import main


def test_app_install_and_doctor(config_path, tmp_path, capsys) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "app_id": "test-app",
                "description": "Test application.",
                "default_scopes": ["events:write"],
                "schemas": [
                    {
                        "event_type": "test.event.created",
                        "version": 1,
                        "record_family": "event",
                        "description": "Test event.",
                        "searchable_fields": ["message"],
                        "redacted_fields": [],
                        "json_schema": {
                            "$schema": "https://json-schema.org/draft/2020-12/schema",
                            "type": "object",
                            "properties": {"message": {"type": "string"}},
                            "required": ["message"],
                            "additionalProperties": False,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert main(["--config", str(config_path), "app", "install", str(manifest)]) == 0
    installed = json.loads(capsys.readouterr().out)
    assert installed["token"].startswith("hist_")
    assert main(["--config", str(config_path), "doctor"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["status"] == "ok"
    assert doctor["apps"] == 2


def test_serve_passes_configured_log_level(config_path, monkeypatch) -> None:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["log_level"] = "WARNING"
    payload["debug_enabled"] = True
    payload["debug_log_path"] = str(config_path.parent / "serve-debug.log")
    payload["resolver_debug_log_path"] = str(config_path.parent / "serve-resolver.log")
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    (config_path.parent / "serve-debug.log").write_text("stale\n", encoding="utf-8")
    captured = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    assert main(["--config", str(config_path), "serve"]) == 0
    assert captured["log_level"] == "warning"
    assert "stale" not in (config_path.parent / "serve-debug.log").read_text(encoding="utf-8")


def test_init_cli_token_becomes_default_credential(config_path, tmp_path, capsys) -> None:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["cli_token_path"] = str(tmp_path / "cli-token")
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    assert main(["--config", str(config_path), "token", "init-cli"]) == 0
    created = json.loads(capsys.readouterr().out)
    token_path = tmp_path / "cli-token"
    assert created["scopes"] == ["events:read", "events:write", "query:nlp"]
    assert token_path.is_file()
    assert os.stat(token_path).st_mode & 0o777 == 0o600

    context = build_app(str(config_path))
    principal = context.store.authenticate(token_path.read_text(encoding="utf-8").strip())
    assert principal.app_id == "historian"
    assert principal.scopes == frozenset({"events:read", "events:write", "query:nlp"})

    assert main(["--config", str(config_path), "events", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["status"] == "ok"
