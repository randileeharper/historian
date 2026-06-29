# Debugging

Enable unified debugging in `config.json`:

```json
{
  "debug_enabled": true,
  "debug_log_path": "~/.local/share/historian/debug.log",
  "resolver_debug_log_path": "~/.local/share/historian/resolver.log",
  "log_level": "INFO"
}
```

`debug_enabled` controls two owner-readable files:

- `debug_log_path` is the operational log. `historian serve` clears it on startup, then records sanitized startup/storage information, authentication failures, HTTP and A2A lifecycle, event IDs/types, batch counts, search controls/result counts, model-call timing, query status, and exception traces. It does not include bearer tokens, API keys, authorization headers, or complete event payloads.
- `resolver_debug_log_path` contains only the newest top-level NLP query. A new query overwrites the file. It contains the compact search-planning call and, when records match, the answer-synthesis call with labeled `SYSTEM PROMPT`, `USER MESSAGE`, `RESPONSE`, optional `REASONING`, and `ERROR` sections, followed by the final query status.

The resolver transcript can contain conversation text and event evidence. Both debug files are created with owner-only permissions. Do not publish them.

`resolver_max_retries` controls correction retries for local-model HTTP failures, malformed JSON, schema violations, invalid timestamps, and invented applications or record types. The default is `3`, in addition to the initial attempt.

`log_level` controls console and Uvicorn verbosity independently. Debug mode always writes detailed `DEBUG` records to the operational file even when console logging remains `INFO` or `WARNING`.

Useful checks:

```console
uv run historian doctor --live
tail -f ~/.local/share/historian/debug.log
less ~/.local/share/historian/resolver.log
```

`doctor` reports whether debug mode is enabled and whether both configured paths are writable.

## Known Limitations

- The resolver transcript retains only the most recent top-level query. Under concurrent queries, earlier queries may have incomplete or missing transcript entries because a new query overwrites the file.
- Time-bound inference from question text recognizes only the word "today" (midnight to now). Phrases such as "this morning", "yesterday", or "this week" do not trigger implied bounds; the local-model planner is the primary mechanism for timestamp selection.
