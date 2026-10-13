"""Portable UTC timestamps, exact identifiers and finite JSON serialization."""
import json
from datetime import UTC
from sqlalchemy import DateTime, String
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.engine import make_url
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(DATETIME(fsp=6) if dialect.name == "mysql" else DateTime())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def exact_string(length):
    return String(length).with_variant(String(length, collation="utf8mb4_bin"), "mysql")


def strict_json_dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def engine_options(settings):
    url = make_url(settings.database_url)
    options = {"json_serializer": strict_json_dumps}
    if url.get_backend_name() == "mysql":
        options.update(
            pool_pre_ping=True, pool_recycle=1800, pool_size=5, max_overflow=5,
            pool_timeout=10, connect_args={"connect_timeout": 10, "charset": "utf8mb4"},
        )
    return options
