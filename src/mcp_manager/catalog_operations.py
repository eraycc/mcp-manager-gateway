"""Count completed MCP operations without counting their internal preparation twice."""
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from uuid import uuid4

current_operation = ContextVar("catalog_operation", default=None)


@dataclass
class Operation:
    catalog: object
    kind: str
    identity: str = field(default_factory=lambda: uuid4().hex)
    success_version: int | None = None
    ticket: dict | None = None
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
        from .control_results import bind_operation
        self.scope, self.ticket = await bind_operation(self.catalog, row, user_id)


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
        # Publication itself commits the authoritative success receipt.
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


@asynccontextmanager
async def call_storage_activity(catalog, kind):
    gate = getattr(getattr(catalog, "logs", None), "gate", None)
    if kind == "call" and gate is not None:
        # Ordinary writes remain concurrent. Only exclusive physical index
        # maintenance is fenced until this call and its terminal log finish.
        async with gate.async_operation():
            yield
    else:
        yield


def tracked(kind):
    def decorate(method):
        @wraps(method)
        async def wrapped(self, target, *args, **kwargs):
            row = target if kind == "call" else await self.get(target)
            user = kwargs.get("user")
            user_id = user.id if user else None
            if kind == "refresh":
                user_id = args[0] if args else kwargs.get("user_id")
            async with call_storage_activity(self, kind), operation(self, row, user_id, kind=kind) as outcome:
                result = await method(self, target, *args, **kwargs)
                if result is None and kind == "refresh":
                    outcome.discard = True
                return result
        return wrapped
    return decorate
