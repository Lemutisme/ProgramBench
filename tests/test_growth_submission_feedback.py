import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from scripts import growth_submission_feedback
from scripts.growth_feedback import extract_source, materialize_feedback, prepare_feedback, validate_config, verify_bundle
from scripts.growth_loop import GrowthError, sha256
from scripts.growth_native import closed_feedback
from tests.test_growth_feedback import source, write


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.PAX_FORMAT) as stream:
        for name, kind, content in entries:
            item = tarfile.TarInfo(name)
            item.mode = 0o755 if name == "compile.sh" else 0o644
            if kind == "file":
                item.size = len(content)
                stream.addfile(item, io.BytesIO(content))
                continue
            item.type = {"link": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE, "directory": tarfile.DIRTYPE,
                         "fifo": tarfile.FIFOTYPE}[kind]
            item.linkname = content
            stream.addfile(item)
    return output.getvalue()


def deliver(job, payload):
    result = json.loads((job / "procontract-result.json").read_bytes())
    result.update(completion="discharged", workspace_frozen=True, outstanding=[],
                  package_hash=sha256(payload), subject_hash="d" * 40)
    write(job / "procontract-result.json", result)
    write(job / "procontract-package.json", {"version": 1, "path": job.name + "/submission.tar.gz",
                                             "sha256": sha256(payload)})
    (job / "submission.tar.gz").write_bytes(payload)
    state = json.loads((job / "state.json").read_bytes())
    state.update(phase="done", artifact_hashes={name: sha256((job / name).read_bytes())
                 for name in ("procontract-result.json", "procontract-package.json", "submission.tar.gz")})
    write(job / "state.json", state)


def consumer(study, bundle, *, enabled=True):
    return {**study, "rounds": [], "feedback": {**study["feedback"], "sources": [bundle], "submitted_sources": enabled}}


def test_closed_delivered_source_reaches_inline_context_and_exact_regular_files(tmp_path):
    study, root, ref, jobs = source(tmp_path / "origin", delivered=True)
    compile_source = b"#!/bin/sh\nexec python3 src/main.py\n"
    payload = archive([("compile.sh", "file", compile_source), ("src/main.py", "file", b"print('implementation')\n"),
                       ("dangling", "link", "missing-input")])
    deliver(jobs[0], payload)
    # An undelivered job's leftover package is not an admitted source.
    (jobs[1] / "submission.tar.gz").write_bytes(b"PRIVATE_UNDELIVERED_PACKAGE")
    bundle = extract_source(ref, root, "first", "incumbent", 7, tmp_path / "tools")
    original = (tmp_path / "tools/manifest.json").read_bytes()
    assert set(verify_bundle(bundle, study["development_instances"])["files"]) == {"actions.jsonl", "index.jsonl", "summary.json"}
    future = consumer(study, bundle)
    first = closed_feedback(future, tmp_path / "future", 0)
    assert first == closed_feedback(future, tmp_path / "future", 0)
    public = first["tool_feedback"]["sources"][0]["submitted_sources"]
    delivered, unavailable = public["programs"]
    assert delivered["availability"] == "delivered"
    assert delivered["compile"]["preview"] == compile_source.decode()
    assert delivered["compile"]["sha256"] == sha256(compile_source)
    assert delivered["inventory"]["source_member_hints"] == ["compile.sh", "src/main.py"]
    assert delivered["inventory"]["kinds"] == {"file": 2, "symlink": 1}
    assert unavailable["availability"] == "not_delivered"
    assert "files" not in unavailable
    assert all(secret not in json.dumps(first) for secret in ["PRIVATE_UNDELIVERED_PACKAGE", "REASONING_SECRET",
        "ASSISTANT_TEXT_SECRET", "VERIFICATION_SECRET", "PRIVATE_P_TASK", "UNOPENED_P_TASK", "P_WINNER_SECRET"])
    receipt = materialize_feedback(future, tmp_path / "future", 0, tmp_path / "visible")
    assert receipt["public"] == first["tool_feedback"]
    assert len(receipt["files"]) == 7
    for name, info in delivered["files"].items():
        path = tmp_path / "visible" / Path(info["path"]).relative_to("/candidate/feedback")
        assert path.is_file() and not path.is_symlink()
        assert sha256(path.read_bytes()) == info["sha256"]
        assert path.stat().st_mode & 0o777 == 0o444
        if name == "submission.tar.gz":
            assert path.read_bytes() == payload
        if name == "index.json":
            assert json.loads(path.read_bytes())["dangling"]["target"] == "missing-input"
    assert not any(path.is_symlink() for path in (tmp_path / "visible").rglob("*"))
    installed = {name: str(tmp_path / "visible" / Path(info["path"]).relative_to("/candidate/feedback"))
                 for name, info in delivered["files"].items()}
    read = subprocess.run([sys.executable, "-c", public["read_regular_member_argv"][2], installed["index.json"],
        installed["submission.tar.gz"], "src/main.py", "0", "8192"], check=True, capture_output=True, timeout=5)
    assert read.stdout == b"print('implementation')\n"
    symlink = subprocess.run([sys.executable, "-c", public["read_regular_member_argv"][2], installed["index.json"],
        installed["submission.tar.gz"], "dangling", "0", "8192"], capture_output=True, timeout=5)
    assert symlink.returncode != 0 and symlink.stdout == b""
    assert (tmp_path / "tools/manifest.json").read_bytes() == original


