"""Self-contained Schema evaluation never fetches a remote URI or local file."""
from referencing import Registry
from referencing.exceptions import Unresolvable

from .runtime import GatewayError

LOCAL_SCHEMA_REGISTRY = Registry()


def validate_local(validator, arguments):
    try:
        validator.validate(arguments)
    except Unresolvable:
        raise GatewayError(
            "schema_reference_unavailable",
            "Schema reference cannot be resolved locally; supply a self-contained definition with local $defs",
        ) from None
