from __future__ import annotations

from typing import Protocol


class BlobStore(Protocol):
    async def put(self, key: str, content: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...
