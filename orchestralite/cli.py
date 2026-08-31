"""Command line for orchestra-lite.

    python -m orchestralite.cli init --db fleet.db
    python -m orchestralite.cli add-source --db fleet.db docs https://example.com/docs --cadence 3600
    python -m orchestralite.cli sweep --db fleet.db --scopes ./scopes
    python -m orchestralite.cli status --db fleet.db
    python -m orchestralite.cli export-otel --db fleet.db [--endpoint http://localhost:4318]
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
    parser = argparse.ArgumentParser(prog="orchestralite")
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
    elif args.cmd == "export-otel":
        n = otel.export(conn, run_id=args.run, endpoint=args.endpoint)
        dest = args.endpoint or "stdout"
        print(f"exported {n} spans to {dest}")


if __name__ == "__main__":
    main()
