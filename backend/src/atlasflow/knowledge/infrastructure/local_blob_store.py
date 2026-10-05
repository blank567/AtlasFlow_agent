from __future__ import annotations

import asyncio
from pathlib import Path


class LocalBlobStore:
    """Content store constrained to one configured E-drive root."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        normalized = key.replace("\\", "/").lstrip("/")
        candidate = (self.root / normalized).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("blob key escapes configured root")
        return candidate

    async def put(self, key: str, content: bytes) -> None:
        path = self._path(key)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(content)
            temporary.replace(path)

        await asyncio.to_thread(write)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            await asyncio.to_thread(path.unlink)


class MemoryBlobStore:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def put(self, key: str, content: bytes) -> None:
        self.data[key] = content

    async def get(self, key: str) -> bytes:
        return self.data[key]

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)
