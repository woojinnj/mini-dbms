import math
import re

from .errors import ValidationError

DEFAULT_SCHEMA = {"users": {
    "name": {"type": "TEXT", "nullable": False},
    "age": {"type": "INTEGER", "nullable": False},
    "email": {"type": "TEXT", "nullable": True},
}}
IDENTIFIER = re.compile(r"[a-z_][a-z_0-9]*\Z")
MAX_INT = 2**63 - 1


def validate_schema(schema):
    if not isinstance(schema, dict) or not schema:
        raise ValidationError("Schema must contain at least one table")
    for table, columns in schema.items():
        if not IDENTIFIER.fullmatch(table) or not isinstance(columns, dict) or not columns:
            raise ValidationError("Tables need lowercase identifiers and at least one column")
        for name, spec in columns.items():
            if name == "id" or not IDENTIFIER.fullmatch(name):
                raise ValidationError("Column names must be lowercase; id is automatically defined")
            if not isinstance(spec, dict) or set(spec) != {"type", "nullable"}:
                raise ValidationError("Each column needs exactly type and nullable")
            if spec["type"] not in ("TEXT", "INTEGER", "REAL", "BOOLEAN") or type(spec["nullable"]) is not bool:
                raise ValidationError("Invalid column type or nullable flag")
    return schema


def validate_value(name, value, spec, allow_null=False):
    if value is None:
        if not allow_null and not spec["nullable"]:
            raise ValidationError(f"{name} cannot be NULL")
        return
    kind = spec["type"]
    valid = (
        (kind == "TEXT" and type(value) is str)
        or (kind == "INTEGER" and type(value) is int and -2**63 <= value <= MAX_INT)
        or (kind == "REAL" and type(value) in (int, float) and -1.7976931348623157e308 <= value <= 1.7976931348623157e308 and math.isfinite(value))
        or (kind == "BOOLEAN" and type(value) is bool)
    )
    if not valid:
        raise ValidationError(f"{name} requires {kind} (received {type(value).__name__})")
    if kind == "TEXT" and len(value.encode("utf-8")) > 64 * 1024:
        raise ValidationError(f"{name} exceeds 64 KiB")
