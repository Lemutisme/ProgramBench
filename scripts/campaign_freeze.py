"""Bounded, descriptor-safe candidate inventory and freeze copying."""

from __future__ import annotations

import hashlib
import errno
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Callable

from scripts.campaign_vanilla_http import VanillaError

MAX_FREEZE_FILES = 100_000
MAX_FREEZE_BYTES = 4 * 1024 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 10_000
MAX_INVENTORY_SECONDS = 300.0


class CredentialLeakError(VanillaError):
    """Raised before publication when credential material is observed."""


def _credential_values(names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(secret for name in names if (secret := os.environ.get(name)))


def _open_directory(path: Path) -> int:
    try:
        return os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EPERM):
            raise VanillaError(f"workspace directory unreadable (errno={error.errno})") from error
        raise VanillaError("workspace directory identity changed") from error


def _scan_names(descriptor: int, deadline: float, count: list[int]) -> list[str]:
    names: list[str] = []
    with os.scandir(descriptor) as iterator:
        for entry in iterator:
            if time.monotonic() > deadline:
                raise VanillaError("workspace freeze enumeration exceeded time limit")
            if len(names) >= MAX_DIRECTORY_ENTRIES:
                raise VanillaError("workspace freeze directory entry count exceeds limit")
            count[0] += 1
            if count[0] > MAX_FREEZE_FILES:
                raise VanillaError("workspace freeze file count exceeds limit")
            names.append(entry.name)
    names.sort()
    return names


def _open_verified(parent: int, name: str, expected: os.stat_result, *, directory: bool) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EPERM):
            raise VanillaError(f"workspace entry unreadable during freeze (errno={error.errno})") from error
        raise VanillaError("workspace entry identity changed during freeze") from error
    current = os.fstat(descriptor)
    expected_kind = stat.S_ISDIR(expected.st_mode) if directory else stat.S_ISREG(expected.st_mode)
    current_kind = stat.S_ISDIR(current.st_mode) if directory else stat.S_ISREG(current.st_mode)
    if not expected_kind or not current_kind or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
        os.close(descriptor)
        raise VanillaError("workspace entry identity changed during freeze")
    return descriptor, current


