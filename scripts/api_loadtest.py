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
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from minidb import Database


def wait_for_port(process, stdout_path, stderr_path, timeout):
    deadline = time.monotonic() + timeout
    while True:
        returncode = process.poll()
        if returncode is not None:
            failure = f"Server exited before startup (exit code {returncode})"
            break

        # Check a regular file so readiness does not depend on pipe events.
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"^MiniDB listening on http://127\.0\.0\.1:(\d+) [^\n]*\n", stdout, re.MULTILINE)
        if match:
            return int(match[1])

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failure = f"Server startup timed out after {timeout}s (pid {process.pid})"
            break
        time.sleep(min(remaining, 0.05))

    stdout = stdout_path.read_text(encoding="utf-8", errors="replace").strip()
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
    raise RuntimeError(f"{failure}\nServer stdout:\n{stdout or '(empty)'}\nServer stderr:\n{stderr or '(empty)'}")


def run(pairs, clients, workers, startup_timeout=60):
    with tempfile.TemporaryDirectory(prefix="minidb-api-") as directory:
        environment = dict(os.environ)
        environment.pop("MINIDB_API_TOKEN", None)
        stdout_path = Path(directory) / "server.stdout.log"
        stderr_path = Path(directory) / "server.stderr.log"
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                [sys.executable, "-u", "-m", "minidb", "--data-dir", directory, "serve", "--port", "0",
                 "--workers", str(workers), "--queue-size", str(max(32, clients * 2))],
                cwd=ROOT, stdout=stdout, stderr=stderr, env=environment)
            try:
                port = wait_for_port(process, stdout_path, stderr_path, startup_timeout)

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
    parser.add_argument("--startup-timeout", type=int, default=60,
                        help="seconds to wait for the server to listen (default: 60)")
    parser.add_argument("--output", type=Path, default=Path("benchmarks/api-load.json"))
    args = parser.parse_args()
    if min(args.pairs, args.clients, args.workers, args.startup_timeout) < 1:
        parser.error("pairs, clients, workers and startup-timeout must be positive")
    result = run(args.pairs, args.clients, args.workers, args.startup_timeout)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
