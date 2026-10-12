from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import zstandard


class RawStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, sha: str, source: str) -> Path:
        return self.root / source / sha[:2] / sha[2:4] / f"{sha}.zst"

    def exists(self, sha256: str, *, source: str) -> bool:
        return self._path(sha256, source).exists()

    def put(self, data: bytes, *, source: str) -> str:
        sha = hashlib.sha256(data).hexdigest()
        path = self._path(sha, source)
        if path.exists():
            return sha
        path.parent.mkdir(parents=True, exist_ok=True)
        compressed = zstandard.ZstdCompressor(level=10).compress(data)
        fd, tmp = tempfile.mkstemp(dir=path.parent)
        with os.fdopen(fd, "wb") as f:
            f.write(compressed)
        os.replace(tmp, path)
        return sha

    def get(self, sha256: str, *, source: str) -> bytes:
        path = self._path(sha256, source)
        return zstandard.ZstdDecompressor().decompress(path.read_bytes())
