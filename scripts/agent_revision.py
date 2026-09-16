"""Issuer-owned source reconstruction and offline qualification of Agent revisions.

Candidate inputs contain a parent hash, a patch and two policies. Only the host
recipe chooses build/check commands; candidate code never executes on the host.
A qualified revision remains a research candidate, without adoption authority.
"""

import copy
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import signal
import stat
import subprocess
import tarfile
import time
from typing import Callable
import uuid

from scripts.campaign_policy import load_policy_artifact
from scripts.campaign_experiments import validate_runtime_options
from scripts.growth_loop import GrowthError, encode, record, seal, sha256

PATCH_LIMIT = 16 * 1024 * 1024
PROPOSAL_LIMIT = 65536
SOURCE_LIMIT = 1024 * 1024 * 1024


def artifact_bytes(ref: dict, limit: int = SOURCE_LIMIT) -> bytes:
    """Read a bounded regular artifact without following any symlink component."""
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
        raise GrowthError("artifact requires exactly path and sha256")
    path = Path(ref["path"])
    if (not path.is_absolute() or path.resolve() != path
            or not isinstance(ref["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"])):
        raise GrowthError("artifact requires canonical absolute path and SHA256")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise GrowthError("artifact is not a bounded regular file")
        data = stream.read(limit + 1)
    if len(data) > limit or sha256(data) != ref["sha256"]:
        raise GrowthError("artifact bytes changed or exceed limit")
    return data


def _publish(path: Path, data: bytes, executable: bool = False) -> dict:
    seal(path, data)
    if executable:
        path.chmod(0o500)
    return {"path": str(path), "sha256": sha256(data)}


def validate_recipe(recipe: dict) -> dict:
    """A recipe is externally frozen input, never a candidate artifact."""
    required = {"version", "image", "build", "qualifications", "dependencies", "output"}
    optional = {"memory_mb", "tmpfs_mb", "pids_limit"}
    if not isinstance(recipe, dict) or not required <= set(recipe) or set(recipe) - required - optional:
        raise GrowthError("invalid trusted revision recipe fields")
    if recipe["version"] != 1 or not re.fullmatch(r"(?:.+@)?sha256:[0-9a-f]{64}", recipe["image"]):
        raise GrowthError("revision build image must be digest pinned")
    _path(recipe["output"])
    if not isinstance(recipe["qualifications"], list) or not recipe["qualifications"]:
        raise GrowthError("independent qualification scripts are required")
    _command(recipe["build"])
    names = set()
    for check in recipe["qualifications"]:
        if set(check) != {"name", "argv", "timeout_seconds", "script"}:
            raise GrowthError("qualification requires a trusted script, command and timeout")
        if not re.fullmatch(r"[a-z0-9-]+", check["name"]) or check["name"] in names:
            raise GrowthError("qualification names must be unique safe names")
        names.add(check["name"])
        _command({key: check[key] for key in ("argv", "timeout_seconds")})
        if "/trusted/check" not in check["argv"]:
            raise GrowthError("qualification command must execute the read-only trusted check")
        artifact_bytes(check["script"], PATCH_LIMIT)
    if not isinstance(recipe["dependencies"], dict):
        raise GrowthError("dependencies must map simple names to immutable files")
    for name, ref in recipe["dependencies"].items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in {".", ".."}:
            raise GrowthError("dependency name is not safe")
        artifact_bytes(ref)
    for key, default in (("memory_mb", 8192), ("tmpfs_mb", 8192), ("pids_limit", 256)):
        value = recipe.get(key, default)
        if type(value) is not int or value < 16 or value > 131072:
            raise GrowthError(f"invalid build resource guard {key}")
    return recipe


def _command(command: dict) -> None:
    if (not isinstance(command, dict) or set(command) != {"argv", "timeout_seconds"}
            or not isinstance(command["argv"], list) or not command["argv"]
            or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command["argv"])
            or type(command["timeout_seconds"]) not in {int, float}
            or not 0 < command["timeout_seconds"] <= 21600):
        raise GrowthError("build/check requires explicit argv and bounded individual timeout")


def _path(name: str) -> PurePosixPath:
    if (not isinstance(name, str) or not name or "\0" in name
            or PurePosixPath(name).is_absolute() or str(PurePosixPath(name)) != name
            or any(part in {".", "..", ".git"} for part in PurePosixPath(name).parts)):
        raise GrowthError("source path is not canonical or escapes its tree")
    return PurePosixPath(name)


