"""Command line for fleetsweep.

    python -m fleetsweep.cli init --db fleet.db
    python -m fleetsweep.cli add-source --db fleet.db docs https://example.com/docs --cadence 3600
    python -m fleetsweep.cli sweep --db fleet.db --scopes ./scopes
    python -m fleetsweep.cli status --db fleet.db
    python -m fleetsweep.cli export-otel --db fleet.db [--endpoint http://localhost:4318]
"""

import argparse
import urllib.request

from . import engine, otel


def http_executor(route: str):
    with urllib.request.urlopen(route, timeout=30) as resp:
        return "ok", resp.read()


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default="fleet.db")
    parser = argparse.ArgumentParser(prog="fleetsweep")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", parents=[common])
    p = sub.add_parser("add-source", parents=[common])
    p.add_argument("name")
    p.add_argument("route")
    p.add_argument("--cadence", type=int, default=86400)
    p = sub.add_parser("sweep", parents=[common])
    p.add_argument("--scopes", default="./scopes")
    p.add_argument("--batch-size", type=int, default=10)
    sub.add_parser("status", parents=[common])
    sub.add_parser("stats", parents=[common])
    p = sub.add_parser("export-otel", parents=[common])
    p.add_argument("--run")
    p.add_argument("--endpoint")

    args = parser.parse_args()
    conn = engine.connect(args.db)

    if args.cmd == "init":
        print(f"initialized {args.db}")
    elif args.cmd == "add-source":
        engine.add_source(conn, args.name, args.route, args.cadence)
        print(f"added {args.name} (every {args.cadence}s)")
    elif args.cmd == "sweep":
        queued = engine.queue_due(conn)
        manifest = engine.lease(conn, args.scopes, args.batch_size)
        print(f"queued {queued}, leased {len(manifest)} batch(es)")
        for batch in manifest:
            engine.run_batch(conn, batch, http_executor)
            counts = engine.reconcile(conn, batch["batch_id"])
            print(f"  {batch['batch_id']}: {counts}")
    elif args.cmd == "status":
        for row in conn.execute(
            """SELECT s.name, s.next_due_at, s.error_streak,
                      (SELECT count(*) FROM observation o WHERE o.source_id = s.source_id) checks,
                      (SELECT sum(changed) FROM observation o WHERE o.source_id = s.source_id) changes
               FROM source s ORDER BY s.next_due_at"""
        ):
            print(f"  {row['name']:<30} next {row['next_due_at']}  "
                  f"checks {row['checks'] or 0}  changes {row['changes'] or 0}  "
                  f"errors {row['error_streak']}")
    elif args.cmd == "stats":
        import math
        import statistics

        rows = conn.execute(
            """SELECT s.name, o.elapsed_ms, o.changed, o.status, o.bytes
               FROM observation o JOIN source s USING (source_id)"""
        ).fetchall()
        if not rows:
            print("no observations yet")
            return
        by_source = {}
        for r in rows:
            by_source.setdefault(r["name"], []).append(r)
        print(f"{'source':<30} {'checks':>6} {'chg%':>6} {'err%':>6} "
              f"{'ms med':>8} {'ms p95':>8} {'bytes':>10}")
        for name, obs in sorted(by_source.items()):
            lat = sorted(o["elapsed_ms"] for o in obs if o["elapsed_ms"] is not None)
            errs = sum(1 for o in obs if o["status"] != "ok")
            chg = sum(o["changed"] or 0 for o in obs)
            p95 = lat[min(len(lat) - 1, math.ceil(len(lat) * 0.95) - 1)] if lat else 0
            med = statistics.median(lat) if lat else 0
            print(f"{name:<30} {len(obs):>6} {100 * chg / len(obs):>5.1f}% "
                  f"{100 * errs / len(obs):>5.1f}% {med:>8.1f} {p95:>8.1f} "
                  f"{sum(o['bytes'] or 0 for o in obs):>10}")
        lat_all = [r["elapsed_ms"] for r in rows if r["elapsed_ms"] is not None]
        if lat_all:
            print(f"\nfleet: {len(rows)} checks over {len(by_source)} sources, "
                  f"median {statistics.median(lat_all):.1f}ms, mean {statistics.mean(lat_all):.1f}ms")
    elif args.cmd == "export-otel":
        n = otel.export(conn, run_id=args.run, endpoint=args.endpoint)
        dest = args.endpoint or "stdout"
        print(f"exported {n} spans to {dest}")


if __name__ == "__main__":
    main()
