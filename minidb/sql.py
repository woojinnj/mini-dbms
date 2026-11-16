"""Tokenizer and recursive-descent parser. No eval or external SQL engine."""

from dataclasses import dataclass
import math
import re

from .errors import ParseError

MAX_SQL_BYTES = 2 * 1024 * 1024
MAX_INSERT_ROWS = 10_000
TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<line>--[^\n]*)|(?P<comment>/\*[\s\S]*?\*/)"
    r"|(?P<string>'(?:[^']|'')*')|(?P<number>-?\d+(?:\.\d+)?)"
    r"|(?P<ident>[A-Za-z_][A-Za-z_0-9]*)|(?P<symbol><=|>=|<>|!=|[=<>(),;*])"
)


@dataclass(frozen=True)
class ColumnDefinition:
    name: str
    kind: str
    constraint: str | None


@dataclass(frozen=True)
class CreateTable:
    table: str
    columns: tuple[ColumnDefinition, ...]


@dataclass(frozen=True)
class Insert:
    table: str
    columns: tuple | None
    rows: tuple


@dataclass(frozen=True)
class Condition:
    column: str
    operator: str
    value: object


@dataclass(frozen=True)
class Select:
    table: str
    columns: tuple
    conditions: tuple
    limit: int | None
    count: bool = False
    explain: bool = False


class Parser:
    def __init__(self, sql):
        if not isinstance(sql, str):
            raise ParseError("SQL must be text of at most 2 MiB")
        try:
            size = len(sql.encode("utf-8"))
        except UnicodeError:
            raise ParseError("SQL contains invalid Unicode") from None
        if size > MAX_SQL_BYTES:
            raise ParseError("SQL must be text of at most 2 MiB")
        self.tokens = []
        position = 0
        while position < len(sql):
            match = TOKEN.match(sql, position)
            if not match:
                raise ParseError(f"Unexpected character at position {position}")
            if match.lastgroup not in ("space", "line", "comment"):
                self.tokens.append((match.lastgroup, match.group(), position))
            position = match.end()
        self.tokens.append(("eof", "<end>", len(sql)))
        self.i = 0

    def peek(self, text):
        kind, value, _ = self.tokens[self.i]
        return kind != "string" and value.upper() == text

    def take(self, text):
        if self.peek(text):
            self.i += 1
            return True
        return False

    def expect(self, text):
        if not self.take(text):
            self.fail(f"Expected {text}")

    def fail(self, message):
        raise ParseError(f"{message} at position {self.tokens[self.i][2]}")

    def identifier(self):
        kind, value, _ = self.tokens[self.i]
        if kind != "ident":
            self.fail("Expected identifier")
        self.i += 1
        return value.lower()

    def literal(self):
        kind, value, _ = self.tokens[self.i]
        self.i += 1
        if kind == "string":
            return value[1:-1].replace("''", "'")
        if kind == "number":
            try:
                number = float(value) if "." in value else int(value)
            except ValueError:
                raise ParseError("Invalid numeric literal") from None
            if isinstance(number, float) and not math.isfinite(number):
                raise ParseError("Numeric literal must be finite")
            return number
        if kind == "ident" and value.upper() in ("NULL", "TRUE", "FALSE"):
            return {"NULL": None, "TRUE": True, "FALSE": False}[value.upper()]
        self.i -= 1
        self.fail("Expected string, number, boolean or NULL")

    def parse(self):
        statements = []
        while self.tokens[self.i][0] != "eof":
            if self.take(";"):
                continue
            if self.take("CREATE"):
                statements.append(self.create_table())
            elif self.take("INSERT"):
                statements.append(self.insert())
            else:
                explain = self.take("EXPLAIN")
                self.expect("SELECT")
                statements.append(self.select(explain))
            if not self.take(";") and self.tokens[self.i][0] != "eof":
                self.fail("Expected semicolon")
        if not statements:
            raise ParseError("No SQL statement supplied")
        return statements

    def create_table(self):
        self.expect("TABLE")
        table = self.identifier()
        self.expect("(")
        columns = []
        while True:
            name = self.identifier()
            kind = self.identifier().upper()
            constraint = None
            if self.take("PRIMARY"):
                self.expect("KEY")
                constraint = "PRIMARY KEY"
            elif self.take("NOT"):
                self.expect("NULL")
                constraint = "NOT NULL"
            elif self.take("NULL"):
                constraint = "NULL"
            columns.append(ColumnDefinition(name, kind, constraint))
            if not self.take(","):
                break
        self.expect(")")
        return CreateTable(table, tuple(columns))

    def insert(self):
        self.expect("INTO")
        table = self.identifier()
        columns = None
        if self.take("("):
            columns = [self.identifier()]
            while self.take(","):
                columns.append(self.identifier())
            self.expect(")")
            columns = tuple(columns)
        self.expect("VALUES")
        rows = []
        while True:
            self.expect("(")
            row = [self.literal()]
            while self.take(","):
                row.append(self.literal())
            self.expect(")")
            rows.append(tuple(row))
            if len(rows) > MAX_INSERT_ROWS:
                raise ParseError("At most 10000 rows per INSERT")
            if not self.take(","):
                break
        return Insert(table, columns, tuple(rows))

    def select(self, explain):
        count = self.peek("COUNT") and self.tokens[self.i + 1][1] == "("
        if count:
            self.expect("COUNT")
            self.expect("(")
            self.expect("*")
            self.expect(")")
            columns = ("count",)
        elif self.take("*"):
            columns = ("*",)
        else:
            columns = [self.identifier()]
            while self.take(","):
                columns.append(self.identifier())
            columns = tuple(columns)
        self.expect("FROM")
        table = self.identifier()
        conditions = []
        if self.take("WHERE"):
            while True:
                column = self.identifier()
                if self.take("IS"):
                    operator = "IS NOT" if self.take("NOT") else "IS"
                    self.expect("NULL")
                    value = None
                else:
                    operator = self.tokens[self.i][1]
                    if operator not in ("=", "!=", "<>", "<", "<=", ">", ">="):
                        self.fail("Expected comparison operator")
                    self.i += 1
                    value = self.literal()
                conditions.append(Condition(column, operator, value))
                if not self.take("AND"):
                    break
        limit = None
        if self.take("LIMIT"):
            limit = self.literal()
            if type(limit) is not int or limit < 0:
                raise ParseError("LIMIT must be a nonnegative integer")
        return Select(table, columns, tuple(conditions), limit, count, explain)


def parse(sql):
    return Parser(sql).parse()
