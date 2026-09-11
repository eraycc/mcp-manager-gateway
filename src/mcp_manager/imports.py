"""Read-only source adapters and deterministic import normalization.

Source files never become the gateway's live configuration. Only explicitly
committed normalized entries are stored in its own encrypted catalog.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml

from .transports import validate_config

CHANNELS = {"generic", "codex", "claude", "dsh"}
MAX_SOURCE_BYTES = 4 * 1024 * 1024
META = {"name", "slug", "description", "tags", "transport", "type", "mode", "isolation",
        "enabled", "disabled", "tier", "id", "notes", "serverName"}
DSH_STATE = {"tools", "disabledTools", "rawConfig", "managed", "registeredAt", "lastLoadAt",
             "lastSyncAt", "systemEntryId", "serverVersion", "serverTitle", "lastError"}


@dataclass(frozen=True)
class CordisScript:
    """An inert marker: Cordis JavaScript is never evaluated by this importer."""
    tag: str


class CordisLoader(yaml.SafeLoader):
    pass


def _cordis_script(loader, node):
    return CordisScript(node.tag)


for _tag in ("!js", "tag:yaml.org,2002:js"):
    CordisLoader.add_constructor(_tag, _cordis_script)


def _script_path(value, path="config"):
    if isinstance(value, CordisScript):
        return path
    if isinstance(value, dict):
        for key, child in value.items():
            found = _script_path(child, path + "." + str(key))
            if found:
                return found
    if isinstance(value, list):
        for index, child in enumerate(value):
            found = _script_path(child, path + "[" + str(index) + "]")
            if found:
                return found
    return None


def channel_name(channel):
    if channel not in CHANNELS:
        raise ValueError("Unsupported import channel")
    return channel


def slug_for(name):
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) and "__" not in name:
        return name
    safe = re.sub(r"[^A-Za-z0-9-]+", "_", name).strip("_") or "mcp"
    return safe[:51] + "_" + hashlib.sha256(name.encode()).hexdigest()[:12]


def _mapping(value):
    if not isinstance(value, dict):
        raise ValueError("MCP server collection must be an object")
    return [(str(name), raw) for name, raw in value.items()]


def _dsh_entries(value):
    if isinstance(value, dict) and "entries" in value:
        if not isinstance(value["entries"], list):
            raise ValueError("DSH registry entries must be an array")
        return [(str(x.get("name", "")) if isinstance(x, dict) else "", x) for x in value["entries"]]
    # Cordis patch inserts are explicit: do not recursively interpret unrelated plugins.
    if isinstance(value, list) and any(isinstance(x, dict) and "insert" in x for x in value):
        found = []
        for block in value:
            if not isinstance(block, dict):
                continue
            entries = block.get("insert", [])
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and entry.get("name") == "@deepseek-ai/dsh-mcp-client":
                    config = entry.get("config", {})
                    if isinstance(config, dict):
                        found.append((str(config.get("serverName", entry.get("id", ""))),
                                      dict(config, disabled=entry.get("disabled", False))))
        return found
    return None


def _source(data, channel):
    if isinstance(data, str):
        if len(data.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError("Configuration exceeds 4 MiB")
        data = data.lstrip("\ufeff")
        if channel == "codex":
            data = tomllib.loads(data)
        elif channel == "dsh":
            # Cordis script tags become inert markers; unrelated plugin fields are ignored.
            data = yaml.load(data, Loader=CordisLoader)
        else:
            data = json.loads(data)
    if channel == "codex":
        if not isinstance(data, dict) or "mcp_servers" not in data:
            raise ValueError("Codex configuration must contain [mcp_servers.<name>] entries")
        return _mapping(data["mcp_servers"])
    if channel == "dsh":
        entries = _dsh_entries(data)
        if entries is not None:
            return entries
    if isinstance(data, list):
        return [(str(x.get("name", "")) if isinstance(x, dict) else "", x) for x in data]
    if not isinstance(data, dict):
        raise ValueError("Import must be an object or array")
    if any(k in data for k in ("config", "command", "url")):
        return [(str(data.get("name", "")), data)]
    if channel == "claude" and "projects" in data:
        found = _mapping(data.get("mcpServers", {}))
        if not isinstance(data["projects"], dict):
            raise ValueError("Claude projects must be an object")
        for project, config in data["projects"].items():
            if not isinstance(config, dict):
                continue
            for name, raw in _mapping(config.get("mcpServers", {})):
                # Preserve same-named servers from distinct project scopes.
                if isinstance(raw, dict):
                    raw = dict(raw, name=name + " · " + project,
                               slug=slug_for(name + "@" + project))
                found.append((name, raw))
        return found
    return _mapping(data.get("mcpServers", data.get("mcp_servers", data)))


def normalize_import(data, channel="generic"):
    channel_name(channel)
    items, errors = [], []
    try:
        source = _source(data, channel)
    except (ValueError, TypeError, yaml.YAMLError, RecursionError) as exc:
        # Parser errors can contain source snippets; return a location, never raw secrets.
        return {"items": [], "errors": [{"name": "", "error": "配置解析失败：" + type(exc).__name__
                                          + ("，请检查配置格式或导入渠道" if not isinstance(exc, ValueError)
                                             else "，请检查配置格式、大小或导入渠道")}]}
    for name, raw in source:
        try:
            if not isinstance(raw, dict):
                raise ValueError("Service entry must be an object")
            script = _script_path(raw)
            if script:
                raise ValueError(script + " 使用 Cordis !js 表达式，请改为静态值或环境变量引用后导入")
            item = copy.deepcopy(raw)
            if "config" in item and not isinstance(item["config"], dict):
                raise ValueError("config must be an object")
            config = copy.deepcopy(item.get("config", {k: v for k, v in item.items() if k not in META}))
            if channel == "dsh":
                for key in DSH_STATE:
                    config.pop(key, None)
                if item.get("disabledTools"):
                    config["disabled_tools"] = item["disabledTools"]
                config["environment_expansion"] = "dsh"
            if channel == "claude":
                config["environment_expansion"] = "claude"
                if "timeout" in config:
                    config["call_timeout"] = float(config.pop("timeout")) / 1000
            transport = item.get("transport", item.get("type")) or (
                "stdio" if config.get("command") else "streamable-http")
            transport = {"http": "streamable-http", "streamableHttp": "streamable-http",
                         "streamable_http": "streamable-http"}.get(transport, transport)
            for old, new in (("startup_timeout_sec", "startup_timeout"), ("tool_timeout_sec", "call_timeout"),
                             ("http_headers", "headers"), ("env_http_headers", "env_headers")):
                if old in config:
                    config[new] = config.pop(old)
            if "startup_timeout_ms" in config:
                config["startup_timeout"] = float(config.pop("startup_timeout_ms")) / 1000
            if config.get("bearer_token_env_var"):
                config["auth"] = {"type": "bearer", "token_env": config.pop("bearer_token_env_var")}
            # Client login caches cannot be migrated as server configuration.
            if isinstance(config.get("auth"), str):
                if config["auth"] not in {"oauth", "chatgpt"}:
                    raise ValueError("Unsupported source authentication")
                config["source_auth"] = config.pop("auth")
            if config.get("experimental_environment") == "remote":
                raise ValueError("Remote executor configuration needs a local command or URL")
            if config.get("http_headers_helper"):
                raise ValueError("Replace http_headers_helper with headers or environment header references")
            name = str(item.get("name") or name).strip()
            if not 1 <= len(name) <= 128:
                raise ValueError("Service name must contain 1–128 characters")
            disabled = item.get("disabled") is True or item.get("enabled") is False
            mode = item.get("mode", {"on-demand": "lazy"}.get(item.get("tier"), item.get("tier", "lazy")))
            if disabled:
                mode = "disabled"
            isolation = item.get("isolation", "service")
            if mode not in {"lazy", "eager", "disabled"} or isolation not in {"service", "user", "session"}:
                raise ValueError("Invalid lifecycle policy")
            validate_config(transport, config)
            values = {k: item[k] for k in ("description", "tags") if k in item}
            items.append(values | {"name": name, "slug": slug_for(str(item.get("slug") or name)),
                                   "transport": transport, "config": config,
                                   "mode": mode, "isolation": isolation})
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            errors.append({"name": name, "error": str(exc)})
    return {"items": items, "errors": errors}


def fingerprint(item):
    config = copy.deepcopy(item["config"])
    # Defaults and runtime tuning do not identify a different downstream service.
    for key in ("startup_timeout", "call_timeout", "stop_timeout", "queue_timeout", "concurrency",
                "idle_seconds", "source_auth"):
        config.pop(key, None)
    for key in ("args", "env", "headers", "env_headers", "env_vars"):
        if not config.get(key):
            config.pop(key, None)
    if config.get("auth", {}).get("type", "none") == "none":
        config.pop("auth", None)
    if config.get("verify_tls", True):
        config.pop("verify_tls", None)
    # Auth, environment, request mapping, isolation and tool restrictions remain significant.
    return hashlib.sha256(json.dumps([item["transport"], item.get("isolation", "service"), config],
                                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def deduplicate(items, existing):
    seen = {fingerprint(item): item["name"] for item in existing}
    kept, duplicates = [], []
    for item in items:
        key = fingerprint(item)
        if key in seen:
            duplicates.append({"name": item["name"], "existing_name": seen[key], "reason": "相同连接配置"})
        else:
            kept.append(item)
            seen[key] = item["name"]
    return kept, duplicates


def source_paths(channel):
    channel_name(channel)
    home = Path.home()
    if channel == "codex":
        return [Path(os.environ.get("CODEX_HOME", home / ".codex")).expanduser() / "config.toml"]
    if channel == "claude":
        paths = [home / ".claude.json", Path.cwd() / ".mcp.json"]
        if os.environ.get("CLAUDE_CONFIG_DIR"):
            paths.insert(0, Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / ".claude.json")
        return list(dict.fromkeys(paths))
    if channel == "dsh":
        root = Path(os.environ.get("DSH_HOME", home / ".dsh")).expanduser()
        return [root / "skill-mcp-manager" / "registry.json",
                *sorted((root / "profiles").glob("*/cordis.patch.yml"))[:64]]
    return []


def _read_source(path):
    # Optional trusted Node channel on managed-encryption Windows hosts. Fixed
    # code + argv, never a shell command or an arbitrary Web-supplied file path.
    node = shutil.which("node") if sys.platform == "win32" else None
    if node:
        script = """const fs=require('fs');const p=process.argv[1];
if(fs.statSync(p).size>67108864)process.exit(2);
const s=fs.readFileSync(p,'utf8');if(Buffer.byteLength(s)>4194304)process.exit(2);
process.stdout.write(s);"""
        result = subprocess.run([node, "-e", script, str(path)], capture_output=True, timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise ValueError("无法读取配置文件；请使用文件上传或粘贴")
        content = result.stdout.decode("utf-8-sig")
    else:
        with path.open("rb") as source:
            content = source.read(MAX_SOURCE_BYTES + 1).decode("utf-8-sig")
    if len(content.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError("Configuration exceeds 4 MiB")
    return content


async def scan_sources(channel):
    paths = source_paths(channel)
    sources, errors = [], []
    for path in paths:
        try:
            if not path.is_file():
                continue
            content = await asyncio.to_thread(_read_source, path)
            sources.append({"path": str(path), "content": content})
        except (OSError, ValueError, subprocess.TimeoutExpired):
            errors.append({"path": str(path), "error": "配置读取失败，请上传文件或粘贴原配置"})
    if not sources and not errors:
        errors.append({"path": "", "error": "未找到此渠道的默认配置；可上传文件或粘贴原配置"})
    return {"sources": sources, "errors": errors}