def _hash_descriptor(descriptor: int, deadline: float, remaining: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        if time.monotonic() > deadline:
            raise VanillaError("workspace freeze hashing exceeded time limit")
        size += len(chunk)
        if size > remaining:
            raise VanillaError("workspace freeze byte count exceeds limit")
        digest.update(chunk)
    return digest.hexdigest(), size


def _inventory_item(
    relative: str,
    status: os.stat_result,
    kind: str,
    *,
    read_only_modes: bool,
    target: str | None = None,
    size: int | None = None,
    digest: str | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "path_sha256": hashlib.sha256(relative.encode()).hexdigest(),
        "device": status.st_dev,
        "inode": status.st_ino,
        "mode": stat.S_IMODE(status.st_mode) & (~0o222 if read_only_modes and kind != "symlink" else 0o777),
        "kind": kind,
    }
    if target is not None:
        item["target_sha256"] = hashlib.sha256(target.encode()).hexdigest()
    if size is not None:
        item["size"] = size
    if digest is not None:
        item["sha256"] = digest
    return item


def _inventory(
    root: Path,
    *,
    excluded: tuple[str, ...] = (),
    read_only_modes: bool = False,
) -> list[dict[str, object]]:
    if root.is_symlink() or not root.is_dir():
        raise VanillaError("workspace candidate must be a real directory")
    result: list[dict[str, object]] = []
    total = 0
    count = [1]
    deadline = time.monotonic() + MAX_INVENTORY_SECONDS
    root_fd = _open_directory(root)
    frames: list[list[object]] = []
    try:
        result.append(_inventory_item(".", os.fstat(root_fd), "directory", read_only_modes=read_only_modes))
        frames.append([root_fd, ".", _scan_names(root_fd, deadline, count), 0])
        root_fd = -1
        while frames:
            descriptor, relative, names, index = frames[-1]
            if not isinstance(descriptor, int) or not isinstance(relative, str) or not isinstance(names, list):
                raise VanillaError("workspace inventory traversal state is invalid")
            if index >= len(names):
                os.close(descriptor)
                frames.pop()
                continue
            name = names[index]
            frames[-1][3] = index + 1
            child_relative = name if relative == "." else f"{relative}/{name}"
            if any(child_relative == value or child_relative.startswith(value + "/") for value in excluded):
                continue
            status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(status.st_mode):
                result.append(
                    _inventory_item(
                        child_relative,
                        status,
                        "symlink",
                        read_only_modes=read_only_modes,
                        target=os.readlink(name, dir_fd=descriptor),
                    )
                )
                continue
            if stat.S_ISDIR(status.st_mode):
                child_fd, current = _open_verified(descriptor, name, status, directory=True)
                result.append(_inventory_item(child_relative, current, "directory", read_only_modes=read_only_modes))
                frames.append([child_fd, child_relative, _scan_names(child_fd, deadline, count), 0])
                continue
            if not stat.S_ISREG(status.st_mode):
                raise VanillaError("workspace contains an unsupported filesystem object")
            child_fd, current = _open_verified(descriptor, name, status, directory=False)
            try:
                digest, size = _hash_descriptor(child_fd, deadline, MAX_FREEZE_BYTES - total)
            finally:
                os.close(child_fd)
            total += size
            result.append(
                _inventory_item(
                    child_relative,
                    current,
                    "file",
                    read_only_modes=read_only_modes,
                    size=size,
                    digest=digest,
                )
            )
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        for frame in frames:
            if isinstance(frame[0], int):
                try:
                    os.close(frame[0])
                except OSError:
                    pass
    return result


def _make_read_only(root: Path) -> None:
    count = 1
    deadline = time.monotonic() + MAX_INVENTORY_SECONDS
    stack = [(root, False)]
    while stack:
        if time.monotonic() > deadline:
            raise VanillaError("workspace freeze permission walk exceeded time limit")
        path, visited = stack.pop()
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode):
            continue
        if stat.S_ISDIR(status.st_mode) and not visited:
            stack.append((path, True))
            entries: list[Path] = []
            with os.scandir(path) as iterator:
                for entry in iterator:
                    if time.monotonic() > deadline:
                        raise VanillaError("workspace freeze permission walk exceeded time limit")
                    if len(entries) >= MAX_DIRECTORY_ENTRIES:
                        raise VanillaError("workspace freeze directory entry count exceeds limit")
                    count += 1
                    if count > MAX_FREEZE_FILES:
                        raise VanillaError("workspace freeze file count exceeds limit")
                    entries.append(Path(entry.path))
            entries.sort(key=lambda value: value.name, reverse=True)
            stack.extend((entry, False) for entry in entries)
            continue
        os.chmod(path, stat.S_IMODE(status.st_mode) & ~0o222)


def _snapshot_candidate(source: Path, destination: Path) -> None:
    count = [1]
    total = 0
    deadline = time.monotonic() + MAX_INVENTORY_SECONDS
    root_fd = _open_directory(source)
    frames: list[list[object]] = []
    try:
        frames.append([root_fd, destination, True, ".", _scan_names(root_fd, deadline, count), 0, None])
        root_fd = -1
        while frames:
            descriptor, target, root, relative, names, index, mode = frames[-1]
            if (
                not isinstance(descriptor, int)
                or not isinstance(target, Path)
                or not isinstance(relative, str)
                or not isinstance(names, list)
            ):
                raise VanillaError("workspace snapshot traversal state is invalid")
            if index >= len(names):
                if mode is not None:
                    os.chmod(target, mode)
                os.close(descriptor)
                frames.pop()
                continue
            name = names[index]
            frames[-1][5] = index + 1
            if root is True and name in {".git", "reference", ".programbench-cleanroom.json"}:
                continue
            child_relative = name if relative == "." else f"{relative}/{name}"
            child = target / name
            if os.path.lexists(child):
                raise VanillaError("credential-safe workspace path collision")
            status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(status.st_mode):
                link = os.readlink(name, dir_fd=descriptor)
                child.symlink_to(link)
                continue
            if stat.S_ISDIR(status.st_mode):
                child_fd, current = _open_verified(descriptor, name, status, directory=True)
                # Populate privately, then restore exact source mode bottom-up.
                # mkdir's mode alone is filtered by the controller's umask;
                # restoring a read-only mode before copying children also fails.
                child.mkdir(mode=0o700)
                frames.append([child_fd, child, False, child_relative,
                               _scan_names(child_fd, deadline, count), 0, stat.S_IMODE(current.st_mode)])
                continue
            if not stat.S_ISREG(status.st_mode):
                raise VanillaError("workspace contains an unsupported filesystem object")
            child_fd, current = _open_verified(descriptor, name, status, directory=False)
            try:
                size = _copy_descriptor(
                    child_fd,
                    child,
                    deadline,
                    MAX_FREEZE_BYTES - total,
                )
            finally:
                os.close(child_fd)
            total += size
            os.chmod(child, stat.S_IMODE(current.st_mode))
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        for frame in frames:
            if isinstance(frame[0], int):
                try:
                    os.close(frame[0])
                except OSError:
                    pass


