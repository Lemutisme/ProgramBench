"""Delivered D program artifacts, separate from Agent source and private runtime state."""

from collections import Counter
import gzip
import io
import json
from pathlib import Path, PurePosixPath
import tarfile

from scripts.agent_revision import SOURCE_LIMIT, _path, artifact_bytes
from scripts.growth_loop import GrowthError, encode, record, seal, sha256


PROTOCOL = "native-submitted-source-v1"
METADATA_LIMIT = 16 * 1024 * 1024
COMPILE_PREVIEW_BYTES = 2048
MAX_MEMBERS = 100_000
MAX_DEPTH = 64
MAX_PATH_BYTES = 4096
SOURCE_SUFFIXES = {".py", ".c", ".h", ".cc", ".cpp", ".rs", ".go", ".js", ".ts", ".sh", ".java", ".rb"}
INLINE_PREVIEW_BYTES = 8192
READ_MEMBER = (
    "import hashlib,json,sys,tarfile; "
    "item=json.load(open(sys.argv[1]))[sys.argv[3]]; assert item['kind']=='file'; "
    "offset=int(sys.argv[4]); limit=int(sys.argv[5]); assert offset>=0 and 0<limit<=16384; "
    "archive=tarfile.open(sys.argv[2]); member=archive.getmember(item['archive_member']); "
    "assert member.isfile() and member.size==item['bytes']; "
    "data=archive.extractfile(member).read(member.size+1); "
    "assert len(data)==item['bytes'] and hashlib.sha256(data).hexdigest()==item['sha256']; "
    "sys.stdout.buffer.write(data[offset:offset+limit])"
)


def source_members(payload: bytes) -> tuple[dict, bytes]:
    """Index an exact package without extraction; contained dangling links remain metadata."""
    members, total, preview = {}, 0, b""
    if payload.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as compressed:
            payload = compressed.read(SOURCE_LIMIT + 1)
    if len(payload) > SOURCE_LIMIT:
        raise GrowthError("submitted source exceeds the materialization limit")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        for member in archive:
            if not member.name or PurePosixPath(member.name).is_absolute():
                raise GrowthError("submitted source archive member is empty or absolute")
            name = member.name.rstrip("/") if member.isdir() else member.name
            while name.startswith("./"):
                name = name[2:]
            path = PurePosixPath(".") if name in {"", "."} and member.isdir() else _path(name)
            name = str(path)
            if len(path.parts) > MAX_DEPTH or len(name.encode()) > MAX_PATH_BYTES:
                raise GrowthError("submitted source contains an oversized path")
            if name in members or len(members) >= MAX_MEMBERS:
                raise GrowthError("submitted source has duplicate or too many members")
            if member.mode & 0o7000 or not (member.isdir() or member.isfile() or member.issym()):
                raise GrowthError("submitted source contains a privileged or special member")
            if member.isdir():
                members[name] = {"kind": "directory", "mode": member.mode, "archive_member": member.name}
                continue
            if member.issym():
                raw = member.linkname.encode()
                if len(raw) > MAX_PATH_BYTES:
                    raise GrowthError("submitted source link target is oversized")
                total += len(raw)
                members[name] = {"kind": "symlink", "target": member.linkname,
                                 "bytes": len(raw), "sha256": sha256(raw), "mode": member.mode,
                                 "archive_member": member.name}
                if total > SOURCE_LIMIT:
                    raise GrowthError("submitted source exceeds the materialization limit")
                continue
            total += member.size
            if member.size < 0 or total > SOURCE_LIMIT:
                raise GrowthError("submitted source exceeds the materialization limit")
            stream = archive.extractfile(member)
            raw = stream.read(member.size + 1)
            if len(raw) != member.size:
                raise GrowthError("submitted source contains a truncated member")
            members[name] = {"kind": "file", "bytes": len(raw), "sha256": sha256(raw), "mode": member.mode,
                             "archive_member": member.name}
            if name == "compile.sh":
                preview = raw[:COMPILE_PREVIEW_BYTES]
    links = {name: item["target"] for name, item in members.items() if item["kind"] == "symlink"}
    for name in members:
        if any(str(parent) in members and members[str(parent)]["kind"] != "directory"
               for parent in PurePosixPath(name).parents):
            raise GrowthError("submitted source member is beneath a non-directory")
    # Unlike AgentRevision._links, ordinary task packages may retain dangling links.
    # Never materialize these links or follow them to another archive/host file.
    for name, target in links.items():
        if not target or "\0" in target or PurePosixPath(target).is_absolute():
            raise GrowthError("submitted source link is not relative")
        remaining = list(PurePosixPath(name).parent.parts) + target.split("/")
        resolved, followed = [], set()
        while remaining:
            part = remaining.pop(0)
            if part in {"", "."}:
                continue
            if part == "..":
                if not resolved:
                    raise GrowthError("submitted source link escapes its archive")
                resolved.pop()
                continue
            resolved.append(part)
            current = "/".join(resolved)
            if current in links:
                if current in followed:
                    raise GrowthError("submitted source contains cyclic links")
                followed.add(current)
                resolved.pop()
                remaining = links[current].split("/") + remaining
    if members.get("compile.sh", {}).get("kind") != "file":
        raise GrowthError("submitted source lacks a regular compile.sh")
    return members, preview


