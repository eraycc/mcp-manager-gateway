"""Reject known-unfixed WAL engines before creating or migrating repositories."""
import sqlite3


def require_safe_sqlite():
    version = sqlite3.sqlite_version_info
    fixed = (version >= (3, 51, 3)
             or (3, 50, 7) <= version < (3, 51, 0)
             or (3, 44, 6) <= version < (3, 45, 0))
    if not fixed:
        current = ".".join(map(str, version))
        raise RuntimeError(
            f"SQLite {current} lacks the WAL-reset concurrency fix. "
            "Use a Python build linked to SQLite 3.51.3+ (or fixed 3.50.7/3.44.6 branches) "
            "before opening or upgrading storage. Upgrading uv alone does not replace Python's SQLite library. "
            "See https://www.sqlite.org/wal.html#walresetbug")