def _git(repo: Path, *arguments: str, data: bytes | None = None) -> bytes:
    # No candidate-controlled Git config, hooks, attributes filters or credential helpers.
    environment = {"PATH": os.defpath, "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
                   "GIT_AUTHOR_NAME": "Agent Revision", "GIT_AUTHOR_EMAIL": "revision@localhost",
                   "GIT_COMMITTER_NAME": "Agent Revision", "GIT_COMMITTER_EMAIL": "revision@localhost",
                   "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000"}
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                             # Detached maintenance can delete loose objects while the build copies this tree.
                             "-c", "gc.auto=0", "-c", "maintenance.auto=false",
                             "-c", "protocol.file.allow=never", *arguments], cwd=repo, input=data,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, timeout=120)
    if result.returncode:
        raise GrowthError("source reconstruction Git command failed: " + result.stderr.decode(errors="replace")[-4096:])
    return result.stdout


def _tree(repo: Path, commit: str) -> tuple[dict, dict]:
    files, contents, objects = {}, {}, []
    total = 0
    for entry in _git(repo, "ls-tree", "-rzl", commit).split(b"\0"):
        if not entry:
            continue
        header, raw = entry.split(b"\t", 1)
        mode, kind, object_id, size = header.decode().split()
        name = raw.decode("utf-8")
        _path(name)
        if kind != "blob" or mode not in {"100644", "100755", "120000"}:
            raise GrowthError("source tree contains a submodule or special file")
        total += int(size)
        if total > SOURCE_LIMIT:
            raise GrowthError("source archive exceeds individual materialization limit")
        objects.append((name, mode, object_id, int(size)))
    stream = io.BytesIO(_git(repo, "cat-file", "--batch",
                            data="".join(item[2] + "\n" for item in objects).encode()))
    for name, mode, object_id, size in objects:
        if stream.readline().decode().strip() != f"{object_id} blob {size}":
            raise GrowthError("source object stream identity mismatch")
        data = stream.read(size)
        if len(data) != size or stream.read(1) != b"\n":
            raise GrowthError("source object stream was truncated")
        files[name] = {"mode": mode, "sha256": sha256(data), "size": size}
        contents[name] = data
        if mode == "120000":
            files[name]["target"] = data.decode("utf-8")
    _links(files)
    return files, contents


def _verify_worktree(repo: Path, files: dict) -> None:
    """Git status normalizes text and ignores files; verify mounted bytes directly."""
    ancestors = {str(parent) for name in files for parent in PurePosixPath(name).parents}
    seen = set()
    for directory, directories, names in os.walk(repo, followlinks=False):
        relative = Path(directory).relative_to(repo)
        if relative == Path("."):
            directories[:] = [name for name in directories if name != ".git"]
        for name in directories:
            path = Path(directory) / name
            key = path.relative_to(repo).as_posix()
            if path.is_symlink():
                names.append(name)
            elif key not in ancestors:
                raise GrowthError("mounted source contains an unbound directory")
        for name in names:
            path = Path(directory) / name
            key = path.relative_to(repo).as_posix()
            expected = files.get(key)
            if expected is None:
                raise GrowthError("mounted source contains an unbound file")
            seen.add(key)
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                if expected["mode"] != "120000" or os.readlink(path) != expected["target"]:
                    raise GrowthError("mounted source link changed")
                continue
            if (not stat.S_ISREG(info.st_mode) or expected["mode"] == "120000"
                    or bool(info.st_mode & 0o111) != (expected["mode"] == "100755")
                    or info.st_size != expected["size"]):
                raise GrowthError("mounted source file type, mode or size changed")
            artifact_bytes({"path": str(path), "sha256": expected["sha256"]})
    if seen != set(files):
        raise GrowthError("mounted source is missing a bound file")


def _links(files: dict) -> None:
    links = {name: item["target"] for name, item in files.items() if item["mode"] == "120000"}
    for name in files:
        if any(str(parent) in links for parent in PurePosixPath(name).parents):
            raise GrowthError("source entry is beneath a symbolic link")
    for name, target in links.items():
        if not target or "\0" in target or PurePosixPath(target).is_absolute():
            raise GrowthError("baseline symlink must be relative and remain inside source")
        remaining = list(PurePosixPath(name).parent.parts) + target.split("/")
        resolved, followed = [], set()
        while remaining:
            part = remaining.pop(0)
            if part in {"", "."}:
                continue
            if part == "..":
                if not resolved:
                    raise GrowthError("baseline symlink escapes source")
                resolved.pop()
                continue
            resolved.append(part)
            current = "/".join(resolved)
            if current in links:
                if current in followed:
                    raise GrowthError("cyclic source symlink")
                followed.add(current)
                resolved.pop()
                remaining = links[current].split("/") + remaining
        destination = "/".join(resolved)
        if not destination or not any(key == destination or key.startswith(destination + "/") for key in files):
            raise GrowthError("baseline symlink target is not a source entry")


