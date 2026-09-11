"""Package and preflight candidate workspaces."""

import gzip
import hashlib
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from programbench.constants import (
    DOCKER_CPUS,
    DOCKER_EXECUTABLE,
    DOCKER_RUN_ARGS,
    TASK_YAML,
    TASKS_DIR,
    WORKSPACE_DIR,
    image_name_from_instance_id,
)
from programbench.container import ContainerEnvironment
from programbench.utils.internet_control import block_build_internet_dns, restore_build_internet_dns

DEFAULT_IMAGE_TAG = "task_cleanroom_v6"
PORTABLE_ARCHIVE_MTIME = 315532800
MAX_PARTIAL_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_PARTIAL_ARCHIVE_FILES = 100_000
MAX_PARTIAL_ARCHIVE_DEPTH = 64
_EXCLUDED_NAMES = {
    ".git",
    ".programbench",
    ".programbench-cleanroom.json",
    "executable",
    "reference",
    "reference-bin",
    "submission.tar.gz",
}


class CandidateError(RuntimeError):
    pass


class PreflightInfrastructureError(RuntimeError):
    """Checker setup/transport failed; never feed this back as a candidate defect."""


class CandidateCredentialError(CandidateError):
    pass


@dataclass(frozen=True)
class PreflightResult:
    executable_hash: str
    output: str


