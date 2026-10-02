"""Validation for the browser translation integration."""
import re
from copy import deepcopy
from urllib.parse import urlparse

DEFAULT_TRANSLATION_CONFIG = {
    "enabled": False,
    "local_language": "chinese_simplified",
    "target_language": "english",
    "service": "client.edge",
    "custom_host": "",
    "sse_enabled": False,
    "ignore": {"class": [], "id": [], "tag": ["code", "pre"], "text": []},
    "terminology": [],
    "url_control": False,
    "url_parameter": "language",
    "dynamic_content": True,
    "whole_page": True,
    "translate_local": False,
    "queue_enabled": True,
}
_ALLOWED_KEYS = set(DEFAULT_TRANSLATION_CONFIG)
_BOOL_KEYS = {
    "enabled", "sse_enabled", "url_control", "dynamic_content",
    "whole_page", "translate_local", "queue_enabled",
}
_SERVICES = {"client.edge", "translate.service", "giteeAI", "custom"}
_LANGUAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,47}$")
_PARAMETER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


def _list(value, name, *, item_limit=200, length=256):
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r"[\n,]+", value)
    if not isinstance(value, list) or len(value) > item_limit:
        raise ValueError(f"{name} must be a list with at most {item_limit} items")
    result = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{name} items must be strings")  # noqa: TRY004
        item = item.strip()
        if not item:
            continue
        if len(item) > length:
            raise ValueError(f"{name} items must contain at most {length} characters")
        if item not in result:
            result.append(item)
    return result


def normalize_translation_config(value):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - _ALLOWED_KEYS:
        raise ValueError("translation_config contains unknown fields")
    result = deepcopy(DEFAULT_TRANSLATION_CONFIG)
    result.update({key: value[key] for key in value if key not in {"ignore", "terminology"}})
    for key in _BOOL_KEYS:
        if not isinstance(result[key], bool):
            raise ValueError(f"translation_config.{key} must be a boolean")  # noqa: TRY004
    for key in ("local_language", "target_language"):
        if not isinstance(result[key], str) or not _LANGUAGE.fullmatch(result[key]):
            raise ValueError(f"translation_config.{key} is invalid")
    if result["service"] not in _SERVICES:
        raise ValueError("translation_config.service is invalid")
    host = result.get("custom_host", "")
    if not isinstance(host, str) or len(host) > 2048:
        raise ValueError("translation_config.custom_host is invalid")
    host = host.strip().rstrip("/")
    if result["service"] == "custom":
        parsed = urlparse(host)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("translation_config.custom_host must be an HTTP(S) URL without credentials")
    result["custom_host"] = host
    if result["sse_enabled"] and result["service"] != "custom":
        raise ValueError("translation_config.sse_enabled requires the custom service")
    parameter = result.get("url_parameter", "")
    if not isinstance(parameter, str) or not _PARAMETER.fullmatch(parameter):
        raise ValueError("translation_config.url_parameter is invalid")
    ignores = value.get("ignore", result["ignore"])
    if not isinstance(ignores, dict) or set(ignores) - {"class", "id", "tag", "text"}:
        raise ValueError("translation_config.ignore is invalid")
    result["ignore"] = {
        key: _list(ignores.get(key, []), f"translation_config.ignore.{key}", length=500)
        for key in ("class", "id", "tag", "text")
    }
    terminology = value.get("terminology", [])
    if not isinstance(terminology, list) or len(terminology) > 200:
        raise ValueError("translation_config.terminology must contain at most 200 items")
    result["terminology"] = []
    for item in terminology:
        if not isinstance(item, dict) or set(item) - {"source", "target"}:
            raise ValueError("translation_config.terminology items are invalid")
        source, target = item.get("source", ""), item.get("target", "")
        if not isinstance(source, str) or not isinstance(target, str):
            raise ValueError("translation terminology must contain strings")  # noqa: TRY004
        source, target = source.strip(), target.strip()
        if not source or not target:
            continue
        if len(source) > 500 or len(target) > 500:
            raise ValueError("translation terminology items must contain at most 500 characters")
        pair = {"source": source, "target": target}
        if pair not in result["terminology"]:
            result["terminology"].append(pair)
    return result
