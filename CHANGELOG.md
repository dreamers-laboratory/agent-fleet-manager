# Changelog

## 0.1.0 — 2026-10-01

First tagged release, including the existing scheduling, isolated batch, reconciliation, statistics and OpenTelemetry engine.

- Optional standard-library TypeSafe/Jev client for `choice`, `noul` and `score` questions, using the official API directly.
- `jev-evaluate` CLI command for explicit JSON inputs, without opening the fleet database.
- Opt-in `sweep --jev-questions` evaluation: resolved model, answers, token usage and separate fetch/evaluation timings in result receipts. Source hashes still describe fetched content, not model output.
- Bounded requests, timeouts, no credential-bearing redirects, redacted errors and strict response validation. No hidden retries or gateway fallback.
- Offline regression tests; existing plain HTTP sweeps require neither a key nor a new dependency.