def _archive(files: dict, contents: dict) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(files):
            info = tarfile.TarInfo(name)
            info.mode = 0o755 if files[name]["mode"] == "100755" else 0o644
            if files[name]["mode"] == "120000":
                info.type, info.linkname = tarfile.SYMTYPE, files[name]["target"]
                archive.addfile(info)
                continue
            info.size = len(contents[name])
            archive.addfile(info, io.BytesIO(contents[name]))
    return stream.getvalue()


def _extract(archive_data: bytes, files: dict, destination: Path) -> None:
    _links(files)
    destination.mkdir(parents=True, exist_ok=False)
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:") as archive:
        for member in archive.getmembers():
            _path(member.name)
            if member.name in seen or member.name not in files:
                raise GrowthError("source archive contains duplicate or unbound path")
            seen.add(member.name)
            expected = files[member.name]
            if expected["mode"] == "120000":
                if not member.issym() or member.linkname != expected["target"]:
                    raise GrowthError("source archive symbolic link mismatch")
                continue
            if (not member.isfile() or member.size != expected["size"]
                    or bool(member.mode & 0o111) != (expected["mode"] == "100755")):
                raise GrowthError("source archive file type/mode/size mismatch")
            data = archive.extractfile(member).read(expected["size"] + 1)
            if sha256(data) != expected["sha256"]:
                raise GrowthError("source archive file bytes mismatch")
            path = destination / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o755 if expected["mode"] == "100755" else 0o644)
    if seen != set(files):
        raise GrowthError("source archive lacks a bound file")
    for name, item in files.items():
        if item["mode"] == "120000":
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(item["target"])


def _source(root: Path, base: dict, parent: dict | None, patch: bytes) -> dict:
    repo = root / "source"
    if parent is None:
        if set(base) != {"repo", "commit"} or not re.fullmatch(r"[0-9a-f]{40}", base["commit"]):
            raise GrowthError("initial source requires exact repository and commit")
        origin = Path(base["repo"])
        if not origin.is_absolute() or origin.resolve() != origin:
            raise GrowthError("source repository must be an absolute canonical path")
        if _git(origin, "cat-file", "-t", base["commit"]).strip() != b"commit":
            raise GrowthError("initial source identity is not an exact Git commit")
        files, contents = _tree(origin, base["commit"])
        initial = _archive(files, contents)
    else:
        files = json.loads(artifact_bytes(parent["source"]["files"]))
        initial = artifact_bytes(parent["source"]["archive"])
    _extract(initial, files, repo)
    _git(repo, "init", "--quiet")
    _git(repo, "add", "--all", "--force")
    _git(repo, "commit", "--quiet", "--allow-empty", "-m", "Frozen parent source")
    if _tree(repo, "HEAD")[0] != files:
        raise GrowthError("materialized parent changed the exact source tree")
    if patch:
        _git(repo, "apply", "--check", "--index", "--whitespace=nowarn", "-", data=patch)
        _git(repo, "apply", "--index", "--whitespace=nowarn", "-", data=patch)
        changed = _git(repo, "diff", "--cached", "--name-only", "-z").decode().split("\0")
        updated, _ = _tree(repo, _git(repo, "write-tree").decode().strip())
        if any(files.get(name, {}).get("mode") == "120000" or updated.get(name, {}).get("mode") == "120000"
               for name in changed if name):
            raise GrowthError("candidate patches cannot introduce or modify symbolic links")
        _git(repo, "commit", "--quiet", "--allow-empty", "-m", "Frozen candidate source")
    commit = _git(repo, "rev-parse", "HEAD").decode().strip()
    files, contents = _tree(repo, commit)
    return {"repo": str(repo), "commit": commit, "base": base, "tree_sha256": sha256(encode(files)),
            "files": _publish(root / "source-files.json", encode(files)),
            "archive": _publish(root / "source.tar", _archive(files, contents))}


