#!/usr/bin/env python3
"""Start the real CLI server, send concurrent HTTP requests, verify persisted data."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import platform
import re
import selectors
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from minidb import Database


def run(pairs, clients, workers):
    with tempfile.TemporaryDirectory(prefix="minidb-api-") as directory:
        environment = dict(os.environ)
        environment.pop("MINIDB_API_TOKEN", None)
        with open(Path(directory) / "server.log", "w+") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "minidb", "--data-dir", directory, "serve", "--port", "0",
                 "--workers", str(workers), "--queue-size", str(max(32, clients * 2))],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=log, text=True, env=environment)
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    if not selector.select(timeout=15):
                        raise RuntimeError("Server startup timed out")
                ready = process.stdout.readline()
                match = re.search(r"http://127\.0\.0\.1:(\d+)", ready)
                if not match:
                    log.seek(0)
                    raise RuntimeError(f"Server failed: {ready} {log.read()}")
                port = int(match[1])

                def request(sql=None, path="/query"):
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
                    started = time.perf_counter_ns()
                    try:
                        if sql is None:
                            connection.request("GET", path)
                        else:
                            body = json.dumps({"sql": sql}).encode()
                            connection.request("POST", path, body, {"Content-Type": "application/json"})
                        response = connection.getresponse()
                        result = json.loads(response.read())
                        if response.status != 200:
                            raise RuntimeError(f"HTTP {response.status}: {result}")
                        return result, (time.perf_counter_ns() - started) / 1_000_000
                    finally:
                        connection.close()

                def pair(i):
                    insert, insert_ms = request(f"INSERT INTO users VALUES ('client_{i}', {i}, NULL)")
                    assigned = insert["result"]["first_id"]
                    selected, select_ms = request(f"SELECT * FROM users WHERE id = {assigned}")
                    assert selected["result"]["rows"] == [{"id": assigned, "name": f"client_{i}", "age": i, "email": None}]
                    assert selected["result"]["rows_examined"] == 1
                    return assigned, insert_ms, select_ms

                request(path="/health")
                start = time.perf_counter()
                with ThreadPoolExecutor(max_workers=clients) as pool:
                    results = list(pool.map(pair, range(pairs)))
                seconds = time.perf_counter() - start
                assert sorted(row[0] for row in results) == list(range(1, pairs + 1))
                metrics, _ = request(path="/metrics")
                assert metrics["peak_active"] <= workers
                assert metrics["errors"] == metrics["rejected"] == 0
            finally:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                process.stdout.close()
            assert process.returncode == 0, f"Server did not shut down gracefully: {process.returncode}"
        with Database(directory) as database:
            assert database.execute("SELECT COUNT(*) FROM users")["rows"] == [{"count": pairs}]
            assert database.tables["users"].next_id == pairs + 1
        latencies = sorted(ms for row in results for ms in row[1:])
        return {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
                "platform": platform.platform(), "clients": clients, "workers": workers,
                "insert_select_pairs": pairs, "requests": pairs * 2, "durable_fsync": True,
                "elapsed_seconds": seconds, "requests_per_second": pairs * 2 / seconds,
                "latency_median_ms": statistics.median(latencies),
                "latency_p95_ms": latencies[max(0, int(len(latencies) * 0.95) - 1)],
                "persisted_rows_verified": pairs, "graceful_shutdown_verified": True,
                "metrics": metrics}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=int, default=500)
    parser.add_argument("--clients", type=int, default=16)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/api-load.json"))
    args = parser.parse_args()
    if min(args.pairs, args.clients, args.workers) < 1:
        parser.error("pairs, clients and workers must be positive")
    result = run(args.pairs, args.clients, args.workers)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
