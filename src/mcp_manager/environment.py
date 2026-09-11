"""Resolve imported environment references only when opening a connection."""
import copy
import os
import re
import sys

from .runtime import GatewayError


def stdio_environment(configured, *, platform=None):
    result = {}
    if (platform or sys.platform) == "win32":
        # SDK defaults omit install roots used by browser and native-tool launchers.
        for name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "CommonProgramFiles",
                     "CommonProgramFiles(x86)", "CommonProgramW6432", "COMSPEC", "TMP"):
            if name in os.environ:
                result[name] = os.environ[name]
    # Windows environment keys are case-insensitive; explicit overrides always win.
    overridden = {key.casefold() for key in configured}
    return {key: value for key, value in result.items() if key.casefold() not in overridden} | configured


def environment_value(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise GatewayError("invalid_config", "Environment reference must be a variable name")
    if name not in os.environ:
        raise GatewayError("missing_environment", "Gateway environment variable is not set: " + name)
    return os.environ[name]


def connection_config(config):
    result = copy.deepcopy(config)
    style = result.pop("environment_expansion", None)
    def expand(value):
        if isinstance(value, list):
            return [expand(x) for x in value]
        if isinstance(value, dict):
            return {k: expand(v) for k, v in value.items()}
        if not isinstance(value, str):
            return value
        if style == "dsh" and re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", value):
            return environment_value(value[1:])
        if style == "claude":
            def replace(match):
                name, fallback = match[1], match[2]
                if name not in os.environ and fallback is not None:
                    return fallback
                return environment_value(name)
            return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}", replace, value)
        return value
    for key in ("command", "args", "env", "url", "headers"):
        if key in result:
            result[key] = expand(result[key])
    env = {}
    for entry in result.pop("env_vars", []):
        name = entry.get("name") if isinstance(entry, dict) else entry
        if isinstance(entry, dict) and entry.get("source", "local") != "local":
            raise GatewayError("invalid_config", "Remote environment variables require a remote executor")
        env[name] = environment_value(name)
    if env:
        result["env"] = env | result.get("env", {})
    return result
