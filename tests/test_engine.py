from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from minidb import Database
from minidb.errors import DBError, StorageError, ValidationError
from minidb.storage import MAGIC, TRANSACTION, TRAILER


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(self.temp.name, durable=False)
        self.addCleanup(self.db.close)

    def seed(self):
        self.db.execute("INSERT INTO users (name, age, email) VALUES ('가', 10, NULL), ('나', 20, 'b'), ('다', 30, 'c')")

    def test_insert_projection_and_plans(self):
        self.seed()
        result = self.db.execute("SELECT name FROM users WHERE id = 2")
        self.assertEqual(result["rows"], [{"name": "나"}])
        self.assertEqual(result["plan"], "BPLUS_POINT_LOOKUP")
        self.assertEqual(result["rows_examined"], 1)
        result = self.db.execute("SELECT name FROM users WHERE age = 20")
        self.assertEqual(result["rows"], [{"name": "나"}])
        self.assertEqual(result["plan"], "FULL_TABLE_SCAN")
        self.assertEqual(result["rows_examined"], 3)
        self.assertEqual(self.db.execute("SELECT * FROM users WHERE id = 99")["rows_examined"], 0)

    def test_range_and_residual_predicates(self):
        self.seed()
        for where, expected in [("id > 1 AND id <= 3", [2, 3]), ("id >= 2 AND id > 2", [3]),
                                ("id <= 2 AND id < 2", [1]), ("id > 3 AND id < 1", []),
                                ("id = 2 AND age = 30", []), ("id = 2 AND id = 3", []),
                                ("id >= 1 AND email IS NOT NULL", [2, 3])]:
            with self.subTest(where=where):
                result = self.db.execute(f"SELECT id FROM users WHERE {where}")
                self.assertEqual([r["id"] for r in result["rows"]], expected)

    def test_null_semantics(self):
        self.seed()
        self.assertEqual(self.db.execute("SELECT id FROM users WHERE email IS NULL")["rows"], [{"id": 1}])
        for condition in ("email = NULL", "email != NULL", "id = NULL", "id IS NULL"):
            self.assertEqual(self.db.execute(f"SELECT * FROM users WHERE {condition}")["rows"], [])

    def test_count_limit_and_truncation(self):
        self.seed()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM users WHERE age > 10")["rows"], [{"count": 2}])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM users LIMIT 1")["rows"], [{"count": 3}])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM users LIMIT 0")["rows"], [])
        self.assertEqual(self.db.execute("SELECT * FROM users LIMIT 0")["rows_examined"], 0)
        self.db.max_result_rows = 2
        result = self.db.execute("SELECT * FROM users")
        self.assertEqual(result["row_count"], 2)
        self.assertTrue(result["truncated"])
        self.assertFalse(self.db.execute("SELECT * FROM users LIMIT 2")["truncated"])
        self.assertTrue(self.db.execute("SELECT * FROM users LIMIT 3")["truncated"])

    def test_validation_is_atomic(self):
        invalid = ["INSERT INTO users (id, name, age) VALUES (1, 'x', 1)",
                   "INSERT INTO users (name, name) VALUES ('x', 'y')",
                   "INSERT INTO users (unknown) VALUES ('x')",
                   "INSERT INTO users (name) VALUES ('x')",
                   "INSERT INTO users (name, age) VALUES ('x', 'bad')",
                   "INSERT INTO users (name, age) VALUES ('x', TRUE)",
                   "INSERT INTO users (name, age) VALUES ('x', 9223372036854775808)",
                   "INSERT INTO users (name, age) VALUES ('valid', 1), ('bad', NULL)",
                   "INSERT INTO users VALUES ('x', 1)"]
        for sql in invalid:
            with self.subTest(sql=sql), self.assertRaises(ValidationError):
                self.db.execute(sql)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM users")["rows"], [{"count": 0}])
        result = self.db.execute("INSERT INTO users (name, age) VALUES ('works', 1)")
        self.assertEqual(result["first_id"], 1)

    def test_unknown_columns_even_on_empty_table(self):
        for sql in ("SELECT missing FROM users", "SELECT * FROM missing", "SELECT id, id FROM users",
                    "SELECT * FROM users WHERE age = 'string'", "SELECT * FROM users WHERE missing = 1"):
            with self.subTest(sql=sql), self.assertRaises(ValidationError):
                self.db.execute(sql)

    def test_explain_and_multi_statement_contract(self):
        self.assertEqual(self.db.execute("EXPLAIN SELECT * FROM users WHERE id = 1")["plan"], "BPLUS_POINT_LOOKUP")
        with self.assertRaises(ValidationError):
            self.db.execute("INSERT INTO users VALUES ('x', 1, NULL); SELECT * FROM users")
        self.assertEqual(self.db.tables["users"].index.size, 0)
        results = self.db.execute_script("INSERT INTO users VALUES ('x', 1, NULL); SELECT * FROM users")
        self.assertEqual(len(results), 2)
        with self.assertRaises(DBError):
            self.db.execute_script("INSERT INTO users VALUES ('y', 1, NULL); INVALID")
        self.assertEqual(self.db.tables["users"].index.size, 1)

    def test_restart_rebuild_and_next_id(self):
        self.seed()
        self.db.close()
        self.db = Database(self.temp.name, durable=False)
        self.addCleanup(self.db.close)
        self.assertEqual(self.db.execute("SELECT name FROM users WHERE id = 3")["rows"], [{"name": "다"}])
        self.assertEqual(self.db.execute("INSERT INTO users VALUES ('new', 1, NULL)")["first_id"], 4)
        self.assertTrue(self.db.tables["users"].index.validate())

    def test_process_lock(self):
        process = subprocess.run([sys.executable, "-m", "minidb", "--data-dir", self.temp.name,
                                  "exec", "--sql", "SELECT * FROM users"], capture_output=True, text=True)
        self.assertEqual(process.returncode, 1)
        self.assertIn("already open", process.stderr)

    def test_concurrent_insert_and_select(self):
        def work(i):
            result = self.db.execute(f"INSERT INTO users VALUES ('u{i}', {i}, NULL)")
            row = self.db.execute(f"SELECT * FROM users WHERE id = {result['first_id']}")["rows"][0]
            self.assertEqual(row["age"], i)
            if i % 10 == 0:
                self.db.execute("SELECT COUNT(*) FROM users")
            return result["first_id"]

        with ThreadPoolExecutor(max_workers=16) as pool:
            ids = list(pool.map(work, range(400)))
        self.assertEqual(sorted(ids), list(range(1, 401)))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM users")["rows"], [{"count": 400}])
        self.db.tables["users"].index.validate()

    def test_random_queries_against_reference(self):
        rng = random.Random(7)
        ages = [rng.randrange(100) for _ in range(300)]
        self.db.execute("INSERT INTO users VALUES " + ",".join(f"('u{i}', {age}, NULL)" for i, age in enumerate(ages)))
        for _ in range(80):
            low, high, age = rng.randrange(350), rng.randrange(350), rng.randrange(100)
            expected = [{"id": i + 1} for i, value in enumerate(ages) if low <= i + 1 < high and value >= age]
            actual = self.db.execute(f"SELECT id FROM users WHERE id >= {low} AND id < {high} AND age >= {age}")
            self.assertEqual(actual["rows"], expected)

    def test_failed_append_rolls_back_file_and_index(self):
        self.seed()
        table = self.db.tables["users"]
        size = table.path.stat().st_size
        original_insert = table.index.insert

        def fail_after_index_change(key, value):
            original_insert(key, value)
            raise OSError("injected failure")

        with patch.object(table.index, "insert", side_effect=fail_after_index_change):
            with self.assertRaises(StorageError):
                self.db.execute("INSERT INTO users VALUES ('bad', 1, NULL)")
        self.assertEqual(table.path.stat().st_size, size)
        self.assertEqual(table.index.size, 3)
        self.assertEqual(self.db.execute("INSERT INTO users VALUES ('ok', 1, NULL)")["first_id"], 4)

    def test_schema_persistence_and_types(self):
        self.db.close()
        with self.assertRaises(ValidationError):
            Database(self.temp.name, schema={"other": {"x": {"type": "TEXT", "nullable": True}}})
        with tempfile.TemporaryDirectory() as directory:
            schema = {"products": {"price": {"type": "REAL", "nullable": False},
                                   "available": {"type": "BOOLEAN", "nullable": False}}}
            with Database(directory, schema, durable=False) as db:
                db.execute("INSERT INTO products VALUES (12.5, TRUE)")
                self.assertEqual(db.execute("SELECT * FROM products WHERE available = TRUE")["rows"][0]["price"], 12.5)

    def test_closed_database(self):
        self.db.close()
        with self.assertRaises(StorageError):
            self.db.execute("SELECT * FROM users")

    def test_index_path_does_not_scan_file(self):
        self.seed()
        with patch.object(self.db.tables["users"], "records", side_effect=AssertionError("unexpected full scan")):
            self.assertEqual(self.db.execute("SELECT id FROM users WHERE id = 2")["rows"], [{"id": 2}])
            self.assertEqual(self.db.execute("SELECT id FROM users WHERE id > 1")["rows"], [{"id": 2}, {"id": 3}])

    def test_result_byte_limit(self):
        self.seed()
        with patch("minidb.engine.MAX_RESULT_BYTES", 23):
            result = self.db.execute("SELECT id FROM users")
            self.assertEqual(result["row_count"], 2)
            self.assertTrue(result["truncated"])
        with patch("minidb.engine.MAX_RESULT_BYTES", 1), self.assertRaises(ValidationError):
            self.db.execute("SELECT * FROM users")

    def test_missing_table_is_not_silently_recreated(self):
        self.seed()
        self.db.close()
        (Path(self.temp.name) / "users.mdb").unlink()
        with self.assertRaises(StorageError):
            Database(self.temp.name, durable=False)

    def test_missing_schema_is_not_silently_recreated(self):
        self.seed()
        self.db.close()
        (Path(self.temp.name) / "schema.json").unlink()
        with self.assertRaises(StorageError):
            Database(self.temp.name, durable=False)

    def test_readers_can_execute_concurrently(self):
        self.seed()
        barrier = threading.Barrier(4)
        original = self.db._select

        def synchronized_select(table, statement):
            barrier.wait(timeout=3)
            return original(table, statement)

        with patch.object(self.db, "_select", side_effect=synchronized_select):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: self.db.execute("SELECT name FROM users WHERE id = 2"), range(4)))
        self.assertTrue(all(result["rows"] == [{"name": "나"}] for result in results))

    def test_text_limit_and_null_defaults(self):
        with self.assertRaises(ValidationError):
            self.db.execute("INSERT INTO users (name, age) VALUES ('" + "가" * 22_000 + "', 1)")
        self.db.execute("INSERT INTO users (name, age) VALUES ('', -1)")
        row = self.db.execute("SELECT * FROM users")["rows"][0]
        self.assertEqual(row, {"id": 1, "name": "", "age": -1, "email": None})


