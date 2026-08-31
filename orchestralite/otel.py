"""Export a run's history as OpenTelemetry traces.

The tables are already span-shaped: a run has batches, a batch has actions,
and everything carries start/end timestamps. This module maps run -> trace,
batch -> parent span, action -> child span, and emits OTLP/HTTP JSON either
to stdout or straight to a collector's /v1/traces endpoint. No dependencies.
"""

import hashlib
import json
import urllib.request
from datetime import datetime, timezone


def _nanos(iso: str) -> int:
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


def _id(seed: str, nbytes: int) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[: nbytes * 2]


def _attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def build_payload(conn, run_id: str = None) -> dict:
    where, params = ("WHERE run_id=?", (run_id,)) if run_id else ("", ())
    batches = conn.execute(f"SELECT * FROM batch {where}", params).fetchall()
    spans = []
    for b in batches:
        trace_id = _id(b["run_id"], 16)
        batch_span_id = _id(b["batch_id"], 8)
        actions = conn.execute(
            """SELECT a.*, s.name, o.sha256, o.bytes, o.elapsed_ms, o.changed, o.status
               FROM action a JOIN source s USING (source_id)
               LEFT JOIN observation o USING (action_id)
               WHERE a.batch_id=? AND a.started_at IS NOT NULL""",
            (b["batch_id"],),
        ).fetchall()
        end = b["reconciled_at"] or b["leased_at"]
        spans.append({
            "traceId": trace_id,
            "spanId": batch_span_id,
            "name": f"batch {b['batch_id'].rsplit('-', 1)[-1]}",
            "kind": 1,
            "startTimeUnixNano": str(_nanos(b["leased_at"])),
            "endTimeUnixNano": str(_nanos(end)),
            "attributes": [_attr("batch.id", b["batch_id"]), _attr("run.id", b["run_id"])],
        })
        for a in actions:
            ok = a["exit_status"] == "ok"
            attrs = [_attr("source.name", a["name"]), _attr("action.attempt", a["attempt"])]
            for key in ("sha256", "bytes", "changed", "status"):
                if a[key] is not None:
                    attrs.append(_attr(f"observation.{key}", a[key]))
            spans.append({
                "traceId": trace_id,
                "spanId": _id(f"action-{a['action_id']}-{a['attempt']}", 8),
                "parentSpanId": batch_span_id,
                "name": f"fetch {a['name']}",
                "kind": 3,
                "startTimeUnixNano": str(_nanos(a["started_at"])),
                "endTimeUnixNano": str(_nanos(a["finished_at"] or a["started_at"])),
                "attributes": attrs,
                "status": {"code": 1 if ok else 2},
            })
    return {
        "resourceSpans": [{
            "resource": {"attributes": [_attr("service.name", "orchestra-lite")]},
            "scopeSpans": [{"scope": {"name": "orchestralite"}, "spans": spans}],
        }]
    }


def export(conn, run_id: str = None, endpoint: str = None) -> int:
    payload = build_payload(conn, run_id)
    n_spans = len(payload["resourceSpans"][0]["scopeSpans"][0]["spans"])
    if endpoint:
        req = urllib.request.Request(
            endpoint.rstrip("/") + "/v1/traces",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:
            resp.read()
    else:
        print(json.dumps(payload, indent=2))
    return n_spans
