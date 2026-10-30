"""Credential-free, immutable catalog references shared across database backends."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogScope:
    server_id: str
    owner: str
    revision: int
    credential_generation: str


@dataclass(frozen=True)
class ArtifactRef:
    id: str
    size_bytes: int
    sha256: str
    namespace: str = "cache"


@dataclass(frozen=True)
class GenerationRef:
    id: str
    scope: CatalogScope
    digest: str
    status: str


@dataclass(frozen=True)
class CatalogPage:
    items: list[dict]
    total: int
    next_cursor: str | None
    generation: str


@dataclass(frozen=True)
class ToolRef:
    generation: str
    original_name: str
    gateway_name: str
    schema_hash: str
    artifact_id: str