class RecoveryTests(unittest.TestCase):
    def test_every_torn_frame_boundary_is_atomic(self):
        with tempfile.TemporaryDirectory() as source:
            with Database(source, durable=False) as db:
                db.execute("INSERT INTO users VALUES ('base', 1, NULL)")
                start = db.tables["users"].path.stat().st_size
                db.execute("INSERT INTO users VALUES ('first', 2, NULL), ('second', 3, NULL)")
            data = (Path(source) / "users.mdb").read_bytes()
            schema = (Path(source) / "schema.json").read_bytes()
            with tempfile.TemporaryDirectory() as target:
                (Path(target) / "schema.json").write_bytes(schema)
                for cut in range(start, len(data)):
                    with self.subTest(cut=cut):
                        (Path(target) / "users.mdb").write_bytes(data[:cut])
                        with Database(target, durable=False) as db:
                            self.assertEqual(db.tables["users"].index.size, 1)
                            self.assertEqual(db.tables["users"].next_id, 2)
                            self.assertEqual(db.tables["users"].path.stat().st_size, start)

    def test_committed_corruption_is_not_silently_discarded(self):
        with tempfile.TemporaryDirectory() as directory:
            with Database(directory, durable=False) as db:
                db.execute("INSERT INTO users VALUES ('base', 1, NULL)")
            path = Path(directory) / "users.mdb"
            original = path.read_bytes()
            for offset in (0, *range(len(MAGIC), len(MAGIC) + TRANSACTION.size),
                           len(MAGIC) + TRANSACTION.size + 10, len(original) - TRAILER.size):
                corrupt = bytearray(original)
                corrupt[offset] ^= 0xFF
                path.write_bytes(corrupt)
                with self.subTest(offset=offset), self.assertRaises(StorageError):
                    Database(directory, durable=False)
                self.assertEqual(path.read_bytes(), corrupt)

    def test_durable_fsync_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            with Database(directory) as db:
                with patch("minidb.storage.os.fsync", side_effect=[OSError("disk failure"), None]):
                    with self.assertRaises(StorageError):
                        db.execute("INSERT INTO users VALUES ('x', 1, NULL)")
                self.assertEqual(db.tables["users"].index.size, 0)
                self.assertEqual(db.tables["users"].path.stat().st_size, len(MAGIC))
