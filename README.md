# orchestra-lite

A SQLite-backed state machine for checking things periodically at fleet scale: web pages, API endpoints, document repositories, anything a worker can fetch. It schedules what is due, leases work to workers in isolated batches, detects change by content hash, backs off on errors, and can export every run as OpenTelemetry traces.

We extracted the pattern from the system that runs our government-procurement intake, where it tracks a few thousand sources with a mixed fleet of LLM agents and plain fetchers. This is a clean rewrite of the part that generalizes. Python standard library only.

## The model

Five tables (see [orchestralite/schema.sql](orchestralite/schema.sql)):

- **source** — a thing to check: a route, a cadence, a `next_due_at`, an error streak
- **action** — one queued unit of work; states `QUEUED → LEASED → RUNNING → DONE | FAILED`, with requeue while attempts remain
- **batch** — a lease of actions to one worker, with a write-scope directory only that worker touches
- **observation** — one check's result: content sha256, bytes, timings, receipt path, a `changed` flag
- **change_log** — every canonical write, stamped with an actor and a reason

Three rules carry the design. Workers never write canonical state; they drop result files in their write scope and the parent folds them in during reconcile, so a crashed or malicious worker can corrupt at most its own scratch directory. Change detection is a content hash, so any run can be repeated safely. Every canonical write is attributed, which matters once several agents share one database.

Scheduling: a successful check sets `next_due_at = now + cadence`. A failure sets `next_due_at = now + cadence × min(2^streak, 16)` and requeues up to 3 attempts.

## Quickstart

```
python -m orchestralite.cli add-source --db fleet.db docs https://example.com/docs --cadence 3600
python -m orchestralite.cli sweep --db fleet.db
python -m orchestralite.cli status --db fleet.db
```

`sweep` queues due sources, leases them into batches, fetches each route over HTTP, and reconciles. `python examples/demo.py` runs the whole loop on local files with no network. Tests: `python tests/test_engine.py` (7 tests, state transitions, backoff, scope isolation).

## Bring your own workers

The built-in executor is a function `route -> (status, content_bytes)`. For an external fleet (LLM agents, containers, other machines), call `lease()` to get a manifest, hand each batch to a worker, and have the worker write `{action_id}.json` result files into its `write_scope`:

```json
{"action_id": 7, "source_id": 3, "status": "ok", "sha256": "…", "bytes": 4096,
 "started_at": "2026-08-31T19:50:21Z", "finished_at": "2026-08-31T19:50:22Z", "elapsed_ms": 812.4}
```

Then `reconcile(conn, batch_id)`. The engine treats a missing result file as a failure, so dead workers resolve themselves.

## Watch it in Grafana

The tables are span-shaped by construction, so telemetry is an export:

```
python -m orchestralite.cli export-otel --db fleet.db --endpoint http://localhost:4318
```

maps run → trace, batch → parent span, action → child span (with source name, hash, bytes, attempt, and changed-flag attributes) and posts OTLP/HTTP JSON to any collector, including a Grafana Cloud OTLP gateway. Omit `--endpoint` to inspect the JSON on stdout.

Apache-2.0. Built by [Dreamers Inc](https://thedreamers.us).
