"""Count completed MCP operations without counting their internal preparation twice."""
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps

current_operation = ContextVar("catalog_operation", default=None)


@dataclass
class Operation:
    catalog: object
    kind: str
    row: object = None
    user_id: str | None = None
    scope: object = None
    error: Exception | None = None
    refresh_version: str | None = None
    closed: bool = False
    discard: bool = False

    async def bind(self, row, user_id):
        if self.row is not None and (self.row.id, self.row.revision, self.user_id) == (
            row.id, row.revision, user_id
        ):
            return
        self.row, self.user_id = row, user_id
        self.scope = await self.catalog.index_scope(row, user_id)


@asynccontextmanager
async def operation(catalog, row=None, user_id=None, *, kind):
    parent = current_operation.get()
    if parent is not None and not parent.closed and (
        parent.row is None or row is None or
        (parent.row.id == row.id and parent.row.revision == row.revision and parent.user_id == user_id)
    ):
        if row is not None:
            await parent.bind(row, user_id)
        yield parent
        # A completed catalog publication is itself a successful refresh.
        if kind == "refresh" and row is not None:
            await catalog.record_operation_result(row, user_id, scope=parent.scope)
        return
    outcome = Operation(catalog, kind)
    if row is not None:
        await outcome.bind(row, user_id)
    token = current_operation.set(outcome)
    try:
        try:
            yield outcome
        except Exception as exc:
            if outcome.row is not None and not outcome.discard:
                await catalog.record_operation_result(
                    outcome.row, outcome.user_id, error=exc, kind=kind,
                    scope=outcome.scope, expected_refresh=outcome.refresh_version)
            raise
        else:
            if outcome.row is not None and not outcome.discard:
                await catalog.record_operation_result(
                    outcome.row, outcome.user_id, error=outcome.error, kind=kind,
                    scope=outcome.scope, expected_refresh=outcome.refresh_version)
    finally:
        outcome.closed = True
        current_operation.reset(token)


def tracked(kind):
    def decorate(method):
        @wraps(method)
        async def wrapped(self, target, *args, **kwargs):
            row = target if kind == "call" else await self.get(target)
            user = kwargs.get("user")
            user_id = user.id if user else None
            if kind == "refresh":
                user_id = args[0] if args else kwargs.get("user_id")
            async with operation(self, row, user_id, kind=kind) as outcome:
                result = await method(self, target, *args, **kwargs)
                if result is None and kind == "refresh":
                    outcome.discard = True
                return result
        return wrapped
    return decorate
