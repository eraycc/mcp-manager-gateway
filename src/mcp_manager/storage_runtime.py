"""Recommend a fixed SQLite engine without blocking existing installations."""
import logging
import sqlite3

LOGGER = logging.getLogger(__name__)


def warn_sqlite_runtime():
    version = sqlite3.sqlite_version_info
    fixed = (version >= (3, 51, 3)
             or (3, 50, 7) <= version < (3, 51, 0)
             or (3, 44, 6) <= version < (3, 45, 0))
    if not fixed:
        current = ".".join(map(str, version))
        LOGGER.warning(
            "SQLite %s may be affected by the rare WAL-reset corruption bug; startup will continue. "
            "We recommend updating Python with uv or installing a newer Python build linked to "
            "SQLite 3.51.3+ (fixed backports: 3.50.7/3.44.6). Updating uv alone does not replace "
            "an existing Python environment. https://www.sqlite.org/wal.html#walresetbug", current)
    return fixed