def _copy_descriptor(
    descriptor: int,
    destination: Path,
    deadline: float,
    remaining: int,
) -> int:
    size = 0
    with destination.open("xb") as writer:
        while chunk := os.read(descriptor, 1024 * 1024):
            if time.monotonic() > deadline:
                raise VanillaError("workspace freeze copy exceeded time limit")
            size += len(chunk)
            if size > remaining:
                raise VanillaError("workspace freeze byte count exceeds limit")
            writer.write(chunk)
    return size


def _remove_tree(root: Path) -> None:
    def writable(function: Callable[[str], None], path: str, exception: object) -> None:
        target = Path(path)
        # Unlink/rmdir require the parent's write bit, not the child's. Do not
        # follow a symlink while cleaning an owned temporary evidence view.
        if target.parent != root.parent:
            os.chmod(target.parent, 0o700)
        if not target.is_symlink():
            os.chmod(target, 0o700)
        function(path)

    shutil.rmtree(root, onerror=writable)


def _verify_no_credentials(root: Path, names: tuple[str, ...], *, published_only: bool = True) -> None:
    secrets = tuple(secret.encode() for secret in _credential_values(names))
    if not secrets:
        return
    deadline = time.monotonic() + MAX_INVENTORY_SECONDS
    count = [1]
    total = 0
    root_fd = _open_directory(root)
    frames: list[list[object]] = []
    try:
        frames.append([root_fd, ".", _scan_names(root_fd, deadline, count), 0])
        root_fd = -1
        while frames:
            descriptor, relative, entries, index = frames[-1]
            if not isinstance(descriptor, int) or not isinstance(relative, str) or not isinstance(entries, list):
                raise VanillaError("credential scan traversal state is invalid")
            if index >= len(entries):
                os.close(descriptor)
                frames.pop()
                continue
            name = entries[index]
            frames[-1][3] = index + 1
            child_relative = name if relative == "." else f"{relative}/{name}"
            if (
                published_only
                and relative == "."
                and name
                not in {
                    "state.json",
                    "vanilla-execution.json",
                    "vanilla-progress.json",
                    "trajectory.json",
                    "usage.json",
                    "workspace-inventory.json",
                    "vanilla-result.json",
                    "subject",
                }
            ):
                continue
            if any(secret in child_relative.encode() for secret in secrets):
                raise CredentialLeakError("provider credential appears in a persisted path")
            status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(status.st_mode):
                target = os.readlink(name, dir_fd=descriptor).encode()
                if any(secret in target for secret in secrets):
                    raise CredentialLeakError("provider credential appears in a persisted symlink")
                continue
            if stat.S_ISDIR(status.st_mode):
                child_fd, _ = _open_verified(descriptor, name, status, directory=True)
                frames.append([child_fd, child_relative, _scan_names(child_fd, deadline, count), 0])
                continue
            if not stat.S_ISREG(status.st_mode):
                continue
            child_fd, _ = _open_verified(descriptor, name, status, directory=False)
            try:
                overlap = max(len(secret) for secret in secrets) - 1
                pending = b""
                while chunk := os.read(child_fd, 1024 * 1024):
                    if time.monotonic() > deadline:
                        raise VanillaError("credential scan exceeded time limit")
                    total += len(chunk)
                    if total > MAX_FREEZE_BYTES:
                        raise VanillaError("credential scan byte count exceeds limit")
                    pending += chunk
                    split = max(0, len(pending) - overlap)
                    if any(secret in pending[: split + overlap] for secret in secrets):
                        raise CredentialLeakError("provider credential appears in persisted artifact bytes")
                    pending = pending[split:]
                if any(secret in pending for secret in secrets):
                    raise CredentialLeakError("provider credential appears in persisted artifact bytes")
            finally:
                os.close(child_fd)
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        for frame in frames:
            if isinstance(frame[0], int):
                try:
                    os.close(frame[0])
                except OSError:
                    pass
