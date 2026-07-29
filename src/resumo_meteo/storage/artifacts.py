"""Safe access and retention operations for local artifact files."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath


class UnsafeArtifactPath(ValueError):
    pass


class ArtifactChecksumError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TemporaryDirectoryCleanupResult:
    """Outcome of removing one abandoned staging directory."""

    storage_path: str
    status: str
    error: str = ""


def normalize_artifact_path(value: str | os.PathLike[str]) -> str:
    """Return a canonical POSIX-relative path or reject it.

    Backslashes are rejected instead of interpreted because accepting them
    would make traversal behavior platform-dependent.
    """

    raw = os.fspath(value).strip()
    if not raw or "\x00" in raw or "\\" in raw:
        raise UnsafeArtifactPath("artifact path must be a non-empty POSIX relative path")
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise UnsafeArtifactPath(f"unsafe artifact path: {raw}")
    normalized = candidate.as_posix()
    if normalized != raw:
        raise UnsafeArtifactPath(f"artifact path is not canonical: {raw}")
    return normalized


def validate_sha256(value: str, *, allow_empty: bool = True) -> str:
    digest = str(value or "").strip().lower()
    if not digest and allow_empty:
        return ""
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("sha256 must contain exactly 64 lowercase hexadecimal characters")
    return digest


@dataclass(frozen=True, slots=True)
class ArtifactStore:
    root: Path

    def __init__(self, root: str | os.PathLike[str]):
        object.__setattr__(self, "root", Path(root).expanduser().resolve())

    def ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def resolve(self, relative_path: str, *, must_exist: bool = False) -> Path:
        normalized = normalize_artifact_path(relative_path)
        path = (self.root / normalized).resolve(strict=False)
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise UnsafeArtifactPath(f"artifact path escapes root: {relative_path}") from exc
        if must_exist and not path.is_file():
            raise FileNotFoundError(path)
        return path

    def verify(self, relative_path: str, *, expected_sha256: str = "") -> Path:
        path = self.resolve(relative_path, must_exist=True)
        digest = validate_sha256(expected_sha256)
        if digest:
            with path.open("rb") as file_obj:
                actual = hashlib.file_digest(file_obj, "sha256").hexdigest()
            if actual != digest:
                raise ArtifactChecksumError(f"checksum mismatch for {relative_path}")
        return path

    def unlink(self, relative_path: str, *, missing_ok: bool = True) -> bool:
        path = self.resolve(relative_path)
        if not path.exists():
            if missing_ok:
                return False
            raise FileNotFoundError(path)
        if not path.is_file() and not path.is_symlink():
            raise IsADirectoryError(path)
        path.unlink()
        self._prune_empty_parents(path.parent)
        return True

    def remove_tree(self, relative_path: str, *, missing_ok: bool = True) -> bool:
        """Safely remove a directory owned by a failed ingestion attempt."""

        path = self.resolve(relative_path)
        if not path.exists():
            if missing_ok:
                return False
            raise FileNotFoundError(path)
        if path.is_symlink() or not path.is_dir():
            raise NotADirectoryError(path)
        shutil.rmtree(path)
        self._prune_empty_parents(path.parent)
        return True

    def staging_root(self) -> Path:
        """Return the only directory used for interruptible local staging."""

        return self.ensure_root() / ".tmp"

    def cleanup_stale_staging_directories(
        self,
        *,
        older_than: timedelta = timedelta(hours=24),
        now_utc: datetime | None = None,
        dry_run: bool = False,
    ) -> tuple[TemporaryDirectoryCleanupResult, ...]:
        """Remove old direct children of the dedicated staging directory.

        This never recursively scans the artifact store and never follows
        symlinks, so normalized and audit artifacts cannot be selected.
        """

        if older_than.total_seconds() < 0:
            raise ValueError("older_than must not be negative")
        now = now_utc or datetime.now(UTC)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now_utc must be timezone-aware")
        threshold = now.astimezone(UTC).timestamp() - older_than.total_seconds()
        staging = self.root / ".tmp"
        if not staging.exists():
            return ()
        if staging.is_symlink() or not staging.is_dir():
            raise UnsafeArtifactPath("artifact staging root must be a directory")

        results: list[TemporaryDirectoryCleanupResult] = []
        for entry in staging.iterdir():
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                results.append(
                    TemporaryDirectoryCleanupResult(
                        entry.name, "error", f"{type(exc).__name__}: {exc}"
                    )
                )
                continue
            if not stat.S_ISDIR(entry_stat.st_mode) or entry.is_symlink():
                continue
            if entry_stat.st_mtime > threshold:
                continue
            try:
                if not dry_run:
                    shutil.rmtree(entry)
                results.append(
                    TemporaryDirectoryCleanupResult(
                        entry.name, "would_delete" if dry_run else "deleted"
                    )
                )
            except OSError as exc:
                results.append(
                    TemporaryDirectoryCleanupResult(
                        entry.name, "error", f"{type(exc).__name__}: {exc}"
                    )
                )
        return tuple(results)

    def _prune_empty_parents(self, directory: Path) -> None:
        while directory != self.root:
            try:
                directory.rmdir()
            except OSError:
                return
            directory = directory.parent
