"""Bounded JSON compatibility responses; never silently truncate a full contract."""
import asyncio
import json

from fastapi.encoders import jsonable_encoder

from .runtime import GatewayError


async def ensure_json_budget(value, *, max_bytes=131072):
    def check():
        encoder = json.JSONEncoder(
            ensure_ascii=False, allow_nan=False, separators=(",", ":"), default=jsonable_encoder
        )
        size = 0
        for chunk in encoder.iterencode(value):
            size += len(chunk.encode("utf-8"))
            if size > max_bytes:
                raise GatewayError(
                    "catalog_payload_too_large",
                    "Catalog JSON exceeds the response budget; use capability summary pages or a smaller page size",
                    {"max_bytes": max_bytes},
                )
    await asyncio.to_thread(check)
