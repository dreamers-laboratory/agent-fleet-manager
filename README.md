# agent-fleet-manager

A general-purpose engine for large-scale, repeated information gathering by a fleet of workers — plain fetchers, scrapers, or LLM agents. You register things to check and how often; the engine schedules what is due, leases work out in isolated batches, records what came back, detects change by content hash, backs off on failures, and keeps an auditable ledger of every write. One SQLite file is the entire system. Python standard library only.

It came out of a production system where a mixed fleet of LLM agents and plain fetchers tracks a few thousand sources on daily and weekly cadences. This is a clean rewrite of the part that generalizes: the bookkeeping. The engine never interprets what a source *is* — the route field means nothing to it and everything to your worker — which is why the same engine can run very different fleets.

## What people use this shape of system for

- **Sales prospecting.** Register target companies' careers pages, press pages, and funding announcements. A changed hash on a careers page is a hiring signal; your worker (or an LLM agent reading the diff) turns it into a lead with a timestamp and a receipt.
- **Competitor and price watch.** Pricing pages, feature pages, changelogs, app-store listings — checked on cadence, with `changed` flags you can alert on and history you can chart.
- **Regulatory and compliance tracking.** Agency pages, statute databases, published guidance. The hash ledger doubles as evidence of when a change appeared and when you saw it.
- **Research corpus upkeep.** Datasets, preprint feeds, documentation sets that must stay current. Re-ingest is safe because everything is idempotent by hash. Research-tool example: [alphaXiv](https://x.com/askalphaxiv/status/2094797932994113805).
- **Marketplace and listing monitoring.** Job boards, real-estate listings, auction lots — anything where being first to notice a change is the point.

In every case the engine's contribution is the same: which of your thousands of sources are due right now, what actually changed, what is failing and backing off, and proof of all of it.

## The model

Five tables (see [agentfleet/schema.sql](agentfleet/schema.sql)):

- **source** — a thing to check: a route, a cadence, a `next_due_at`, an error streak
- **action** — one unit of work; states `QUEUED → LEASED → RUNNING → DONE | FAILED`, requeued while attempts remain
- **batch** — a lease of actions to one worker, with a write-scope directory only that worker touches
- **observation** — one check's result: content sha256, bytes, timings, receipt path, a `changed` flag
- **change_log** — every canonical write, stamped with an actor and a reason

Three rules carry the design. Workers never write canonical state; they drop result files in their write scope and the parent folds them in during reconcile, so a crashed or misbehaving worker can corrupt at most its own scratch directory. Change detection is a content hash, so any run can be repeated safely. Every canonical write is attributed, which matters once several agents share one database.

Scheduling: success sets `next_due_at = now + cadence`; failure sets `next_due_at = now + cadence × min(2^streak, 16)` and requeues up to 3 attempts.

## Quickstart

```
python -m agentfleet.cli add-source --db fleet.db acme-careers https://example.com/careers --cadence 3600
python -m agentfleet.cli sweep --db fleet.db
python -m agentfleet.cli status --db fleet.db
python -m agentfleet.cli stats --db fleet.db
```

`sweep` queues due sources, leases batches, fetches each route over HTTP, and reconciles. `stats` prints per-source change rate, error rate, median and p95 latency, and fleet totals. `python examples/demo.py` runs the whole loop on local files with no network. Tests: `python tests/test_engine.py`.

## Bring your own workers

The built-in executor is a function `route -> (status, content_bytes)`. For an external fleet (LLM agents, containers, other machines), call `lease()` to get a manifest, hand each batch to a worker, and have the worker write `{action_id}.json` result files into its `write_scope`:

```json
{"action_id": 7, "source_id": 3, "status": "ok", "sha256": "…", "bytes": 4096,
 "started_at": "2026-08-31T19:50:21Z", "finished_at": "2026-08-31T19:50:22Z", "elapsed_ms": 812.4}
```

Then `reconcile(conn, batch_id)`. The engine treats a missing result file as a failure, so dead workers resolve themselves. An LLM agent makes a good worker precisely because the contract is this narrow: read the manifest, do the check however you judge best, write result files, touch nothing else. [SKILL.md](SKILL.md) teaches a coding agent to operate the whole loop.

## Watch it in Grafana

The tables are span-shaped by construction, so telemetry is an export:

```
python -m agentfleet.cli export-otel --db fleet.db --endpoint http://localhost:4318
```

maps run → trace, batch → parent span, action → child span (with source name, hash, bytes, attempt, and changed-flag attributes) and posts OTLP/HTTP JSON to any collector, including a Grafana Cloud OTLP gateway. Omit `--endpoint` to inspect the JSON on stdout.

Apache-2.0. Built by [Dreamers Inc](https://thedreamers.us).