def package_workspace(
    workspace: Path,
    run_dir: Path,
    instance_id: str,
    *,
    require_compile: bool = True,
) -> Path:
    if not workspace.is_dir():
        raise CandidateError(f"Candidate workspace does not exist: {workspace}")
    if type(require_compile) is not bool:
        raise CandidateError("Candidate compile requirement must be bool")
    if require_compile and not (workspace / "compile.sh").is_file():
        raise CandidateError("Candidate workspace must contain compile.sh")

    output = run_dir / instance_id / "submission.tar.gz"
    temporary = output.with_name(f".{output.name}.tmp")
    clean_hashes = _clean_hashes(instance_id)
    partial_bytes = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as archive:
                    paths = workspace.rglob("*") if require_compile else _partial_paths(workspace)
                    for path in sorted(paths):
                        relative = path.relative_to(workspace)
                        if any(part in _EXCLUDED_NAMES for part in relative.parts):
                            continue
                        if path.is_file() and not path.is_symlink():
                            try:
                                cleanroom_reference = sha256(path) in clean_hashes
                            except PermissionError:
                                if require_compile:
                                    raise
                                continue
                            if cleanroom_reference:
                                if require_compile:
                                    raise CandidateError(
                                        f"Candidate contains a cleanroom reference artifact: {relative}"
                                    )
                                continue
                        try:
                            info = archive.gettarinfo(path, arcname=relative.as_posix())
                        except PermissionError:
                            if require_compile:
                                raise
                            continue
                        if info is None:
                            if require_compile:
                                raise CandidateError(f"Candidate contains an unsupported filesystem entry: {relative}")
                            continue
                        if not require_compile and info.isfile():
                            if partial_bytes + info.size > MAX_PARTIAL_ARCHIVE_BYTES:
                                continue
                            partial_bytes += info.size
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        # ZIP-based build tools reject the Unix epoch; 1980 is deterministic and portable.
                        info.mtime = PORTABLE_ARCHIVE_MTIME
                        if path.is_file() and not path.is_symlink():
                            try:
                                with path.open("rb") as source:
                                    archive.addfile(info, source)
                            except PermissionError:
                                if require_compile:
                                    raise
                        else:
                            archive.addfile(info)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def preflight_candidate(
    archive: Path,
    instance_id: str,
    *,
    image_tag: str = DEFAULT_IMAGE_TAG,
    docker_cpus: int = DOCKER_CPUS,
    deadline: float | None = None,
    image_ref: str | None = None,
) -> PreflightResult:
    if not archive.is_file():
        raise CandidateError(f"Candidate archive does not exist: {archive}")

    env = ContainerEnvironment(
        image=image_ref or f"{image_name_from_instance_id(instance_id)}:{image_tag}",
        cwd=WORKSPACE_DIR,
        executable=DOCKER_EXECUTABLE,
        timeout=900,
        cpus=docker_cpus,
        deadline=deadline,
        prepare_workspace=True,
        run_args=[*DOCKER_RUN_ARGS, "--init", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"],
    )
    output: list[str] = [f"Trusted build preparation: {env.preparation}\n"]
    try:
        _step(env, f"find {WORKSPACE_DIR} -mindepth 1 -maxdepth 1 -exec rm -rf -- {{}} +", output)
        env.copy_submission(archive, f"{WORKSPACE_DIR}/")
        hashes = _clean_hashes(instance_id)
        if hashes:
            pattern = "|".join(sorted(hashes))
            _step(
                env,
                f"! find {WORKSPACE_DIR} -type f -exec sha256sum {{}} + 2>/dev/null | grep -E '^({pattern})  '",
                output,
            )
        _step(env, "rm -f ./executable", output)
        _step(
            env,
            "if [ ! -d .git ]; then "
            "GIT_AUTHOR_DATE='2000-01-01T00:00:00Z' GIT_COMMITTER_DATE='2000-01-01T00:00:00Z' "
            f"git -c safe.directory={WORKSPACE_DIR} -c init.defaultBranch=gold init -q && "
            f"git -c safe.directory={WORKSPACE_DIR} -c user.email=gold@local -c user.name=gold "
            "-c commit.gpgsign=false add -A && "
            "GIT_AUTHOR_DATE='2000-01-01T00:00:00Z' GIT_COMMITTER_DATE='2000-01-01T00:00:00Z' "
            f"git -c safe.directory={WORKSPACE_DIR} -c user.email=gold@local -c user.name=gold "
            "-c commit.gpgsign=false "
            "commit -q --allow-empty -m gold; fi",
            output,
        )
        try:
            block_build_internet_dns(env)
            _step(env, "chmod +x ./compile.sh && ./compile.sh", output, candidate=True)
            _step(env, "if [ -f ./validate.sh ]; then chmod +x ./validate.sh && ./validate.sh; fi", output, candidate=True)
        finally:
            restore_build_internet_dns(env)
        result = _step(env, "test -x ./executable && sha256sum ./executable", output, candidate=True)
        return PreflightResult(executable_hash=result.split()[0], output="".join(output))
    finally:
        env.cleanup()


def verify_archive_no_credentials(archive: Path, secrets: tuple[bytes, ...]) -> None:
    if not secrets:
        return
    if not archive.is_file() or archive.is_symlink():
        raise CandidateError("Candidate archive is not a regular file")
    overlap = max(len(secret) for secret in secrets) - 1
    try:
        with tarfile.open(archive, "r:gz") as packaged:
            for member in packaged:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or any(secret in member.name.encode() for secret in secrets):
                    raise CandidateCredentialError("Candidate archive contains a provider credential")
                if member.issym() or member.islnk():
                    if any(secret in member.linkname.encode() for secret in secrets):
                        raise CandidateCredentialError("Candidate archive contains a provider credential")
                    continue
                if not member.isfile():
                    continue
                source = packaged.extractfile(member)
                if source is None:
                    raise CandidateError("Candidate archive member cannot be read")
                pending = b""
                while chunk := source.read(1024 * 1024):
                    pending += chunk
                    split = max(0, len(pending) - overlap)
                    if any(secret in pending[: split + overlap] for secret in secrets):
                        raise CandidateCredentialError("Candidate archive contains a provider credential")
                    pending = pending[split:]
                if any(secret in pending for secret in secrets):
                    raise CandidateCredentialError("Candidate archive contains a provider credential")
    except (tarfile.TarError, OSError) as error:
        raise CandidateError("Candidate archive cannot be scanned") from error


def _partial_paths(workspace: Path) -> list[Path]:
    paths: list[Path] = []
    for root, directories, files in os.walk(workspace, topdown=True, onerror=lambda _error: None, followlinks=False):
        if len(Path(root).relative_to(workspace).parts) >= MAX_PARTIAL_ARCHIVE_DEPTH:
            directories.clear()
        directories.sort()
        files.sort()
        remaining = MAX_PARTIAL_ARCHIVE_FILES - len(paths)
        if remaining <= 0:
            break
        paths.extend(Path(root) / name for name in (*directories, *files)[:remaining])
    return paths


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _step(env: ContainerEnvironment, command: str, output: list[str], *, candidate: bool = False) -> str:
    result = env.execute(command, timeout=900)
    output.append(result["output"])
    if result["returncode"] != 0:
        detail = result["output"] or result["exception_info"]
        error = CandidateError if candidate else PreflightInfrastructureError
        raise error(f"Preflight command failed: {command}\n{detail.strip()}")
    return result["output"]


def _clean_hashes(instance_id: str) -> set[str]:
    task = TASKS_DIR / instance_id / TASK_YAML
    if not task.is_file():
        return set()
    try:
        value = yaml.safe_load(task.read_text())
    except yaml.YAMLError as error:
        raise CandidateError(f"Task metadata is invalid YAML: {task}") from error
    if not isinstance(value, dict):
        raise CandidateError("Task metadata must be a mapping")
    clean_hashes = value.get("eval_clean_hashes", [])
    if not isinstance(clean_hashes, list):
        raise CandidateError("eval_clean_hashes must be a list")
    if not all(
        isinstance(item, str) and len(item) == 64 and all(character in "0123456789abcdef" for character in item)
        for item in clean_hashes
    ):
        raise CandidateError("eval_clean_hashes entries must be 64-char lowercase hex strings")
    return set(clean_hashes)