def changed_components(paths: list[str]) -> list[str]:
    """Conservative trusted path classification; candidate labels grant no rights."""
    components = set()
    for name in paths:
        lower = name.lower()
        parts = set(PurePosixPath(lower).parts)
        if (parts.intersection({"contract", "procontract", "pro-contract", "permission", "permissions", "kernel", "settlement", "migration", "migrations"})
                or PurePosixPath(lower).stem in {"pro-contract", "permission", "permissions", "authority", "credential", "kernel", "settlement", "migration"}
                or any(token in lower for token in ("session-store", "session.sql", "event-v2", "authority", "credential", "sandbox"))):
            components.add("sensitive")
        if parts.intersection({"script", "scripts"}) or PurePosixPath(lower).name in {
                "package.json", "bun.lock", "bun.lockb", "pyproject.toml", "uv.lock"}:
            components.add("build-or-improver")
        if parts.intersection({"tool", "tools"}):
            components.add("tools")
        if parts.intersection({"system-context", "history", "memory"}):
            components.add("context")
    return sorted(components or {"runtime"})


def _docker(root: Path, phase: str, command: dict, recipe: dict, source: dict,
            binary: dict | None, cancelled: Callable[[], bool], *, runtime_options: dict | None = None) -> dict:
    if cancelled():
        raise GrowthError("revision operation explicitly cancelled before Docker admission")
    name = "agent-revision-" + uuid.uuid4().hex
    output = root / "build-output"
    output.mkdir(exist_ok=True)
    output.chmod(0o700)
    argv = ["docker", "run", "--name", name, "--pull=never", "--network=none", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
            "--pids-limit", str(recipe.get("pids_limit", 256)), "--memory", f"{recipe.get('memory_mb', 8192)}m",
            "--memory-swap", f"{recipe.get('memory_mb', 8192)}m",
            "--tmpfs", f"/tmp:rw,nosuid,nodev,mode=1777,size={recipe.get('tmpfs_mb', 8192)}m",
            "--tmpfs", f"/work:rw,nosuid,nodev,uid={os.getuid()},gid={os.getgid()},mode=0700,size={recipe.get('tmpfs_mb', 8192)}m",
            "--workdir", "/work", "--env", "HOME=/tmp", "--env", "TMPDIR=/tmp",
            "--mount", f"type=bind,src={source['repo']},dst=/source,readonly"]
    if binary is None:
        argv += ["--tmpfs", f"/output:rw,nosuid,nodev,uid={os.getuid()},gid={os.getgid()},mode=0700,size={recipe.get('tmpfs_mb', 8192)}m"]
    else:
        argv += ["--mount", f"type=bind,src={binary['path']},dst=/artifact/opencode,readonly",
                 "--mount", f"type=bind,src={command['script']['path']},dst=/trusted/check,readonly"]
        if runtime_options is not None:
            artifact_bytes(runtime_options, PROPOSAL_LIMIT)
            argv += ["--mount", f"type=bind,src={runtime_options['path']},dst=/trusted/runtime-options.json,readonly"]
    for key, ref in recipe["dependencies"].items():
        argv += ["--mount", f"type=bind,src={ref['path']},dst=/dependencies/{key},readonly"]
    argv += ["--detach", "--entrypoint", "/bin/sh", recipe["image"], "-c", "exec sleep 21660"]
    # No model/provider environment, agent credentials, Docker socket or host home.
    environment = {"PATH": os.defpath, "HOME": "/nonexistent", "DOCKER_CONFIG": "/nonexistent"}
    started = time.time()
    monotonic = time.monotonic()
    timed_out, was_cancelled, output_limited = False, False, False
    cleanup_ok = False
    export_error, phase_error, interruption = None, None, None
    process, selector, exit_code = None, None, None
    with (root / f"{phase}.log").open("xb") as log:
        try:
            bootstrap = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, timeout=30)
            if bootstrap.returncode:
                log.write(bootstrap.stderr[:PATCH_LIMIT])
                exit_code = bootstrap.returncode
                raise GrowthError("offline build/check container admission failed")
            process = subprocess.Popen(["docker", "exec", name, *command["argv"]], stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                       start_new_session=True, env=environment)
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            retained = 0
            while selector.get_map():
                was_cancelled = cancelled()
                timed_out = time.monotonic() - monotonic >= command["timeout_seconds"]
                if was_cancelled or timed_out or output_limited:
                    break
                for key, _ in selector.select(timeout=0.05):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = PATCH_LIMIT - retained
                    log.write(data[:remaining])
                    log.flush()
                    retained += min(len(data), remaining)
                    output_limited = len(data) > remaining
            if not (was_cancelled or timed_out or output_limited):
                process.wait(timeout=20)
                if process.returncode == 0 and binary is None:
                    try:
                        _copy_output(name, recipe["output"], output, environment, cancelled)
                    except (GrowthError, OSError, tarfile.TarError, subprocess.SubprocessError) as error:
                        was_cancelled = cancelled()
                        export_error = str(error)
        except BaseException as error:
            # Preserve a receipt even when admission or local subprocess setup fails.
            phase_error = type(error).__name__ + ": " + str(error)
            interruption = error if not isinstance(error, Exception) else None
        finally:
            if selector is not None:
                selector.close()
            try:
                cleanup = subprocess.run(["docker", "rm", "--force", name], env=environment,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
                cleanup_ok = cleanup.returncode == 0 or b"No such container" in cleanup.stderr
                if not cleanup_ok:
                    phase_error = (phase_error or "") + " Docker cleanup could not be confirmed"
            except (OSError, subprocess.SubprocessError) as error:
                phase_error = (phase_error or "") + " Docker cleanup failed: " + type(error).__name__
            if process is not None:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=20)
                exit_code = process.returncode
                process.stdout.close()
    receipt = {"version": 1, "phase": phase, "recipe_sha256": sha256(encode(recipe)),
               "source_sha256": source["tree_sha256"], "binary_sha256": binary["sha256"] if binary else None,
               "image": recipe["image"], "argv": command["argv"], "started_at": started, "ended_at": time.time(),
               "exit_code": exit_code, "timed_out": timed_out, "cancelled": was_cancelled,
               "output_limited": output_limited, "cleanup_confirmed": cleanup_ok, "export_error": export_error,
               "phase_error": phase_error,
               "isolation": {"network": "none", "credentials": "none", "host_execution": False},
               "log": _publish(root / f"{phase}-sealed.log", (root / f"{phase}.log").read_bytes())}
    if runtime_options is not None:
        receipt["runtime_options_sha256"] = runtime_options["sha256"]
    ref = _publish(root / f"{phase}-receipt.json", encode(receipt))
    if interruption is not None:
        raise interruption
    if (receipt["exit_code"] != 0 or timed_out or was_cancelled or output_limited
            or not cleanup_ok or export_error or phase_error):
        raise GrowthError(f"isolated {phase} failed; durable receipt {ref['path']}")
    return ref


