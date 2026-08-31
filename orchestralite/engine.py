"""The engine: queue due work, lease it to batches, reconcile results.

Rules the whole design hangs on:
1. Workers never write canonical state. Each batch gets a write-scope
   directory; the worker drops result files there and the parent folds them
   in during reconcile.
2. Every canonical write lands in change_log with an actor and a reason.
3. Change detection is a content hash, so re-running anything is safe.
"""

import hashlib
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone

SCHEMA = os.path.join(os.path.dirname(__file__), "schema.sql")
MAX_ATTEMPTS = 3
MAX_BACKOFF_MULTIPLIER = 16


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    with open(SCHEMA) as f:
        conn.executescript(f.read())
    return conn


def log_change(conn, actor, entity, entity_id, reason, field=None, old=None, new=None):
    conn.execute(
        "INSERT INTO change_log (actor, entity, entity_id, field, old_value, new_value, reason)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (actor, entity, str(entity_id), field, old, new, reason),
    )


def add_source(conn, name: str, route: str, cadence_seconds: int = 86400, actor: str = "cli"):
    cur = conn.execute(
        "INSERT INTO source (name, route, cadence_seconds) VALUES (?, ?, ?)",
        (name, route, cadence_seconds),
    )
    log_change(conn, actor, "source", cur.lastrowid, f"registered source {name}")
    conn.commit()
    return cur.lastrowid


def queue_due(conn, actor: str = "scheduler") -> int:
    """One QUEUED action per due source that has no action in flight."""
    rows = conn.execute(
        """SELECT s.source_id FROM source s
           WHERE s.enabled = 1 AND s.next_due_at <= ?
             AND NOT EXISTS (SELECT 1 FROM action a WHERE a.source_id = s.source_id
                             AND a.state IN ('QUEUED', 'LEASED', 'RUNNING'))""",
        (now_iso(),),
    ).fetchall()
    for row in rows:
        cur = conn.execute("INSERT INTO action (source_id) VALUES (?)", (row["source_id"],))
        log_change(conn, actor, "action", cur.lastrowid, "queued: source due")
    conn.commit()
    return len(rows)


def lease(conn, scope_root: str, batch_size: int = 10, actor: str = "scheduler"):
    """Partition QUEUED actions into batches. Returns the manifest: one dict per
    batch with its write scope and the actions a worker should execute."""
    run_id = f"run-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    queued = conn.execute(
        """SELECT a.action_id, a.source_id, s.name, s.route FROM action a
           JOIN source s USING (source_id) WHERE a.state = 'QUEUED' ORDER BY a.action_id"""
    ).fetchall()
    manifest = []
    for i in range(0, len(queued), batch_size):
        chunk = queued[i : i + batch_size]
        batch_id = f"{run_id}-batch-{i // batch_size:04d}"
        write_scope = os.path.join(scope_root, run_id, batch_id)
        os.makedirs(write_scope, exist_ok=True)
        conn.execute(
            "INSERT INTO batch (batch_id, run_id, write_scope) VALUES (?, ?, ?)",
            (batch_id, run_id, write_scope),
        )
        for a in chunk:
            conn.execute(
                "UPDATE action SET state='LEASED', batch_id=?, attempt=attempt+1 WHERE action_id=?",
                (batch_id, a["action_id"]),
            )
        log_change(conn, actor, "batch", batch_id, f"leased {len(chunk)} actions")
        manifest.append({
            "batch_id": batch_id,
            "run_id": run_id,
            "write_scope": write_scope,
            "actions": [dict(a) for a in chunk],
        })
    conn.commit()
    return manifest


