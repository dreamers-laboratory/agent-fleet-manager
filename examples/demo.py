"""End-to-end demo, no network needed.

Registers three local files as sources, sweeps them, edits one, sweeps again,
and prints the fleet status plus a count of exported OTel spans. Run:

    python examples/demo.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentfleet import engine, otel  # noqa: E402


def file_executor(route):
    with open(route, "rb") as f:
        return "ok", f.read()


def sweep(conn, scopes):
    engine.queue_due(conn)
    for batch in engine.lease(conn, scopes, batch_size=2):
        engine.run_batch(conn, batch, file_executor)
        counts = engine.reconcile(conn, batch["batch_id"])
        print(f"  {batch['batch_id']}: {counts}")


def main():
    tmp = tempfile.mkdtemp(prefix="agentfleet-demo-")
    conn = engine.connect(os.path.join(tmp, "fleet.db"))
    scopes = os.path.join(tmp, "scopes")

    paths = {}
    for name in ("statutes", "budget", "minutes"):
        paths[name] = os.path.join(tmp, f"{name}.html")
        with open(paths[name], "w") as f:
            f.write(f"<html>{name} v1</html>")
        engine.add_source(conn, name, paths[name], cadence_seconds=0)

    print("first sweep (everything is new):")
    sweep(conn, scopes)

    with open(paths["budget"], "w") as f:
        f.write("<html>budget v2 — amended</html>")

    print("second sweep (only budget changed):")
    sweep(conn, scopes)

    print("\nfleet status:")
    for row in conn.execute(
        """SELECT s.name, count(o.observation_id) checks, sum(o.changed) changes
           FROM source s LEFT JOIN observation o USING (source_id) GROUP BY s.name"""
    ):
        print(f"  {row['name']:<10} checks {row['checks']}  changes {row['changes']}")

    payload = otel.build_payload(conn)
    n = len(payload["resourceSpans"][0]["scopeSpans"][0]["spans"])
    print(f"\notel export: {n} spans ready for a collector "
          f"(pipe to Grafana with export-otel --endpoint http://localhost:4318)")


if __name__ == "__main__":
    main()
