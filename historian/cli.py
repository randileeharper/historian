"""Historian command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

import httpx

from .app import build_app
from .client import HistorianClient
from .config import Settings
from .debug import check_debug_path
from .errors import ConfigError, HistorianConnectionError, HistorianError
from .http import create_http_app
from .manifests import VALID_SCOPES, load_manifest
from .models import SearchSpec, to_jsonable


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="historian", description="Historian event and continuity service.")
    parser.add_argument("--config", dest="config_path", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run HTTP and A2A transports.")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)

    app = sub.add_parser("app", help="Manage registered applications.")
    app_sub = app.add_subparsers(dest="app_command", required=True)
    install = app_sub.add_parser("install")
    install.add_argument("manifest", type=Path)
    sync = app_sub.add_parser(
        "sync-schemas",
        help="Development only: replace installed schema definitions without rotating credentials.",
    )
    sync.add_argument("manifest", type=Path)
    app_sub.add_parser("list")

    token = sub.add_parser("token", help="Manage application credentials.")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    token_sub.add_parser("init-cli", help="Create and save an all-access local CLI token.")
    create = token_sub.add_parser("create")
    create.add_argument("app_id")
    create.add_argument("--name", default="manual")
    create.add_argument("--scope", action="append", choices=sorted(VALID_SCOPES), required=True)
    rotate = token_sub.add_parser("rotate")
    rotate.add_argument("app_id")
    rotate.add_argument("--scope", action="append", choices=sorted(VALID_SCOPES), required=True)
    revoke = token_sub.add_parser("revoke")
    revoke.add_argument("app_id")
    revoke.add_argument("--name", default=None)

    schema = sub.add_parser("schema", help="Inspect installed schemas.")
    schema_sub = schema.add_subparsers(dest="schema_command", required=True)
    schema_list = schema_sub.add_parser("list")
    schema_list.add_argument("--app", dest="app_id", default=None)

    events = sub.add_parser("events", help="Inspect raw events.")
    events.add_argument("--token", default=None)
    events_sub = events.add_subparsers(dest="events_command", required=True)
    event_list = events_sub.add_parser("list")
    event_list.add_argument("--app", action="append", default=[])
    event_list.add_argument("--type", dest="event_types", action="append", default=[])
    event_list.add_argument("--family", dest="families", action="append", default=[])
    event_list.add_argument("--after", default=None)
    event_list.add_argument("--before", default=None)
    event_list.add_argument("--term", action="append", default=[])
    event_list.add_argument("--phrase", action="append", default=[])
    event_list.add_argument("--regex", action="append", default=[])
    event_list.add_argument("--limit", type=int, default=50)
    event_show = events_sub.add_parser("show")
    event_show.add_argument("event_id")

    emit = sub.add_parser("emit", help="Emit one event from a JSON file.")
    emit.add_argument("event_file", type=Path)
    emit.add_argument("--token", default=None)

    ask = sub.add_parser("ask", help="Ask a natural-language history question.")
    ask.add_argument("question")
    ask.add_argument("--token", default=None)

    doctor = sub.add_parser("doctor", help="Validate local configuration and storage.")
    doctor.add_argument("--live", action="store_true")

    config = sub.add_parser("config", help="Manage Historian configuration.")
    config_subparsers = config.add_subparsers(dest="config_command", required=True)
    config_init = config_subparsers.add_parser("init", help="Write the default config file.")
    config_init.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Config file path (default: ~/.config/historian/config.json)",
    )
    config_init.add_argument("--force", action="store_true", help="Overwrite an existing config file.")
    config_init.add_argument(
        "--print",
        action="store_true",
        help="Print the template to stdout instead of writing a file.",
    )
    config_subparsers.add_parser("path", help="Print the path Historian loads config from.")

    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output compact minified JSON (machine-readable) instead of indented JSON.",
    )
    return parser


_compact_json = False


def _print(payload: Any) -> None:
    if _compact_json:
        print(json.dumps(to_jsonable(payload), separators=(",", ":"), sort_keys=True))
    else:
        print(json.dumps(to_jsonable(payload), indent=2, sort_keys=True))


def _write_private_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, (token + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def _token(args: argparse.Namespace, settings: Settings) -> str:
    token = args.token or os.getenv("HISTORIAN_TOKEN", "")
    if token:
        return token
    path = settings.expanded_cli_token_path
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    raise HistorianError(
        "No CLI credential found. Run 'historian token init-cli', provide --token, or set HISTORIAN_TOKEN."
    )


def _try_client(settings: Settings, token: str) -> HistorianClient:
    return HistorianClient(
        settings.public_base_url,
        token,
        timeout_seconds=settings.request_timeout_seconds,
        verify_tls=settings.verify_tls,
    )


def main(argv: Sequence[str] | None = None) -> int:
    global _compact_json
    parser = build_parser()
    args = parser.parse_args(argv)
    _compact_json = args.as_json
    try:
        if args.command == "serve":
            import uvicorn

            context = build_app(args.config_path, clear_operational_log=True)
            uvicorn.run(
                create_http_app(context),
                host=args.host or context.settings.http_host,
                port=args.port or context.settings.http_port,
                log_level=context.settings.log_level.lower(),
            )
            return 0

        if args.command == "config" and args.config_command == "init":
            from .config import read_config_template, write_default_config

            if args.print:
                print(read_config_template(), end="")
                return 0

            path = write_default_config(args.path, force=args.force)
            print(f"Wrote config to {path}")
            return 0

        if args.command == "config" and args.config_command == "path":
            settings = Settings.load(args.config_path)
            print(settings.loaded_config_path or "(none — using built-in defaults)")
            return 0

        context = build_app(args.config_path)
        settings = context.settings

        if args.command == "app":
            if args.app_command == "install":
                manifest = load_manifest(args.manifest)
                token = context.store.install_app(manifest)
                _print(
                    {
                        "status": "ok",
                        "app_id": manifest.app_id,
                        "token": token,
                        "environment": f"HISTORIAN_TOKEN={token}",
                        "warning": "This token is shown once. Store it in the application's secret configuration.",
                    }
                )
            elif args.app_command == "sync-schemas":
                manifest = load_manifest(args.manifest)
                context.store.replace_app_schemas(manifest)
                _print(
                    {
                        "status": "ok",
                        "app_id": manifest.app_id,
                        "schemas": len(manifest.schemas),
                        "warning": (
                            "Schema definitions were replaced in place. Existing events were not migrated "
                            "or revalidated."
                        ),
                    }
                )
            else:
                _print({"status": "ok", "apps": context.store.list_apps()})
            return 0

        if args.command == "token":
            if args.token_command == "init-cli":
                context.store.revoke_tokens("historian", "cli")
                token = context.store.create_token(
                    "historian",
                    sorted(VALID_SCOPES),
                    "cli",
                )
                _write_private_token(settings.expanded_cli_token_path, token)
                _print(
                    {
                        "status": "ok",
                        "app_id": "historian",
                        "name": "cli",
                        "scopes": sorted(VALID_SCOPES),
                        "token_path": str(settings.expanded_cli_token_path),
                    }
                )
            elif args.token_command == "create":
                token = context.store.create_token(args.app_id, args.scope, args.name)
                _print({"status": "ok", "app_id": args.app_id, "name": args.name, "token": token})
            elif args.token_command == "rotate":
                context.store.revoke_tokens(args.app_id, "default")
                token = context.store.create_token(args.app_id, args.scope, "default")
                _print({"status": "ok", "app_id": args.app_id, "name": "default", "token": token})
            else:
                count = context.store.revoke_tokens(args.app_id, args.name)
                _print({"status": "ok", "revoked": count})
            return 0

        if args.command == "schema":
            _print({"status": "ok", "schemas": context.store.list_schemas(args.app_id)})
            return 0

        if args.command == "events":
            principal = context.store.authenticate(_token(args, settings))
            if args.events_command == "show":
                event = context.service.get_event(principal, args.event_id)
                _print({"status": "ok" if event else "not_found", "event": event})
            else:
                spec = SearchSpec(
                    apps=args.app,
                    event_types=args.event_types,
                    record_families=args.families,
                    occurred_after=args.after,
                    occurred_before=args.before,
                    required_terms=args.term,
                    exact_phrases=args.phrase,
                    regex_patterns=args.regex,
                    limit=args.limit,
                )
                _print({"status": "ok", "events": context.service.raw_search(principal, spec)})
            return 0

        if args.command == "emit":
            token = _token(args, settings)
            event = json.loads(args.event_file.read_text(encoding="utf-8"))
            try:
                payload = _try_client(settings, token).emit(event)
            except HistorianConnectionError:
                # Server unreachable (connection refused, timeout, DNS failure): fall
                # back to in-process execution. Non-transport errors (auth, validation,
                # conflict, 5xx) propagate to the caller as HistorianError.
                principal = context.store.authenticate(token)
                stored, duplicate = context.service.ingest(principal, event)
                payload = {"status": "ok", "duplicate": duplicate, "event": stored}
            _print(payload)
            return 0

        if args.command == "ask":
            token = _token(args, settings)
            try:
                payload = _try_client(settings, token).query(args.question)
            except HistorianConnectionError:
                principal = context.store.authenticate(token)
                payload = to_jsonable(context.service.query(principal, args.question))
            _print(payload)
            return 1 if payload.get("status") == "error" else 0

        if args.command == "doctor":
            payload: dict[str, Any] = {
                "status": "ok",
                "config": settings.sanitized(),
                "apps": len(context.store.list_apps()),
                "schemas": len(context.store.list_schemas()),
                "database_exists": settings.expanded_database_path.exists(),
                "cli_token": {
                    "path": str(settings.expanded_cli_token_path),
                    "exists": settings.expanded_cli_token_path.is_file(),
                },
                "debug": {
                    "enabled": settings.debug_enabled,
                    "operational_log": check_debug_path(settings.expanded_debug_log_path)
                    if settings.debug_enabled
                    else {"path": str(settings.expanded_debug_log_path), "writable": None, "error": None},
                    "resolver_log": check_debug_path(settings.expanded_resolver_debug_log_path)
                    if settings.debug_enabled
                    else {
                        "path": str(settings.expanded_resolver_debug_log_path),
                        "writable": None,
                        "error": None,
                    },
                },
            }
            if settings.debug_enabled and not (
                payload["debug"]["operational_log"]["writable"]
                and payload["debug"]["resolver_log"]["writable"]
            ):
                payload["status"] = "error"
            if args.live and settings.resolver_backend == "openai_compatible":
                try:
                    response = httpx.get(
                        settings.resolver_base_url.rstrip("/") + "/models",
                        timeout=min(settings.request_timeout_seconds, 10),
                        verify=settings.verify_tls,
                    )
                    payload["resolver_http_status"] = response.status_code
                except httpx.HTTPError as exc:
                    payload["status"] = "error"
                    payload["resolver_error"] = str(exc)
            _print(payload)
            return 0 if payload["status"] == "ok" else 2
    except (HistorianError, ConfigError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2 if isinstance(exc, (ConfigError, OSError)) else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
