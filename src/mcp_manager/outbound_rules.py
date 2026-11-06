"""Manual targets: exact literals first, then IP-only CIDRs, then subdomain wildcards."""
import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Rule:
    priority: int
    value: object
    scheme: str = ""
    port: int | None = None


def canonical_host(value):
    value = value.lower().rstrip(".")
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        if not value or re.search(r"[\s/@?#\\:*]", value):
            raise ValueError("名单中的 hostname 格式不正确") from None
        try:
            return value.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError("名单中的 hostname 格式不正确") from None


def parse_rule(value):
    if value == "*":
        return Rule(2, None)
    if "/" in value and "://" not in value:
        try:
            return Rule(1, ipaddress.ip_network(value, strict=False))
        except ValueError:
            raise ValueError("名单中的 CIDR 格式不正确") from None
    literal = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    try:
        return Rule(0, ipaddress.ip_address(literal))
    except ValueError:
        pass
    parsed = urlsplit(value if "://" in value else "//" + value)
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or (parsed.scheme and parsed.scheme not in {"http", "https"})):
        raise ValueError("名单只支持 hostname、IP、CIDR、*. 通配或 HTTP(S) origin")
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("名单端口须为 1–65535")
    host = parsed.hostname
    wildcard = host.startswith(("*.", "."))
    if wildcard:
        host = host[2:] if host.startswith("*.") else host[1:]
    host = canonical_host(host)
    if wildcard and not isinstance(host, str):
        raise ValueError("*. 通配仅支持域名；IP 网段请使用 CIDR")
    return Rule(2 if wildcard else 0, host, parsed.scheme, port)


def matches(url, entries, *, no_proxy=False):
    target = urlsplit(str(url))
    host = canonical_host(target.hostname or "")
    literal_ip = host if not isinstance(host, str) else None
    rules = []
    for entry in entries:
        try:
            rules.append(parse_rule(entry))
        except ValueError:
            if not no_proxy:
                raise
            # Environment conventions vary: ignore unrecognized exclusions.
    for rule in sorted(rules, key=lambda item: item.priority):
        if rule.scheme and rule.scheme != target.scheme:
            continue
        if rule.port and rule.port != (target.port or (443 if target.scheme == "https" else 80)):
            continue
        if rule.priority == 0:
            if host == rule.value:
                return True
            if no_proxy and isinstance(host, str) and isinstance(rule.value, str) and host.endswith("." + rule.value):
                return True
        elif rule.priority == 1:
            if literal_ip is not None and literal_ip.version == rule.value.version and literal_ip in rule.value:
                return True
        elif rule.value is None or (isinstance(host, str) and host.endswith("." + rule.value)):
            return True
    return False