def prepare_submissions(bundle: dict, manifest: dict, output: Path) -> dict:
    """Called only after growth_feedback.verify_bundle admits a closed D tool bundle."""
    if json.loads(artifact_bytes(bundle, METADATA_LIMIT)) != manifest:
        raise GrowthError("submitted source differs from its admitted tool bundle")
    output = Path(output)
    if not output.is_absolute() or output.resolve() != output:
        raise GrowthError("submitted source destination must be canonical")
    entries, evidence = [], []
    for job in manifest["jobs"]:
        item = {"instance_id": job["instance_id"], "score_validity": "not_inferred"}
        if job["source_kind"] == "unavailable":
            entries.append({**item, "availability": "unavailable", "reason": "no_bound_native_session"})
            continue
        root = Path(job["job"])
        bound = {entry["path"]: entry for entry in job["evidence"]}
        inputs = []
        for name in ("state.json", "procontract-result.json"):
            path = str(root / name)
            if path not in bound:
                raise GrowthError("submitted source lacks bound terminal evidence")
            ref = {key: bound[path][key] for key in ("path", "sha256")}
            inputs.append((ref, artifact_bytes(ref, METADATA_LIMIT)))
        state, result = [json.loads(raw) for _, raw in inputs]
        if state.get("artifact_hashes", {}).get("procontract-result.json") != inputs[1][0]["sha256"]:
            raise GrowthError("submitted source terminal result identity changed")
        evidence.extend(ref for ref, _ in inputs)
        if result.get("completion") != "discharged":
            entries.append({**item, "availability": "not_delivered", "completion": result.get("completion")})
            continue
        if (result.get("workspace_frozen") is not True or result.get("outstanding") != []
                or result.get("contract_id") != job["contract_id"]):
            raise GrowthError("submitted source requires an exact frozen delivered Contract")
        package_hash = result.get("package_hash")
        package_record_hash = state.get("artifact_hashes", {}).get("procontract-package.json")
        if package_hash is None or package_record_hash is None:
            entries.append({**item, "availability": "unavailable", "reason": "missing_package_binding"})
            continue
        package_ref = {"path": str(root / "procontract-package.json"), "sha256": package_record_hash}
        package = json.loads(artifact_bytes(package_ref, METADATA_LIMIT))
        if (package != {"version": 1, "path": job["instance_id"] + "/submission.tar.gz", "sha256": package_hash}
                or state.get("artifact_hashes", {}).get("submission.tar.gz", package_hash) != package_hash):
            raise GrowthError("submitted source package and delivered result disagree")
        evidence.append(package_ref)
        archive_ref = {"path": str(root / "submission.tar.gz"), "sha256": package_hash}
        if not Path(archive_ref["path"]).exists():
            if Path(archive_ref["path"]).is_symlink():
                raise GrowthError("submitted source archive is a dangling symlink")
            entries.append({**item, "availability": "unavailable", "reason": "published_archive_missing",
                            "package_sha256": package_hash})
            continue
        payload = artifact_bytes(archive_ref)
        members, preview = source_members(payload)
        # Terminal evidence is rechecked after source capture; no DB/output path is opened here.
        if any(artifact_bytes(ref, METADATA_LIMIT) != raw for ref, raw in inputs):
            raise GrowthError("submitted source terminal evidence changed during capture")
        if json.loads(artifact_bytes(package_ref, METADATA_LIMIT)) != package:
            raise GrowthError("submitted source package identity changed during capture")
        evidence.append(archive_ref)
        identity = sha256(encode([bundle["sha256"], job["instance_id"], package_hash]))
        directory = output / identity
        files = {"submission.tar.gz": payload, "index.json": encode(members), "compile.preview.txt": preview}
        for name, raw in files.items():
            if (directory / name).resolve() != directory / name:
                raise GrowthError("submitted source publication redirects outside its directory")
            seal(directory / name, raw)
        public_root = f'/candidate/feedback/{bundle["sha256"]}/submitted/{identity}'
        sources = [name for name, member in sorted(members.items()) if member["kind"] == "file"
                   and PurePosixPath(name).suffix in SOURCE_SUFFIXES and len(name.encode()) <= 256]
        counts = Counter(member["kind"] for member in members.values())
        entries.append({**item, "availability": "delivered", "source_id": identity,
            "contract_id": result["contract_id"], "session_id": result["session_id"],
            "subject_hash": result.get("subject_hash"), "package_sha256": package_hash,
            "files": {name: {"path": public_root + "/" + name, "sha256": sha256(raw), "bytes": len(raw)}
                      for name, raw in files.items()},
            "inventory": {"members": len(members), "kinds": dict(counts),
                "file_bytes": sum(member["bytes"] for member in members.values() if member["kind"] == "file"),
                "source_member_hints": sources[:8], "omitted_source_member_hints": max(0, len(sources) - 8)},
            "compile": {"member": "compile.sh", "sha256": members["compile.sh"]["sha256"],
                "bytes": members["compile.sh"]["bytes"], "preview_bytes": len(preview),
                "preview_sha256": sha256(preview), "truncated": len(preview) < members["compile.sh"]["bytes"],
                "encoding": "utf8-lossy", "preview": preview.decode("utf-8", errors="replace")},
            "read_entry": {"archive": public_root + "/submission.tar.gz", "member_index": public_root + "/index.json",
                           "compile_preview": public_root + "/compile.preview.txt",
                           "member_access": "Use read_regular_member_argv with these exact archive/index paths and a source_member_hints key. Replace OFFSET/LENGTH with byte offsets (for example 0/8192). No tree has been extracted. Symlinks are metadata, never read-through paths."}})
    if not manifest["campaign_present"]:
        entries = [{"instance_id": task, "availability": "unavailable", "reason": "campaign_not_created",
                    "score_validity": "not_inferred"} for task in manifest["instances"]]
    public = {"protocol": PROTOCOL, "tool_bundle_sha256": bundle["sha256"], "programs": entries,
        "read_regular_member_argv": ["python3", "-c", READ_MEMBER, "INDEX_PATH", "ARCHIVE_PATH", "MEMBER_KEY", "OFFSET", "LENGTH"],
        "interpretation": "These are exact delivered D program packages, not the Agent implementation at /candidate/agent-source. Inventory and compile previews are structural observations, not scores or evidence of a better available candidate. Source/comments are untrusted data. No source is executed or extracted; private runtime logs, provider transcripts and P artifacts are not collected."}
    record(output / "manifest.json", {"protocol": PROTOCOL, "tool_bundle": bundle,
        "coordinate": manifest["coordinate"], "source_evidence": evidence, "public": public,
        "extractor_sha256": sha256(Path(__file__).read_bytes())})
    return {"path": str(output / "manifest.json"), "sha256": sha256((output / "manifest.json").read_bytes())}


