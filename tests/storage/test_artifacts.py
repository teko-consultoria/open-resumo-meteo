from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from resumo_meteo.storage import ArtifactChecksumError, ArtifactStore, UnsafeArtifactPath


@pytest.mark.parametrize(
    "value",
    ["", "/absolute/file", "../escape", "model/../escape", "model\\file", "model//file"],
)
def test_unsafe_artifact_paths_are_rejected(tmp_path: Path, value: str) -> None:
    store = ArtifactStore(tmp_path)

    with pytest.raises(UnsafeArtifactPath):
        store.resolve(value)


def test_checksum_is_verified_before_returning_file(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    path = store.resolve("normalized/gfs.csv.gz")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"meteorology")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    assert store.verify("normalized/gfs.csv.gz", expected_sha256=digest) == path
    with pytest.raises(ArtifactChecksumError):
        store.verify("normalized/gfs.csv.gz", expected_sha256="0" * 64)


def test_symlink_cannot_escape_artifact_root(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not remove", encoding="utf-8")
    (root / "link").symlink_to(outside)

    with pytest.raises(UnsafeArtifactPath):
        ArtifactStore(root).resolve("link")


def test_stale_staging_cleanup_only_removes_old_real_directories(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    staging = store.staging_root()
    stale = staging / "stale-worker"
    active = staging / "active-worker"
    stale.mkdir(parents=True)
    active.mkdir()
    (stale / "raw.grib2").write_bytes(b"temporary")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = staging / "linked-worker"
    link.symlink_to(outside, target_is_directory=True)
    old_timestamp = (datetime.now(UTC) - timedelta(hours=25)).timestamp()
    os.utime(stale, (old_timestamp, old_timestamp))

    result = store.cleanup_stale_staging_directories()

    assert [(item.storage_path, item.status) for item in result] == [("stale-worker", "deleted")]
    assert not stale.exists()
    assert active.exists()
    assert outside.exists()
    assert link.is_symlink()


def test_remove_tree_rejects_files_and_removes_only_artifact_subtree(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    target = store.resolve("attempts/run-1")
    target.mkdir(parents=True)
    (target / "manifest.json").write_text("{}", encoding="utf-8")
    file_path = store.resolve("attempts/file.txt")
    file_path.write_text("keep", encoding="utf-8")

    assert store.remove_tree("attempts/run-1") is True
    assert not target.exists()
    assert file_path.read_text(encoding="utf-8") == "keep"
    with pytest.raises(NotADirectoryError):
        store.remove_tree("attempts/file.txt")