def test_opt_out_does_not_collect_submitted_artifact(tmp_path):
    study, root, ref, jobs = source(tmp_path / "origin", delivered=True, tasks=("task-a",))
    # This would fail package parsing if source collection ran.
    deliver(jobs[0], b"NOT_AN_ARCHIVE")
    bundle = extract_source(ref, root, "first", "incumbent", 7, tmp_path / "tools")
    disabled = consumer(study, bundle, enabled=False)
    receipt = materialize_feedback(disabled, tmp_path / "future", 0, tmp_path / "visible")
    assert len(receipt["files"]) == 4
    assert "submitted_sources" not in receipt["public"]["sources"][0]
    assert not (tmp_path / "future/submitted-feedback").exists()


@pytest.mark.parametrize(("value",), [(value,) for value in [None, 1, "true", {}]])
def test_submitted_source_activation_is_an_explicit_boolean(value):
    with pytest.raises(GrowthError):
        validate_config({"feedback": {"protocol": "native-tool-feedback-v1", "sources": [], "submitted_sources": value}})


@pytest.mark.parametrize(("drift",), [(value,) for value in ["package_hash", "published_hash", "package_path", "symlink", "bytes"]])
def test_delivered_package_binding_and_canonical_path_are_required(tmp_path, drift):
    study, root, ref, jobs = source(tmp_path / "origin", delivered=True, tasks=("task-a",))
    job = jobs[0]
    payload = archive([("compile.sh", "file", b"#!/bin/sh\n")])
    deliver(job, payload)
    state = json.loads((job / "state.json").read_bytes())
    if drift in {"package_hash", "package_path"}:
        package = json.loads((job / "procontract-package.json").read_bytes())
        package["sha256" if drift == "package_hash" else "path"] = "e" * 64 if drift == "package_hash" else "../other/submission.tar.gz"
        write(job / "procontract-package.json", package)
        state["artifact_hashes"]["procontract-package.json"] = sha256((job / "procontract-package.json").read_bytes())
    if drift == "published_hash":
        state["artifact_hashes"]["submission.tar.gz"] = "e" * 64
    if drift == "symlink":
        (job / "submission.tar.gz").rename(job / "original.tar.gz")
        (job / "submission.tar.gz").symlink_to("original.tar.gz")
    if drift == "bytes":
        (job / "submission.tar.gz").write_bytes(payload + b"CHANGED")
    write(job / "state.json", state)
    bundle = extract_source(ref, root, "first", "incumbent", 7, tmp_path / "tools")
    with pytest.raises(GrowthError):
        prepare_feedback(consumer(study, bundle), tmp_path / "future", 0)
    assert not list((tmp_path / "future").rglob("submission.tar.gz"))


@pytest.mark.parametrize(("missing",), [(value,) for value in ["archive", "binding"]])
def test_missing_historical_artifact_is_explicitly_unavailable(tmp_path, missing):
    study, root, ref, jobs = source(tmp_path / "origin", delivered=True, tasks=("task-a",))
    deliver(jobs[0], archive([("compile.sh", "file", b"#!/bin/sh\n")]))
    if missing == "archive":
        (jobs[0] / "submission.tar.gz").unlink()
    if missing == "binding":
        state = json.loads((jobs[0] / "state.json").read_bytes())
        del state["artifact_hashes"]["procontract-package.json"]
        write(jobs[0] / "state.json", state)
    bundle = extract_source(ref, root, "first", "incumbent", 7, tmp_path / "tools")
    publication = prepare_feedback(consumer(study, bundle), tmp_path / "future", 0)
    program = json.loads(Path(publication["path"]).read_bytes())["public"]["sources"][0]["submitted_sources"]["programs"][0]
    assert program["availability"] == "unavailable"
    assert program["reason"] == ("published_archive_missing" if missing == "archive" else "missing_package_binding")
    assert program["score_validity"] == "not_inferred" and "files" not in program