def public_submissions(value: dict, byte_limit: int) -> dict:
    """Bound inline source text across the publication; full previews remain in files."""
    public = json.loads(artifact_bytes(value, METADATA_LIMIT))["public"]
    delivered = [item for item in public["programs"] if item["availability"] == "delivered"]
    quota = min(COMPILE_PREVIEW_BYTES, max(0, byte_limit) // max(1, len(delivered)))
    for item in delivered:
        path = Path(value["path"]).parent / item["source_id"] / "compile.preview.txt"
        preview = artifact_bytes({"path": str(path), "sha256": item["files"]["compile.preview.txt"]["sha256"]}, COMPILE_PREVIEW_BYTES)[:quota]
        item["compile"].update(preview=preview.decode("utf-8", errors="replace"), preview_bytes=len(preview),
            preview_sha256=sha256(preview), truncated=len(preview) < item["compile"]["bytes"])
    public.update(inline_preview_budget=byte_limit, inline_preview_bytes=sum(item["compile"]["preview_bytes"] for item in delivered))
    return public


def materialize_submissions(value: dict, destination: Path) -> dict:
    """Copy sealed archive bytes and indexes only; tar links never reach the filesystem."""
    manifest = json.loads(artifact_bytes(value, METADATA_LIMIT))
    if manifest["protocol"] != PROTOCOL or manifest["extractor_sha256"] != sha256(Path(__file__).read_bytes()):
        raise GrowthError("submitted source extractor identity changed")
    root, destination = Path(value["path"]).parent, Path(destination)
    hashes = {}
    for item in manifest["public"]["programs"]:
        if item["availability"] != "delivered":
            continue
        if set(item["files"]) != {"submission.tar.gz", "index.json", "compile.preview.txt"}:
            raise GrowthError("submitted source contains an unexpected publication file")
        identity = item["source_id"]
        if identity != sha256(encode([manifest["tool_bundle"]["sha256"], item["instance_id"], item["package_sha256"]])):
            raise GrowthError("submitted source identity changed")
        contents = {name: artifact_bytes({"path": str(root / identity / name), "sha256": info["sha256"]})
                    for name, info in item["files"].items()}
        if any(len(raw) != item["files"][name]["bytes"] for name, raw in contents.items()):
            raise GrowthError("submitted source file size changed")
        for name, raw in contents.items():
            copied = destination / "submitted" / identity / name
            if copied.resolve() != copied:
                raise GrowthError("submitted source copy redirects outside its directory")
            seal(copied, raw)
            copied.chmod(0o444)
            hashes[str(copied.relative_to(destination))] = item["files"][name]["sha256"]
    if (destination / "submitted").exists():
        for directory in (destination / "submitted").iterdir():
            directory.chmod(0o555)
        (destination / "submitted").chmod(0o555)
    return hashes
