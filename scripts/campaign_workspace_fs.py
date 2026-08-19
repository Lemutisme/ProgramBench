"""Owned filesystem identities and immutable workspace artifacts."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


class WorkspaceFilesystemError(RuntimeError):
    """Raised when owned filesystem evidence no longer names the same object."""


@dataclass(frozen=True)
class PathIdentity:
    name: str
    path: Path
    device: int
    inode: int
    kind: str

    @classmethod
    def capture(cls, name: str, path: Path) -> "PathIdentity":
        value = Path(path)
        if value.is_symlink() or value.resolve(strict=True) != value:
            raise WorkspaceFilesystemError(f"{name} must be a canonical non-symlink path")
        status = value.stat()
        kind = "directory" if value.is_dir() else "file" if value.is_file() else "other"
        return cls(name, value, status.st_dev, status.st_ino, kind)

    def verify(self) -> None:
        try:
            current = PathIdentity.capture(self.name, self.path)
        except (OSError, WorkspaceFilesystemError) as error:
            raise WorkspaceFilesystemError(f"{self.name} identity changed") from error
        if current != self:
            raise WorkspaceFilesystemError(f"{self.name} identity changed")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": str(self.path),
            "device": self.device,
            "inode": self.inode,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "PathIdentity":
        if (
            not isinstance(raw, dict)
            or set(raw) != {"name", "path", "device", "inode", "kind"}
            or not isinstance(raw["name"], str)
            or not isinstance(raw["path"], str)
            or not isinstance(raw["kind"], str)
            or type(raw["device"]) is not int
            or type(raw["inode"]) is not int
            or raw["kind"] not in {"directory", "file"}
        ):
            raise WorkspaceFilesystemError("filesystem identity is invalid")
        return cls(raw["name"], Path(raw["path"]), raw["device"], raw["inode"], raw["kind"])


def snapshot_binary(source: Path, destination: Path, expected_sha256: str) -> PathIdentity:
    """Atomically publish one read-only binary snapshot after a double source hash."""
    if _sha256(source) != expected_sha256:
        raise WorkspaceFilesystemError("installed binary changed before snapshot")
    destination.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o444 | (source.stat().st_mode & 0o111)
    if destination.exists():
        if (
            destination.is_symlink()
            or _sha256(destination) != expected_sha256
            or destination.stat().st_mode & 0o777 != mode
        ):
            raise WorkspaceFilesystemError("owned binary snapshot conflicts with installation evidence")
        return PathIdentity.capture("binary", destination)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as target:
            shutil.copyfileobj(source_handle, target, 1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        if _sha256(source) != expected_sha256 or _sha256(Path(temporary)) != expected_sha256:
            raise WorkspaceFilesystemError("installed binary changed during snapshot")
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
        _sync_directory(destination.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return PathIdentity.capture("binary", destination)


def sync_tree(root: Path) -> None:
    """Fsync every regular file and directory in one owned tree."""
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        descriptor = os.open(str(path), os.O_RDONLY | (getattr(os, "O_DIRECTORY", 0) if path.is_dir() else 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    _sync_directory(root)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