@pytest.mark.parametrize(("entry",), [(value,) for value in [
    ("../escape", "file", b"bad"), ("/absolute", "file", b"bad"), ("/", "directory", ""),
    ("hard", "hardlink", "compile.sh"),
    ("fifo", "fifo", ""), ("out", "link", "../private"), ("out", "link", "/private"),
    ("cycle", "link", "cycle"), ("compile.sh", "file", b"duplicate"),
]])
def test_unsafe_members_never_get_extracted_or_admitted(entry):
    with pytest.raises(GrowthError):
        growth_submission_feedback.source_members(archive([("compile.sh", "file", b"safe"), entry]))


@pytest.mark.parametrize(("prefix",), [(value,) for value in ["link", "file"]])
def test_archive_member_cannot_be_beneath_a_nondirectory(prefix):
    with pytest.raises(GrowthError, match="non-directory"):
        growth_submission_feedback.source_members(archive([("compile.sh", "file", b"safe"),
            ("parent", prefix, "contained" if prefix == "link" else b"file"), ("parent/child", "file", b"bad")]))


def test_archive_rejects_declared_oversize_before_reading_member_bytes():
    member = tarfile.TarInfo("compile.sh")
    member.size = growth_submission_feedback.SOURCE_LIMIT + 1
    payload = member.tobuf() + b"\0" * 1024
    with pytest.raises(GrowthError, match="materialization"):
        growth_submission_feedback.source_members(payload)


def test_directory_aliases_do_not_hide_duplicate_paths():
    with pytest.raises(GrowthError, match="duplicate"):
        growth_submission_feedback.source_members(archive([("compile.sh", "file", b"safe"),
            ("directory", "directory", ""), ("./directory/", "directory", "")]))


def test_normal_tar_root_prefix_and_task_fixture_names_preserve_archive_identity():
    payload = archive([(".", "directory", ""), ("./compile.sh", "file", b"safe"),
                       ("./.env", "file", b"fixture"), ("auth.json", "file", b"fixture")])
    members, preview = growth_submission_feedback.source_members(payload)
    assert preview == b"safe"
    assert members["compile.sh"]["archive_member"] == "./compile.sh"
    assert members[".env"]["sha256"] == sha256(b"fixture")
    assert members["auth.json"]["kind"] == "file"


def test_compile_preview_is_bounded_and_tied_to_complete_source(tmp_path):
    study, root, ref, jobs = source(tmp_path / "origin", delivered=True, tasks=("task-a",))
    body = b"x" * (growth_submission_feedback.COMPILE_PREVIEW_BYTES + 1)
    deliver(jobs[0], archive([("compile.sh", "file", body)]))
    bundle = extract_source(ref, root, "first", "incumbent", 7, tmp_path / "tools")
    publication = prepare_feedback(consumer(study, bundle), tmp_path / "future", 0)
    program = json.loads(Path(publication["path"]).read_bytes())["public"]["sources"][0]["submitted_sources"]["programs"][0]
    assert program["compile"]["truncated"] is True
    assert program["compile"]["bytes"] == len(body)
    assert program["compile"]["sha256"] == sha256(body)
    assert len(program["compile"]["preview"].encode()) == growth_submission_feedback.COMPILE_PREVIEW_BYTES
    assert program["read_entry"]["archive"].endswith("/submission.tar.gz")
    assert program["read_entry"]["compile_preview"].endswith("/compile.preview.txt")


def test_cross_stage_bundle_rejected_before_submitted_source_publication(tmp_path):
    study, root, ref, jobs = source(tmp_path / "origin", delivered=True)
    deliver(jobs[0], archive([("compile.sh", "file", b"safe")]))
    bundle = extract_source(ref, root, "first", "incumbent", 7, tmp_path / "tools")
    future = {**consumer(study, bundle), "development_instances": ["another-stage"]}
    with pytest.raises(GrowthError, match="outside the declared D"):
        prepare_feedback(future, tmp_path / "future", 0)
    assert not (tmp_path / "future").exists()


def test_sealed_source_copy_detects_archive_drift(tmp_path):
    study, root, ref, jobs = source(tmp_path / "origin", delivered=True, tasks=("task-a",))
    deliver(jobs[0], archive([("compile.sh", "file", b"safe")]))
    bundle = extract_source(ref, root, "first", "incumbent", 7, tmp_path / "tools")
    publication = prepare_feedback(consumer(study, bundle), tmp_path / "future", 0)
    submitted = json.loads(Path(publication["path"]).read_bytes())["submitted_sources"][0]
    path = next(Path(submitted["path"]).parent.rglob("submission.tar.gz"))
    path.chmod(0o600)
    path.write_bytes(b"drift")
    with pytest.raises(GrowthError, match="changed"):
        growth_submission_feedback.materialize_submissions(submitted, tmp_path / "copy")
    assert not (tmp_path / "copy").exists()
