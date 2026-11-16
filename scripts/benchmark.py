#!/usr/bin/env python3
"""Reproducible million-row benchmark through the same public SQL processor."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import random
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from minidb import Database


def run(directory, rows, batch_size, repetitions, durable):
    if (Path(directory) / "schema.json").exists():
        raise ValueError("Benchmark requires a fresh directory to avoid modifying existing data")
    measurements = []
    with Database(directory, durable=durable) as database:
        start = time.perf_counter()
        for first in range(1, rows + 1, batch_size):
            last = min(first + batch_size, rows + 1)
            sql = "INSERT INTO users (name, age, email) VALUES " + ",".join(
                f"('user_{i}', {i % 100}, 'user_{i}@example.com')" for i in range(first, last))
            inserted = database.execute(sql)
            assert inserted["first_id"] == first and inserted["last_id"] == last - 1
            if (last - 1) % 100_000 == 0 or last == rows + 1:
                print(f"Inserted {last - 1:,}/{rows:,}", flush=True)
        insert_seconds = time.perf_counter() - start
        table = database.tables["users"]
        assert table.index.size == rows
        table.index.validate()
        # Warm both access paths without hiding setup cost in the measured data.
        assert database.execute("SELECT COUNT(*) FROM users")["rows"] == [{"count": rows}]
        database.execute(f"SELECT * FROM users WHERE id = {rows}")
        targets = random.Random(2026).sample(range(1, rows + 1), min(repetitions, rows))
        for i, target in enumerate(targets):
            results, timings = {}, {}
            # Alternate order to reduce order/cache bias. Both return the same single record.
            paths = ("indexed", "linear") if i % 2 == 0 else ("linear", "indexed")
            for path in paths:
                predicate = f"id = {target}" if path == "indexed" else f"email = 'user_{target}@example.com'"
                started = time.perf_counter_ns()
                results[path] = database.execute(f"SELECT * FROM users WHERE {predicate}")
                timings[path] = (time.perf_counter_ns() - started) / 1_000_000
            assert results["indexed"]["rows"] == results["linear"]["rows"]
            assert results["indexed"]["rows"][0]["id"] == target
            assert results["indexed"]["rows_examined"] == 1
            assert results["linear"]["rows_examined"] == rows
            measurements.append({"id": target, "indexed_ms": timings["indexed"], "linear_ms": timings["linear"],
                                 "indexed_rows_examined": 1, "linear_rows_examined": rows})
            print(f"Lookup {target}: index={timings['indexed']:.4f} ms, scan={timings['linear']:.2f} ms", flush=True)
        file_bytes = table.path.stat().st_size
    started = time.perf_counter()
    with Database(directory, durable=durable) as database:
        rebuild_seconds = time.perf_counter() - started
        assert database.tables["users"].index.size == rows
        assert database.tables["users"].next_id == rows + 1
        assert database.execute(f"SELECT * FROM users WHERE id = {rows}")["rows"][0]["id"] == rows
    indexed = statistics.median(m["indexed_ms"] for m in measurements)
    linear = statistics.median(m["linear_ms"] for m in measurements)
    return {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
            "platform": platform.platform(), "machine": platform.machine(),
            "rows": rows, "batch_size": batch_size, "durable_fsync": durable,
            "insert_seconds": insert_seconds, "insert_rows_per_second": rows / insert_seconds,
            "table_file_bytes": file_bytes, "restart_rebuild_seconds": rebuild_seconds,
            "indexed_median_ms": indexed, "linear_median_ms": linear, "median_speedup": linear / indexed,
            "methodology": "Same SQL parser and executor; unique email vs ID; identical results; "
                           "warm OS cache; alternating query order; complete scan without LIMIT; wall clock includes parsing. "
                           "Insertion uses multi-row SQL statements, one fsync per statement when durable.",
            "samples": measurements}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--data-dir", type=Path, help="new directory to retain benchmark files; defaults to temporary")
    parser.add_argument("--no-fsync", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("benchmarks/latest.json"))
    args = parser.parse_args()
    if args.rows < 1 or not 1 <= args.batch_size <= 10_000 or args.repetitions < 1:
        parser.error("rows/repetitions must be positive; batch size must be 1..10000")
    if args.data_dir:
        result = run(args.data_dir, args.rows, args.batch_size, args.repetitions, not args.no_fsync)
    else:
        with tempfile.TemporaryDirectory(prefix="minidb-benchmark-") as directory:
            result = run(directory, args.rows, args.batch_size, args.repetitions, not args.no_fsync)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
