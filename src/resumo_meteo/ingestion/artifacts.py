"""Serializable artifact contracts with atomic persistence and safe cleanup."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def sha256_path(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def managed_workdir(
    *,
    temporary_root: str | Path | None = None,
    prefix: str = "resumo-meteo-",
) -> Iterator[Path]:
    """Yield an isolated directory and remove it on success or failure."""

    parent = Path(temporary_root).resolve() if temporary_root else None
    if parent:
        parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def atomic_write_bytes(path: str | Path, content: bytes) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as file_obj:
            file_obj.write(content)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


__all__ = [
    "atomic_write_bytes",
    "managed_workdir",
    "sha256_path",
]
