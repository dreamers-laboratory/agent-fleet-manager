---
name: agent-fleet-manager
description: Operate an agentfleet fleet for large-scale periodic information gathering. Use when the user wants to monitor many sources on a schedule (prospect signals, competitor pages, regulatory updates, listings, datasets), act as a worker executing leased batches, review what changed or failed across the fleet, or export fleet activity as OpenTelemetry traces.
---

# Operating an agentfleet fleet

One SQLite file holds the whole fleet. All commands take `--db <path>`. Full mechanics are in [README.md](README.md); this file is the operating procedure.

## Register what to watch

```
python -m agentfleet.cli add-source --db fleet.db <name> <route> --cadence <seconds>
```

The route means whatever your worker decides: a URL for the built-in HTTP fetcher, a search query, an API endpoint, a natural-language instruction for an agent worker. Pick cadences honestly — a careers page is an hourly-to-daily source; a statute database is weekly. Do not register sources the user has no right to fetch, and respect robots.txt and rate expectations when the worker is a fetcher you control.

## Run a sweep

```
python -m agentfleet.cli sweep --db fleet.db --scopes ./scopes
```

Queues everything due, leases batches, fetches over HTTP, reconciles. Nothing due means the sweep is a cheap no-op, so running it on a timer is fine.

## Act as a worker yourself

When routes need judgment (log in, interpret a page, summarize a diff), be the worker instead of the HTTP fetcher:

1. In Python: `manifest = agentfleet.lease(conn, scope_root)`.
2. For each batch in the manifest, for each action: perform the check however the route demands. Compute sha256 and byte count of the content you retrieved.
3. Write `{action_id}.json` into that batch's `write_scope` with: `action_id`, `source_id`, `status` ("ok" or an error string), `sha256`, `bytes`, `started_at`, `finished_at` (UTC, `%Y-%m-%dT%H:%M:%SZ`), `elapsed_ms`. Optionally write the payload beside it.
4. Write nothing anywhere else. The write scope is the entire surface a worker may touch.
5. `agentfleet.reconcile(conn, batch_id)`.

Skipping a result file marks that action failed; the engine requeues and backs off on its own. Never edit the source, action, or observation tables directly — reconcile is the only door into canonical state.

## Optional Jev evaluation

Only with authorized data sharing: use `jev-evaluate <request.json>` for explicit state/questions, or `sweep --jev-questions <questions.json>` to send fetched UTF-8 text to the official TypeSafe API. Set `TYPESAFE_API_KEY` in the environment; never store it in routes, inputs, receipts or Git. See [README.md](README.md#optional-jev-classification) for examples. Preserve ambiguous cases for review; a model answer is not an automatic approval/rejection. API failures are failed checks, not negative findings. Answers, usage and phase timings live in receipts; source hashes remain unchanged.

## Review the fleet

```
python -m agentfleet.cli status --db fleet.db   # per-source: next due, checks, changes, error streak
python -m agentfleet.cli stats  --db fleet.db   # change %, error %, median/p95 latency, fleet totals
```

When reporting to the user, lead with what changed (the point of the fleet), then what is failing and how far it has backed off. A source with a growing error streak needs a route fix or removal; say so rather than letting it back off forever.

## Export telemetry

```
python -m agentfleet.cli export-otel --db fleet.db --endpoint <otlp-http-collector>
```

Run → trace, batch → parent span, action → child span. Use it when the user has Grafana or any OTLP collector; omit `--endpoint` to inspect JSON on stdout first.