def _copy_output(container: str, name: str, output: Path, environment: dict, cancelled: Callable[[], bool]) -> None:
    """Stream one tmpfs output as bounded data, never a host archive extraction."""
    started = time.monotonic()
    archive_path = output / "artifact.tar"
    with archive_path.open("xb") as archive:
        process = subprocess.Popen(["docker", "exec", container, "/bin/tar", "-C", "/output", "-cf", "-", "--", name],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        size = 0
        try:
            while selector.get_map():
                if cancelled():
                    raise GrowthError("binary export explicitly cancelled")
                if time.monotonic() - started > 30:
                    raise GrowthError("bounded binary export timed out")
                for key, _ in selector.select(timeout=0.05):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    size += len(data)
                    if size > SOURCE_LIMIT + 1024 * 1024:
                        raise GrowthError("binary export exceeds individual artifact limit")
                    archive.write(data)
            if process.wait(timeout=5):
                raise GrowthError("Docker could not stream the trusted binary output")
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()
    with tarfile.open(archive_path, "r:") as archive:
        members = archive.getmembers()
        if (len(members) != 1 or not members[0].isfile() or members[0].name != name
                or members[0].size > SOURCE_LIMIT or not members[0].mode & 0o111):
            raise GrowthError("binary export must contain exactly one regular executable")
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as target:
            stream = archive.extractfile(members[0])
            while data := stream.read(65536):
                target.write(data)
        destination.chmod(0o500)


def freeze_initial(root: Path, source: dict, solver_policy: dict, generator_policy: dict, recipe: dict,
                   cancelled: Callable[[], bool] = lambda: False, *, runtime_options: dict | None = None) -> dict:
    return _build(Path(root), None, source, solver_policy, generator_policy, b"", None, recipe, cancelled,
                  runtime_options=runtime_options)


def _runtime_options(value: object) -> dict:
    try:
        return validate_runtime_options(value)
    except ValueError as error:
        raise GrowthError(str(error)) from error


def validate_proposal(proposal: dict, parent_sha256: str) -> dict:
    required = {"parent_sha256", "rationale", "metadata"}
    if (not isinstance(proposal, dict) or not required <= set(proposal) or set(proposal) - required - {"runtime_options"}
            or proposal["parent_sha256"] != parent_sha256
            or not isinstance(proposal["rationale"], str) or not proposal["rationale"].strip()
            or "\0" in proposal["rationale"]):
        raise GrowthError("proposal may only name exact parent, rationale, required metadata and optional runtime_options")
    if "runtime_options" in proposal:
        _runtime_options(proposal["runtime_options"])
    metadata = proposal["metadata"]
    if not isinstance(metadata, dict) or set(metadata) != {
            "expected_effect", "failure_modes", "permission_changes", "state_migration", "recovery"}:
        raise GrowthError("proposal requires expected effect, failure modes, permissions, migration and recovery")
    for key in ("expected_effect", "recovery"):
        if not isinstance(metadata[key], str) or not metadata[key].strip() or "\0" in metadata[key]:
            raise GrowthError(f"proposal metadata {key} must be nonempty text")
    for key in ("failure_modes", "permission_changes"):
        if (not isinstance(metadata[key], list) or (key == "failure_modes" and not metadata[key])
                or any(not isinstance(item, str) or not item.strip() or "\0" in item for item in metadata[key])):
            raise GrowthError(f"proposal metadata {key} must be an explicit text list")
    if metadata["state_migration"] is not None and (not isinstance(metadata["state_migration"], str)
            or not metadata["state_migration"].strip() or "\0" in metadata["state_migration"]):
        raise GrowthError("state migration must be null or explicit nonempty text")
    return proposal


def build_revision(root: Path, parent_ref: dict, artifacts: dict, recipe: dict,
                   cancelled: Callable[[], bool] = lambda: False) -> dict:
    parent = validate_revision(parent_ref, recipe)
    if set(artifacts) != {"revision_proposal", "source_patch", "solver_policy", "generator_policy"}:
        raise GrowthError("revision proposal must contain exactly four typed artifacts")
    proposal = validate_proposal(json.loads(artifact_bytes(artifacts["revision_proposal"], PROPOSAL_LIMIT)),
                                 parent_ref["sha256"])
    patch = artifact_bytes(artifacts["source_patch"], PATCH_LIMIT)
    if "\0" in patch.decode("utf-8"):
        raise GrowthError("source patch cannot contain NUL")
    return _build(Path(root), parent_ref, parent["source"]["base"], artifacts["solver_policy"],
                  artifacts["generator_policy"], patch, proposal, recipe, cancelled)


def _build(root: Path, parent_ref: dict | None, base: dict, solver_policy: dict, generator_policy: dict,
           patch: bytes, proposal: dict | None, recipe: dict, cancelled: Callable[[], bool], *,
           runtime_options: dict | None = None) -> dict:
    validate_recipe(recipe)
    if not root.is_absolute() or root.resolve() != root:
        raise GrowthError("revision output requires a canonical absolute root")
    for ref in (solver_policy, generator_policy):
        artifact_bytes(ref, PROPOSAL_LIMIT)
        load_policy_artifact(Path(ref["path"]), ref["sha256"])
    parent = validate_revision(parent_ref, recipe) if parent_ref else None
    options = _runtime_options(
        proposal.get("runtime_options", parent.get("runtime_options", {"experiments": None})) if parent
        else {"experiments": None} if runtime_options is None else runtime_options
    )
    specification = {"parent": parent_ref, "base": base, "solver_policy": solver_policy,
                     "generator_policy": generator_policy, "patch_sha256": sha256(patch),
                     "proposal": proposal, "recipe": recipe, "runtime_options": options}
    root.mkdir(parents=True, exist_ok=True)
    record(root / "specification.json", specification)
    if (root / "manifest.json").exists():
        ref = {"path": str(root / "manifest.json"), "sha256": sha256((root / "manifest.json").read_bytes())}
        validate_revision(ref, recipe, executable=False)
        return ref
    if (root / "started.json").exists():
        raise GrowthError("ambiguous or failed revision build requires explicit recovery, not automatic re-execution")
    if cancelled():
        raise GrowthError("revision operation explicitly cancelled before reconstruction")
    record(root / "started.json", {"time": time.time(), "specification_sha256": sha256(encode(specification))})
    source = _source(root, base, parent, patch)
    previous = json.loads(artifact_bytes(parent["source"]["files"])) if parent else {}
    files = json.loads(artifact_bytes(source["files"]))
    paths = sorted(name for name in set(previous) | set(files) if previous.get(name) != files.get(name)) if parent else []
    components = changed_components(paths) if paths else []
    manifest = {"version": 1, "kind": "agent-revision", "parent": parent_ref, "source": source,
                "source_patch": _publish(root / "source.patch", patch),
                "proposal": _publish(root / "proposal.json", encode(proposal)),
                "solver_policy": _publish(root / "solver-policy.txt", artifact_bytes(solver_policy, PROPOSAL_LIMIT)),
                "generator_policy": _publish(root / "generator-policy.txt", artifact_bytes(generator_policy, PROPOSAL_LIMIT)),
                "recipe": _publish(root / "recipe.json", encode(recipe)), "changed_paths": paths,
                "components": components, "status": "proposal-only", "binary": None,
                "build_receipt": None, "qualification_receipts": [], "adoption_authority": False,
                "runtime_options": options,
                "runtime_options_file": _publish(root / "runtime-options.json", encode(options))}
    # This release archives sensitive candidates; Kernel succession is not automated.
    if ("sensitive" in components or (proposal is not None and (proposal["metadata"]["permission_changes"]
            or proposal["metadata"]["state_migration"] is not None))):
        manifest["reason"] = "independent sensitive-change qualification and external authorization are not implemented"
        return _publish(root / "manifest.json", encode(manifest))
    manifest["build_receipt"] = _docker(root, "build", recipe["build"], recipe, source, None, cancelled)
    output = root / "build-output" / recipe["output"]
    if output.resolve() != output or not output.is_file() or not os.access(output, os.X_OK):
        raise GrowthError("isolated build did not produce a regular executable at trusted output path")
    manifest["binary"] = _publish(root / "opencode", artifact_bytes({"path": str(output),
                                          "sha256": sha256(output.read_bytes())}), executable=True)
    for check in recipe["qualifications"]:
        manifest["qualification_receipts"].append(_docker(root, "check-" + check["name"], check, recipe,
                                                         source, manifest["binary"], cancelled,
                                                         runtime_options=manifest["runtime_options_file"]))
    manifest["status"] = "qualified"
    ref = _publish(root / "manifest.json", encode(manifest))
    validate_revision(ref, recipe)
    return ref


def validate_revision(ref: dict, recipe: dict | None = None, executable: bool = True, _seen: set | None = None) -> dict:
    seen = set() if _seen is None else _seen
    if ref.get("sha256") in seen:
        raise GrowthError("cyclic revision ancestry")
    seen.add(ref.get("sha256"))
    manifest = json.loads(artifact_bytes(ref, PATCH_LIMIT))
    if (manifest.get("version") != 1 or manifest.get("kind") != "agent-revision"
            or manifest.get("adoption_authority") is not False):
        raise GrowthError("invalid AgentRevision manifest")
    options = _runtime_options(manifest.get("runtime_options", {"experiments": None}))
    if ("runtime_options" in manifest) != ("runtime_options_file" in manifest):
        raise GrowthError("revision runtime_options require their immutable qualification input")
    options_ref = manifest.get("runtime_options_file")
    if "runtime_options_file" in manifest and artifact_bytes(options_ref, PROPOSAL_LIMIT) != encode(options):
        raise GrowthError("revision runtime_options differ from their immutable qualification input")
    actual_recipe = json.loads(artifact_bytes(manifest["recipe"], PATCH_LIMIT))
    validate_recipe(actual_recipe)
    if recipe is not None and encode(actual_recipe) != encode(recipe):
        raise GrowthError("revision changed the externally frozen recipe")
    parent = validate_revision(manifest["parent"], actual_recipe, _seen=seen) if manifest["parent"] else None
    source = manifest["source"]
    files = json.loads(artifact_bytes(source["files"]))
    if sha256(encode(files)) != source["tree_sha256"]:
        raise GrowthError("revision source identity changed")
    archive = artifact_bytes(source["archive"])
    actual, contents = _tree(Path(source["repo"]), source["commit"])
    if actual != files or _archive(actual, contents) != archive:
        raise GrowthError("revision source archive no longer matches exact Git source")
    _verify_worktree(Path(source["repo"]), files)
    if (_git(Path(source["repo"]), "rev-parse", "HEAD").decode().strip() != source["commit"]
            or _git(Path(source["repo"]), "status", "--porcelain", "--untracked-files=all", "--ignored").strip()):
        raise GrowthError("materialized revision source has changed")
    for key in ("solver_policy", "generator_policy"):
        artifact_bytes(manifest[key], PROPOSAL_LIMIT)
        load_policy_artifact(Path(manifest[key]["path"]), manifest[key]["sha256"])
    patch = artifact_bytes(manifest["source_patch"], PATCH_LIMIT)
    proposal = json.loads(artifact_bytes(manifest["proposal"], PROPOSAL_LIMIT))
    if parent:
        validate_proposal(proposal, manifest["parent"]["sha256"])
        if options != _runtime_options(proposal.get("runtime_options", parent.get("runtime_options", {"experiments": None}))):
            raise GrowthError("revision runtime_options differ from its proposal or inherited parent")
        if (proposal["parent_sha256"] != manifest["parent"]["sha256"]
                or source["base"] != parent["source"]["base"]):
            raise GrowthError("revision proposal/source parent binding changed")
        previous = json.loads(artifact_bytes(parent["source"]["files"]))
        paths = sorted(name for name in set(previous) | set(files) if previous.get(name) != files.get(name))
        ancestor_files = _tree(Path(source["repo"]), "HEAD^" if patch else "HEAD")[0]
        if ancestor_files != previous:
            raise GrowthError("revision is not reconstructed from its exact parent source")
        if patch:
            _git(Path(source["repo"]), "apply", "--reverse", "--check", "--index", "--whitespace=nowarn", "-", data=patch)
        if (manifest["changed_paths"] != paths or manifest["components"] != (changed_components(paths) if paths else [])):
            raise GrowthError("trusted changed-path classification no longer matches source")
    elif patch or proposal is not None or manifest["changed_paths"] or manifest["components"]:
        raise GrowthError("initial revision unexpectedly claims a candidate proposal")
    blocked = "sensitive" in manifest["components"] or (proposal is not None and (
        proposal["metadata"]["permission_changes"] or proposal["metadata"]["state_migration"] is not None))
    if bool(blocked) != (manifest["status"] == "proposal-only"):
        raise GrowthError("revision execution eligibility differs from trusted classification")
    if manifest["status"] == "proposal-only":
        if executable:
            raise GrowthError("sensitive revision is proposal-only and cannot execute or be adopted")
        return manifest
    if manifest["status"] != "qualified":
        raise GrowthError("revision has no trusted qualification")
    artifact_bytes(manifest["binary"])
    if not os.access(manifest["binary"]["path"], os.X_OK):
        raise GrowthError("revision binary is not executable")
    checks = [(manifest["build_receipt"], "build", actual_recipe["build"], None)]
    if len(manifest["qualification_receipts"]) != len(actual_recipe["qualifications"]):
        raise GrowthError("revision qualification set changed")
    checks += [(receipt, "check-" + command["name"], command, manifest["binary"]["sha256"])
               for receipt, command in zip(manifest["qualification_receipts"], actual_recipe["qualifications"], strict=True)]
    for receipt_ref, phase, command, binary_sha in checks:
        receipt = json.loads(artifact_bytes(receipt_ref, PATCH_LIMIT))
        options_sha256 = options_ref["sha256"] if options_ref is not None and phase != "build" else None
        if (receipt.get("phase") != phase or receipt.get("recipe_sha256") != manifest["recipe"]["sha256"]
                or receipt.get("runtime_options_sha256") != options_sha256
                or receipt.get("source_sha256") != source["tree_sha256"] or receipt.get("binary_sha256") != binary_sha
                or receipt.get("image") != actual_recipe["image"] or receipt.get("argv") != command["argv"]
                or receipt.get("exit_code") != 0 or receipt.get("timed_out") is not False
                or receipt.get("cancelled") is not False or receipt.get("output_limited") is not False
                or receipt.get("cleanup_confirmed") is not True or receipt.get("export_error") is not None
                or receipt.get("phase_error") is not None
                or receipt.get("isolation") != {
                    "network": "none", "credentials": "none", "host_execution": False}):
            raise GrowthError("trusted revision receipt does not qualify these exact bytes")
        artifact_bytes(receipt["log"])
    return manifest


def runtime_config(config: dict, ref: dict, recipe: dict | None = None) -> dict:
    manifest = validate_revision(ref, recipe)
    actual_recipe = json.loads(artifact_bytes(manifest["recipe"], PATCH_LIMIT))
    result = copy.deepcopy(config)
    experiments = _runtime_options(manifest.get("runtime_options", {"experiments": None}))["experiments"]
    if "experiments" in result and _runtime_options({"experiments": result["experiments"]})["experiments"] != experiments:
        raise GrowthError("runtime configuration conflicts with the complete AgentRevision experiments binding")
    if experiments is not None:
        result["experiments"] = experiments
    result["arms"]["procontract"].update(repo=manifest["source"]["repo"], revision=manifest["source"]["commit"],
        binary=manifest["binary"]["path"], binary_probe_image=actual_recipe["image"],
        execution_policy="artifact", runtime_protocol="policy-split-v1", policy_file=manifest["solver_policy"]["path"],
        policy_sha256=manifest["solver_policy"]["sha256"])
    return result


def export_source(ref: dict, destination: Path) -> None:
    manifest = validate_revision(ref)
    _extract(artifact_bytes(manifest["source"]["archive"]),
             json.loads(artifact_bytes(manifest["source"]["files"])), destination)
