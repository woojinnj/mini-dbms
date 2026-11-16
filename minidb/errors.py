class DBError(Exception):
    """An actionable error safe to return to a SQL client."""


class ParseError(DBError):
    pass


class ValidationError(DBError):
    pass


class StorageError(DBError):
    pass
