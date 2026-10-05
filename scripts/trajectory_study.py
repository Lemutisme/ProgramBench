"""Fresh, controlled native continuations from an issuer-frozen checkpoint.

This adapter owns only initial inputs, not a new solver, verifier, or budget loop.
Use one new campaign per policy arm with the SAME SeedSpec. Public trajectory and
context are issuer-approved development facts, not independently accepted claims.
No model execution occurs until main enters the existing campaign runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import sqlite3
import stat
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from typing import Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from programbench.candidate import verify_archive_no_credentials
from programbench.utils.load_data import load_all_instances
from scripts.campaign_admission import disk_admission
from scripts.campaign_runner import CampaignError, _bounded_cleanup_stop, plan_campaign, run_campaign, run_job
from scripts.campaign_stop import StopToken
from scripts.campaign_usage import CircuitBreaker
from scripts.campaign_workspace import WorkspaceLease, WorkspaceRequest, _run_helper, ensure_workspace
from scripts.campaign_workspace_fs import PathIdentity, sync_tree
from scripts.run_opencode import load_config

# Bounds on one import operation, never cumulative model/search budgets.
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 20000
MAX_DEPTH = 32
CONTROL = frozenset({".git", ".programbench", ".programbench-cleanroom.json", "reference"})
FORBIDDEN = CONTROL | {"executable", "reference-bin", "submission.tar.gz", "TRAJECTORY.public.json", "STUDY-CONTEXT.md"}


class StudyError(CampaignError):
    """A fresh continuation cannot be safely admitted from these coordinates."""


@dataclass(frozen=True)
class SeedSpec:
    instance_id: str
    checkpoint: Path
    checkpoint_sha256: str
    trajectory: Path
    trajectory_sha256: str
    context: Path
    context_sha256: str

    @classmethod
    def load(cls, path: Path) -> "SeedSpec":
        value = _read_json(path)
        if set(value) != {"version", "instance_id", "checkpoint", "trajectory", "context"} or value["version"] != 1:
            raise StudyError("seed spec requires version 1 and exact input coordinates")
        if not isinstance(value["instance_id"], str) or re.fullmatch(r"[A-Za-z0-9_.-]+", value["instance_id"]) is None:
            raise StudyError("invalid seed instance ID")
        coordinates = {}
        for name in ("checkpoint", "trajectory", "context"):
            source = value[name]
            if not isinstance(source, dict) or set(source) != {"path", "sha256"}:
                raise StudyError(f"invalid {name} coordinates")
            if not isinstance(source["path"], str) or not isinstance(source["sha256"], str):
                raise StudyError(f"invalid {name} coordinates")
            coordinates[name] = Path(source["path"])
            coordinates[name + "_sha256"] = source["sha256"]
        result = cls(instance_id=value["instance_id"], **coordinates)
        result.validate()
        return result

    def manifest(self) -> dict[str, object]:
        return {"version": 1, "instance_id": self.instance_id, **{
            name: {"path": str(getattr(self, name)), "sha256": getattr(self, name + "_sha256")}
            for name in ("checkpoint", "trajectory", "context")
        }}

    def validate(self) -> None:
        secrets = tuple(value.encode() for name in ("OPENAI_API_KEY", "OPENCODE_SERVER_PASSWORD") if (value := os.environ.get(name)))
        for name in ("checkpoint", "trajectory", "context"):
            path = getattr(self, name)
            expected = getattr(self, name + "_sha256")
            if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
                raise StudyError(f"invalid {name} SHA256")
            if path.resolve(strict=True) != path or path.is_symlink():
                raise StudyError(f"{name} must be a canonical non-symlink path")
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
                info = os.fstat(stream.fileno())
                limit = MAX_ARCHIVE_BYTES if name == "checkpoint" else MAX_TEXT_BYTES
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o222 or info.st_size > limit:
                    raise StudyError(f"{name} must be a bounded read-only regular file with one link")
                digest = hashlib.sha256()
                size = 0
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise StudyError(f"{name} grew beyond its import bound")
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise StudyError(f"{name} hash mismatch")
        verify_archive_no_credentials(self.checkpoint, secrets)
        for path in (self.trajectory, self.context):
            data = path.read_bytes()
            if b"\0" in data or any(secret in data for secret in secrets):
                raise StudyError("public input contains NUL or a live credential")
            data.decode("utf-8")
        trajectory = _read_json(self.trajectory)
        if trajectory.get("version") != 1 or trajectory.get("kind") != "public-trajectory" or trajectory.get("purpose") != "development-only":
            raise StudyError("trajectory must be an issuer-approved public-trajectory development packet")
        # The marker checks format, not whether arbitrary user/tool text is safe.
        # Semantic projection and permission to disclose belong to the issuer.


def workspace_factory(spec: SeedSpec):
    """Wrap the real lease-bound workspace admission; never re-seed an issue."""
    def prepare(request: WorkspaceRequest, lease: WorkspaceLease):
        if request.instance_id != spec.instance_id or request.arm != "procontract" or not request.deadline_only or not request.isolated:
            raise StudyError("checkpoint study requires the matching isolated deadline-only native task")
        spec.validate()
        root = request.job.root
        receipt_path = root / "trajectory-seed.json"
        pending_path = root / "trajectory-seed-pending.json"
        job_identity = {"root": str(root.resolve()), "campaign": request.campaign,
                        "instance_id": request.instance_id, "arm": request.arm}
        issued = _issued(root)
        if issued and not receipt_path.exists():
            raise StudyError("existing issue has no seed receipt; refusing overwrite or historical readmission")
        if pending_path.exists() and not receipt_path.exists():
            raise StudyError("ambiguous interrupted seed; preserved for inspection, never automatically re-seeded")
        if receipt_path.exists():
            receipt = _read_json(receipt_path)
            if receipt.get("job") != job_identity or receipt.get("spec") != spec.manifest() or receipt.get("status") != "seeded" or receipt.get("receiptHash") != _hash_json({key: value for key, value in receipt.items() if key != "receiptHash"}):
                raise StudyError("seed receipt conflicts with requested inputs")
            PathIdentity.from_dict(receipt.get("candidateIdentity")).verify()
            if not issued and _inventory(Path(receipt["candidateIdentity"]["path"])) != receipt.get("files"):
                raise StudyError("pre-issue seeded candidate changed; refusing a different starting point")
            if not issued:
                _require_unused_gateway(request.state_root / "provider-control" / "requests.db")
            workspace = ensure_workspace(request, lease)
            if PathIdentity.capture("candidate", workspace.candidate).to_dict() != receipt["candidateIdentity"]:
                _stop_workspace(workspace)
                raise StudyError("seed receipt belongs to a different candidate")
            return workspace
        workspace = ensure_workspace(request, lease)
        # Stop the owned native server and gateway before any mutable import.
        _stop_workspace(workspace)
        if _issued(root):
            raise StudyError("issue appeared before seed; refusing overwrite")
        _require_unused_gateway(request.state_root / "provider-control" / "requests.db")
        identity = PathIdentity.capture("candidate", workspace.candidate)
        pending = {"version": 1, "job": job_identity, "spec": spec.manifest(), "candidateIdentity": identity.to_dict()}
        _write_json(pending_path, pending, exclusive=True)
        with tempfile.TemporaryDirectory(prefix=".trajectory-seed-", dir=root) as temporary:
            stage = Path(temporary)
            files = _stage(spec, stage)
            sync_tree(stage)
            stage.chmod(0o755)
            info = workspace.candidate.stat()
            command = (workspace.docker, "run", "--rm", "--platform", workspace.manifest.platform,
                       "--user", f"{info.st_uid}:{info.st_gid}", "--network", "none", "--read-only",
                       "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--entrypoint", "/usr/bin/python3",
                       "-v", f"{stage}:/seed:ro", "-v", f"{workspace.candidate}:/candidate:rw",
                       workspace.manifest.image_digest, "-I", "-S", "-c", _COPY_SCRIPT, "/seed", "/candidate")
            # Reuse labeled helper ownership, bounded cgroup-backed Docker launch,
            # and exact cleanup instead of introducing an unowned subprocess.
            _run_helper(workspace.docker_runner, workspace.docker, workspace.manifest, "trajectory-seed", command, 90)
            identity.verify()
            if _inventory(workspace.candidate) != files:
                raise StudyError("seed copy does not match the validated frozen input")
            complete = {**pending, "status": "seeded", "files": files}
            _write_json(receipt_path, {**complete, "receiptHash": _hash_json(complete)}, exclusive=True)
        return workspace.restart()
    return prepare


def _stop_workspace(workspace) -> None:
    # run_job cannot register its cleanup owner until this factory returns.
    # Retain the same owned handle, and attempt bounded cleanup if stop fails.
    try:
        workspace.stop_for_freeze()
    except BaseException as error:
        if not _bounded_cleanup_stop(workspace.force_stop, workspace.cleanup_timeout_seconds):
            raise StudyError("pre-admission workspace stop and bounded cleanup failed; preserve owned evidence") from error
        workspace.lease.release_handle()
        raise


def _stage(spec: SeedSpec, root: Path) -> list[dict[str, object]]:
    """Manual regular-file extraction: tar metadata never chooses permissions/owners."""
    entries = []
    total = 0
    with tarfile.open(spec.checkpoint, "r:gz") as archive:
        names = set()
        for member in archive:
            name = member.name.removesuffix("/") if member.isdir() else member.name
            path = PurePosixPath(name)
            if (not name or path.is_absolute() or path.as_posix() != name or "." in path.parts or ".." in path.parts
                    or "\\" in name or len(path.parts) > MAX_DEPTH or any(part in FORBIDDEN for part in path.parts)
                    or any(ord(character) < 32 for character in name) or name in names):
                raise StudyError("unsafe, protected, or duplicate checkpoint path")
            if not (member.isfile() or member.isdir()) or member.issparse() or member.size < 0:
                raise StudyError("checkpoint may contain only ordinary files and directories")
            names.add(name)
            total += member.size
            if len(names) > MAX_ENTRIES or total > MAX_EXPANDED_BYTES:
                raise StudyError("checkpoint import exceeds bounded operation size")
            entries.append((member, path))
        if "compile.sh" not in names:
            raise StudyError("checkpoint does not contain compile.sh")
        # Validate the entire archive before writing any member.
        for member, path in entries:
            destination = root.joinpath(*path.parts)
            destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            if member.isdir():
                destination.mkdir(mode=0o755, exist_ok=True)
                continue
            source = archive.extractfile(member)
            if source is None:
                raise StudyError("checkpoint regular member is unreadable")
            with source, destination.open("xb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
            destination.chmod(0o755 if member.mode & 0o111 else 0o644)
    shutil.copyfile(spec.trajectory, root / "TRAJECTORY.public.json")
    shutil.copyfile(spec.context, root / "STUDY-CONTEXT.md")
    for name in ("TRAJECTORY.public.json", "STUDY-CONTEXT.md"):
        (root / name).chmod(0o644)
    # Launcher umask must not make staged directories unreadable to the
    # candidate UID; contents remain read-only mounted into the helper.
    for path in root.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
    # Recheck exact inputs after staging; no silent source replacement race.
    spec.validate()
    return _inventory(root)


def _inventory(root: Path) -> list[dict[str, object]]:
    result = []
    for directory, directories, files in os.walk(root, followlinks=False):
        if Path(directory) == root:
            directories[:] = [name for name in directories if name not in CONTROL]
            files = [name for name in files if name not in CONTROL]
        for name in sorted((*directories, *files)):
            path = Path(directory) / name
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
                raise StudyError("seed tree contains links or special files")
            result.append({"path": path.relative_to(root).as_posix(), "kind": "directory" if path.is_dir() else "file",
                           "mode": stat.S_IMODE(info.st_mode),
                           **({"sha256": _sha256(path)} if path.is_file() else {})})
            if len(result) > MAX_ENTRIES + 2:
                raise StudyError("seed tree exceeds bounded inventory size")
    return sorted(result, key=lambda entry: entry["path"])


def _issued(root: Path) -> bool:
    return any(root.glob("procontract-issue*.json")) or any((root / name).exists() for name in (
        "procontract-result.json", "procontract-final.json", "procontract-session.json", "submission.tar.gz"))


def _require_unused_gateway(path: Path) -> None:
    if not path.exists():
        raise StudyError("fresh isolated workspace lacks gateway accounting")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        for table in ("request", "execution_budget"):
            if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
                raise StudyError("workspace has already admitted provider work or a deadline")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_json(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _read_json(path: Path) -> dict:
    if path.is_symlink():
        raise StudyError("JSON evidence cannot be a symlink")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise StudyError("JSON evidence must be an object")
    return value


def _write_json(path: Path, value: object, *, exclusive: bool = False) -> None:
    data = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


_COPY_SCRIPT = r'''
import os, pathlib, shutil, stat, sys
source, target = map(pathlib.Path, sys.argv[1:])
protected = {".git", ".programbench", ".programbench-cleanroom.json", "reference"}
# The server is stopped and neither mount exposes host state or credentials.
# Do not follow even preexisting untrusted links while clearing candidate files.
for child in target.iterdir():
    if child.name in protected:
        continue
    if child.is_dir() and not child.is_symlink():
        shutil.rmtree(child)
    else:
        child.unlink()
for child in source.iterdir():
    if child.name in protected or child.is_symlink():
        raise RuntimeError("protected or linked seed entry")
    if child.is_dir():
        shutil.copytree(child, target / child.name, copy_function=shutil.copyfile)
    else:
        shutil.copyfile(child, target / child.name)
for root, dirs, files in os.walk(source):
    for name in dirs + files:
        path = pathlib.Path(root) / name
        copied = target / path.relative_to(source)
        copied.chmod(stat.S_IMODE(path.stat().st_mode))
        descriptor = os.open(copied, os.O_RDONLY | (os.O_DIRECTORY if copied.is_dir() else 0))
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
try: os.fsync(fd)
finally: os.close(fd)
'''


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed-spec", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--evaluate", action="store_true", help="opt in to official independent evaluation after delivery")
    parser.add_argument("--minimum-disk-free-bytes", type=int)
    args = parser.parse_args(arguments)
    spec = SeedSpec.load(args.seed_spec)
    config = load_config(args.config)
    if not config.limits.deadline_only or config.limits.wall_time_seconds != 21600:
        raise StudyError("new continuation uses the six-hour deadline-only profile")
    # Instance selection reads only public registry coordinates, never tests.
    instances = () if args.resume else tuple(row for row in load_all_instances(include_tests=False) if row["instance_id"] == spec.instance_id)
    plan = plan_campaign(config, instances, output=args.output, limit=1, seed=42,
                         arm_selection="procontract", dry_run=args.dry_run, resume=args.resume)
    if plan.selected_ids != (spec.instance_id,) or plan.cleanup_only:
        raise StudyError("study selection differs or campaign requires separate cleanup")
    frozen = {"version": 1, "kind": "fresh-checkpoint-continuation", "spec": spec.manifest(),
              "campaignHash": _sha256(plan.manifest_path), "adapterHash": _sha256(Path(__file__).resolve()),
              "evaluation": "official" if args.evaluate else "external-public-panel",
              "budget": "new six-hour task; not an extension of the source task", "automaticPromotion": False}
    study_path = plan.output / "trajectory-study.json"
    if study_path.exists():
        if _read_json(study_path) != frozen:
            raise StudyError("frozen study coordinates changed")
    else:
        if args.resume:
            raise StudyError("cannot resume a campaign without frozen study coordinates")
        _write_json(study_path, frozen, exclusive=True)
    if args.dry_run:
        return 0
    missing = [name for name in ("OPENAI_API_KEY", "OPENCODE_SERVER_PASSWORD") if not os.environ.get(name)]
    if missing:
        raise StudyError("required credential environment missing: " + ", ".join(missing))
    breaker_path = plan.output / "breaker.json"
    breaker = CircuitBreaker.from_snapshot(_read_json(breaker_path)) if breaker_path.exists() else CircuitBreaker.from_campaign_config(config)
    stop = StopToken()
    previous = {number: signal.signal(number, lambda number, _frame: stop.set(f"signal_{number}")) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        result = run_campaign(plan.jobs, workers=1, breaker=breaker, state_path=breaker_path, stop_event=stop,
                              admission_check=lambda: disk_admission(plan.output, args.minimum_disk_free_bytes, plan.output / "disk-admission.json"),
                              execute=lambda candidate, token: run_job(plan, candidate, breaker=breaker, resume=args.resume,
                                                                       stop_token=token, workspace_factory=workspace_factory(spec)))
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    if result.failed or result.paused or result.interrupted or result.pending or result.running:
        return 1
    if args.evaluate:
        from scripts.campaign_evaluate import evaluate_campaign
        return 0 if evaluate_campaign(plan.output, unattended=True).status == "completed" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
