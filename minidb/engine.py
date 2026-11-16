import fcntl
import json
import operator
import os
from pathlib import Path
from threading import Lock
import time

from .errors import StorageError, ValidationError
from .schema import DEFAULT_SCHEMA, MAX_INT, validate_schema, validate_value
from .sql import CreateTable, Insert, parse
from .storage import MAGIC, Table

COMPARISONS = {"=": operator.eq, "!=": operator.ne, "<>": operator.ne,
               "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}
ID_SPEC = {"type": "INTEGER", "nullable": False}
MAX_RESULT_BYTES = 8 * 1024 * 1024


class Database:
    def __init__(self, directory="data", schema=None, durable=True, max_result_rows=10_000):
        if max_result_rows < 1:
            raise ValueError("max_result_rows must be positive")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_result_rows = max_result_rows
        self.durable = durable
        self.closed = False
        self._catalog_lock = Lock()
        self._lock_file = (self.directory / ".lock").open("a+b")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            raise StorageError("Database is already open in another process; use the running API server") from None
        try:
            schema_path = self.directory / "schema.json"
            if schema_path.exists():
                stored = validate_schema(json.loads(schema_path.read_text(encoding="utf-8")))
                if schema is not None:
                    provided = validate_schema(schema)
                    if any(stored.get(name) != columns for name, columns in provided.items()):
                        raise ValidationError("Provided schema differs from the persisted schema")
                self.schema = stored
                for name in stored:
                    if not (self.directory / f"{name}.mdb").is_file():
                        raise StorageError(f"Missing table file: {name}.mdb; restore the database from backup")
            else:
                if any(self.directory.glob("*.mdb")):
                    raise StorageError("Schema is missing while table files exist; restore schema.json from backup")
                self.schema = validate_schema(json.loads(json.dumps(schema if schema is not None else DEFAULT_SCHEMA)))
                self._write_schema(self.schema)
            self.tables = {name: Table(self.directory / f"{name}.mdb", columns, durable)
                           for name, columns in self.schema.items()}
            self._sync_directory()
        except Exception:
            self.close()
            raise

    def close(self):
        if not self.closed:
            self.closed = True
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _write_schema(self, schema):
        temporary = self.directory / "schema.json.tmp"
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(schema, file, ensure_ascii=False, indent=2)
            file.flush()
            if self.durable:
                os.fsync(file.fileno())
        temporary.replace(self.directory / "schema.json")

    def _sync_directory(self):
        if self.durable:
            descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def table_names(self):
        with self._catalog_lock:
            return list(self.tables)

    def execute(self, sql):
        """Exactly one statement, suitable for an API request."""
        statements = parse(sql)
        if len(statements) != 1:
            raise ValidationError("Exactly one SQL statement is required per API/execute call")
        return self.execute_statement(statements[0])

    def execute_script(self, sql):
        # Parse all syntax before writes. Each statement commits independently.
        return [self.execute_statement(statement) for statement in parse(sql)]

    def execute_statement(self, statement):
        if self.closed:
            raise StorageError("Database is closed")
        start = time.perf_counter()
        try:
            if isinstance(statement, CreateTable):
                result = self._create_table(statement)
            else:
                with self._catalog_lock:
                    table = self.tables.get(statement.table)
                if table is None:
                    raise ValidationError(f"Unknown table: {statement.table}")
                if isinstance(statement, Insert):
                    with table.lock.write():
                        result = self._insert(table, statement)
                else:
                    with table.lock.read():
                        if table.poisoned:
                            raise StorageError("Table unavailable after an I/O failure")
                        result = self._select(table, statement)
        except OSError as error:
            raise StorageError(f"Storage I/O failed: {error.strerror}") from error
        result["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 4)
        return result

    def _create_table(self, statement):
        definitions = {column.name: column for column in statement.columns}
        if len(definitions) != len(statement.columns):
            raise ValidationError("Duplicate CREATE TABLE column")
        id_column = definitions.pop("id", None)
        if id_column is not None and (id_column.kind, id_column.constraint) != ("INTEGER", "PRIMARY KEY"):
            raise ValidationError("id must be declared as INTEGER PRIMARY KEY or omitted")
        if any(column.constraint == "PRIMARY KEY" for column in definitions.values()):
            raise ValidationError("Only id may be the PRIMARY KEY")
        columns = {
            column.name: {"type": column.kind, "nullable": column.constraint != "NOT NULL"}
            for column in definitions.values()
        }
        name = statement.table
        validate_schema({name: columns})

        with self._catalog_lock:
            if name in self.tables:
                raise ValidationError(f"Table already exists: {name}")
            path = self.directory / f"{name}.mdb"
            # An interrupted CREATE can leave an empty file before the schema is committed.
            if path.exists() and path.stat().st_size != len(MAGIC):
                raise StorageError(f"Unregistered table file contains data: {path.name}")
            table = Table(path, columns, self.durable)
            self._sync_directory()
            schema = {**self.schema, name: columns}
            self._write_schema(schema)
            self.schema = schema
            self.tables[name] = table
            self._sync_directory()
        return {"command": "CREATE TABLE", "table": name}

    def _insert(self, table, statement):
        columns = statement.columns if statement.columns is not None else tuple(table.columns)
        if len(columns) != len(set(columns)):
            raise ValidationError("Duplicate INSERT column")
        for name in columns:
            if name == "id":
                raise ValidationError("id is assigned automatically and cannot be supplied")
            if name not in table.columns:
                raise ValidationError(f"Unknown column: {name}")
        if table.next_id + len(statement.rows) - 1 > MAX_INT:
            raise ValidationError("Automatic ID exhausted")
        rows = []
        for i, values in enumerate(statement.rows):
            if len(values) != len(columns):
                raise ValidationError("INSERT column and value counts differ")
            supplied = dict(zip(columns, values))
            row = {"id": table.next_id + i}
            for name, spec in table.columns.items():
                value = supplied.get(name)
                validate_value(name, value, spec)
                row[name] = value
            rows.append(row)
        table.append(rows)
        return {"command": "INSERT", "affected_rows": len(rows), "first_id": rows[0]["id"],
                "last_id": rows[-1]["id"]}

    @staticmethod
    def _matches(row, conditions):
        for condition in conditions:
            value = row[condition.column]
            if condition.operator == "IS":
                matches = value is None
            elif condition.operator == "IS NOT":
                matches = value is not None
            elif value is None or condition.value is None:
                matches = False  # SQL UNKNOWN does not pass WHERE.
            else:
                matches = COMPARISONS[condition.operator](value, condition.value)
            if not matches:
                return False
        return True

    @staticmethod
    def _plan(statement):
        usable = [c for c in statement.conditions if c.column == "id" and type(c.value) is int]
        equality = next((c for c in usable if c.operator == "="), None)
        if equality:
            return "BPLUS_POINT_LOOKUP", equality
        ranges = [c for c in usable if c.operator in (">", ">=", "<", "<=")]
        if ranges:
            return "BPLUS_RANGE_SCAN", ranges
        return "FULL_TABLE_SCAN", None

    def _select(self, table, statement):
        specs = {"id": ID_SPEC, **table.columns}
        columns = tuple(specs) if statement.columns == ("*",) else statement.columns
        if not statement.count:
            if len(columns) != len(set(columns)):
                raise ValidationError("Duplicate SELECT column")
            for name in columns:
                if name not in specs:
                    raise ValidationError(f"Unknown column: {name}")
        for condition in statement.conditions:
            if condition.column not in specs:
                raise ValidationError(f"Unknown column: {condition.column}")
            validate_value(condition.column, condition.value, specs[condition.column], allow_null=True)
        plan, constraint = self._plan(statement)
        if statement.explain:
            return {"command": "EXPLAIN", "plan": plan, "table": statement.table,
                    "index": "PRIMARY(id)" if constraint else None, "table_rows": table.index.size}

        def candidates():
            if plan == "FULL_TABLE_SCAN":
                for _, row in table.records():
                    yield row
                return
            if plan == "BPLUS_POINT_LOOKUP":
                offset = table.index.get(constraint.value)
                offsets = [] if offset is None else [(constraint.value, offset)]
            else:
                low, high, include_low, include_high = None, None, True, True
                for c in constraint:
                    if c.operator in (">", ">="):
                        if low is None or c.value > low:
                            low, include_low = c.value, c.operator == ">="
                        elif c.value == low:
                            include_low = include_low and c.operator == ">="
                    else:
                        if high is None or c.value < high:
                            high, include_high = c.value, c.operator == "<="
                        elif c.value == high:
                            include_high = include_high and c.operator == "<="
                offsets = table.index.range(low, high, include_low, include_high)
            # Each query owns its file handle, avoiding shared seek position races.
            with table.path.open("rb") as file:
                for _, offset in offsets:
                    yield table.read_at(file, offset)

        rows, examined, matched, truncated, result_bytes = [], 0, 0, False, 0
        requested = statement.limit
        cap = min(requested, self.max_result_rows) if requested is not None else self.max_result_rows
        iterator = candidates()
        try:
            if requested != 0:
                for row in iterator:
                    examined += 1
                    if not self._matches(row, statement.conditions):
                        continue
                    matched += 1
                    if statement.count:
                        continue
                    if len(rows) == cap:
                        truncated = True
                        break
                    projected = {name: row[name] for name in columns}
                    row_bytes = len(json.dumps(projected, ensure_ascii=False).encode("utf-8")) + 2
                    if result_bytes + row_bytes > MAX_RESULT_BYTES:
                        if not rows:
                            raise ValidationError("Projected row exceeds 8 MiB; select fewer columns")
                        truncated = True
                        break
                    rows.append(projected)
                    result_bytes += row_bytes
                    if requested is not None and len(rows) >= requested:
                        break
        finally:
            iterator.close()
        if statement.count and requested != 0:
            rows = [{"count": matched}]
        return {"command": "SELECT", "columns": list(columns), "rows": rows, "row_count": len(rows),
                "plan": plan, "rows_examined": examined, "truncated": truncated}
