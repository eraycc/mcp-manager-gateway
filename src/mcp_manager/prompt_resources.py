"""Explicit, exact prompt URI compatibility; never retry a failed resource as a prompt."""
from urllib.parse import quote

from .runtime import GatewayError


def prompt_uri(row, name):
    return "prompt://" + row.slug + "/" + quote(name, safe="")


def prompt_read_hint(row, item):
    uri = "mcp-manager://" + row.id + "/" + prompt_uri(row, item["name"])
    return {
        "uri": uri,
        "read_with": {"tool": "gateway_read_resource", "arguments": {"uri": uri}},
        "get_with": {"method": "prompts/get", "params": {"name": item.get("gateway_name")}},
    }


def validate_prompt_arguments(definition, arguments):
    fields = {item["name"]: item for item in definition.get("arguments", [])}
    missing = [name for name, item in fields.items() if item.get("required") and name not in arguments]
    unexpected = [name for name in arguments if name not in fields]
    if missing or unexpected or any(not isinstance(value, str) for value in arguments.values()):
        message = "Invalid prompt arguments"
        if missing:
            message += "; missing: " + ", ".join(missing)
        if unexpected:
            message += "; unexpected: " + ", ".join(unexpected)
        raise GatewayError("invalid_arguments", message, {"missing": missing, "unexpected": unexpected})
