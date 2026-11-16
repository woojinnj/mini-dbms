import argparse
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading

from .engine import Database
from .errors import DBError
from .server import PoolHTTPServer
from .sql import parse


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def emit(result, output):
    if output == "json":
        print(json.dumps(result, ensure_ascii=False))
        return
    if result["command"] == "CREATE TABLE":
        print(f"CREATE TABLE {result['table']}")
    elif result["command"] == "INSERT":
        print(f"INSERT {result['affected_rows']} (IDs {result['first_id']}..{result['last_id']})")
    elif result["command"] == "EXPLAIN":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        columns = result["columns"]
        # JSON quoting keeps embedded newlines/control characters from breaking the table.
        values = [["NULL" if row[column] is None else json.dumps(row[column], ensure_ascii=False)
                   for column in columns] for row in result["rows"]]
        widths = [max(len(column), *(len(row[i]) for row in values)) if values else len(column)
                  for i, column in enumerate(columns)]
        print(" | ".join(column.ljust(width) for column, width in zip(columns, widths)))
        print("-+-".join("-" * width for width in widths))
        for row in values:
            print(" | ".join(value.ljust(width) for value, width in zip(row, widths)))
        print(f"{result['row_count']} row(s); {result['plan']}; examined={result['rows_examined']}; "
              f"{result['elapsed_ms']:.3f} ms" + ("; TRUNCATED (use WHERE id > ... LIMIT ...)" if result["truncated"] else ""))


def main(argv=None):
    parser = argparse.ArgumentParser(description="MiniDB: SQL → B+ tree → files → threaded HTTP")
    parser.add_argument("--data-dir", default="data", help="database directory (default: data)")
    parser.add_argument("--schema", type=Path, help="schema JSON for a new database")
    parser.add_argument("--no-fsync", action="store_true", help="disable durable commits for experiments")
    parser.add_argument("--max-result-rows", type=positive, default=10_000)
    commands = parser.add_subparsers(dest="command", required=True)
    execute = commands.add_parser("exec", help="execute a SQL file, SQL text or stdin")
    source = execute.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path)
    source.add_argument("--sql")
    source.add_argument("--stdin", action="store_true")
    execute.add_argument("--format", choices=("table", "json"), default="table")
    serve = commands.add_parser("serve", help="start the bounded thread-pool HTTP server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--workers", type=positive, default=8)
    serve.add_argument("--queue-size", type=int, default=32)
    serve.add_argument("--request-timeout", type=positive, default=10)
    args = parser.parse_args(argv)
    try:
        schema = json.loads(args.schema.read_text(encoding="utf-8")) if args.schema else None
        with Database(args.data_dir, schema, not args.no_fsync, args.max_result_rows) as database:
            recovered = sum(table.recovered_bytes for table in database.tables.values())
            if recovered:
                print(f"Recovered storage: removed {recovered} incomplete trailing bytes", file=sys.stderr)
            if args.command == "exec":
                sql = args.file.read_text(encoding="utf-8") if args.file else (sys.stdin.read() if args.stdin else args.sql)
                for statement in parse(sql):
                    emit(database.execute_statement(statement), args.format)
            else:
                logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
                with PoolHTTPServer((args.host, args.port), database, args.workers, args.queue_size,
                                    args.request_timeout, os.environ.get("MINIDB_API_TOKEN")) as server:
                    # HTTPServer.shutdown must be called from a different thread.
                    def shutdown(signum, frame):
                        threading.Thread(target=server.shutdown, daemon=True).start()

                    previous = {sig: signal.signal(sig, shutdown) for sig in (signal.SIGINT, signal.SIGTERM)}
                    try:
                        print(f"MiniDB listening on http://{args.host}:{server.server_port} "
                              f"(workers={args.workers}, queue={args.queue_size}, durable={not args.no_fsync})", flush=True)
                        server.serve_forever(poll_interval=0.1)
                    finally:
                        for sig, handler in previous.items():
                            signal.signal(sig, handler)
    except (DBError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