def run_batch(conn, batch: dict, executor):
    """In-process worker. `executor(route) -> (status_str, content_bytes)`.
    Only writes inside the batch's write scope, like an external worker would."""
    for a in batch["actions"]:
        conn.execute(
            "UPDATE action SET state='RUNNING', started_at=? WHERE action_id=?",
            (now_iso(), a["action_id"]),
        )
        conn.commit()
        started = now_iso()
        t0 = time.monotonic()
        try:
            status, content = executor(a["route"])
        except Exception as exc:  # a worker failure is a result too
            status, content = f"error: {type(exc).__name__}: {exc}", b""
        elapsed_ms = (time.monotonic() - t0) * 1000
        result = {
            "action_id": a["action_id"],
            "source_id": a["source_id"],
            "status": status,
            "sha256": hashlib.sha256(content).hexdigest() if content else None,
            "bytes": len(content),
            "started_at": started,
            "finished_at": now_iso(),
            "elapsed_ms": round(elapsed_ms, 3),
        }
        path = os.path.join(batch["write_scope"], f"{a['action_id']}.json")
        with open(path, "w") as f:
            json.dump(result, f)
        if content:
            with open(os.path.join(batch["write_scope"], f"{a['action_id']}.payload"), "wb") as f:
                f.write(content)


def reconcile(conn, batch_id: str, actor: str = "reconciler") -> dict:
    """Fold a batch's write scope back into canonical state."""
    batch = conn.execute("SELECT * FROM batch WHERE batch_id=?", (batch_id,)).fetchone()
    counts = {"done": 0, "failed": 0, "requeued": 0, "changed": 0}
    for a in conn.execute("SELECT * FROM action WHERE batch_id=?", (batch_id,)).fetchall():
        path = os.path.join(batch["write_scope"], f"{a['action_id']}.json")
        result = None
        if os.path.exists(path):
            with open(path) as f:
                result = json.load(f)
        ok = bool(result) and result["status"] == "ok"

        source = conn.execute(
            "SELECT * FROM source WHERE source_id=?", (a["source_id"],)
        ).fetchone()
        if result:
            changed = int(bool(result["sha256"]) and result["sha256"] != source["last_sha256"])
            counts["changed"] += changed
            conn.execute(
                """INSERT INTO observation (action_id, source_id, sha256, status, bytes,
                   started_at, finished_at, elapsed_ms, receipt_path, changed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (a["action_id"], a["source_id"], result["sha256"], result["status"],
                 result["bytes"], result["started_at"], result["finished_at"],
                 result["elapsed_ms"], path, changed),
            )

        if ok:
            due = datetime.now(timezone.utc) + timedelta(seconds=source["cadence_seconds"])
            conn.execute(
                """UPDATE source SET last_sha256=?, last_checked_at=?, error_streak=0,
                   next_due_at=? WHERE source_id=?""",
                (result["sha256"], result["finished_at"],
                 due.strftime("%Y-%m-%dT%H:%M:%SZ"), a["source_id"]),
            )
            conn.execute(
                "UPDATE action SET state='DONE', started_at=?, finished_at=?, exit_status=? WHERE action_id=?",
                (result["started_at"], result["finished_at"], "ok", a["action_id"]),
            )
            counts["done"] += 1
        else:
            streak = source["error_streak"] + 1
            backoff = source["cadence_seconds"] * min(2 ** streak, MAX_BACKOFF_MULTIPLIER)
            due = datetime.now(timezone.utc) + timedelta(seconds=backoff)
            conn.execute(
                "UPDATE source SET error_streak=?, last_checked_at=?, next_due_at=? WHERE source_id=?",
                (streak, now_iso(), due.strftime("%Y-%m-%dT%H:%M:%SZ"), a["source_id"]),
            )
            exit_status = result["status"] if result else "no result file"
            if a["attempt"] < MAX_ATTEMPTS:
                conn.execute(
                    "UPDATE action SET state='QUEUED', batch_id=NULL, exit_status=? WHERE action_id=?",
                    (exit_status, a["action_id"]),
                )
                counts["requeued"] += 1
            else:
                conn.execute(
                    "UPDATE action SET state='FAILED', finished_at=?, exit_status=? WHERE action_id=?",
                    (now_iso(), exit_status, a["action_id"]),
                )
                counts["failed"] += 1
            log_change(conn, actor, "source", a["source_id"],
                       f"error streak {streak}, backing off {backoff}s",
                       field="next_due_at", new=due.strftime("%Y-%m-%dT%H:%M:%SZ"))

    conn.execute("UPDATE batch SET reconciled_at=? WHERE batch_id=?", (now_iso(), batch_id))
    log_change(conn, actor, "batch", batch_id, f"reconciled: {counts}")
    conn.commit()
    return counts
