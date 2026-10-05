from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

from scripts import trajectory_study as study
from scripts.campaign_workspace import DockerCommandError, DockerNotFoundError
from scripts.campaign_workspace_fs import PathIdentity


def frozen(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    path.chmod(0o444)
    return path


def seed(tmp_path: Path, members=None) -> study.SeedSpec:
    archive_path = tmp_path / "checkpoint.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        for name, content, mode in members or [
            ("compile.sh", b"#!/bin/sh\ntrue\n", 0o755),
            ("README.md", b"Original documentation\n", 0o644),
            ("src/main.py", b"print('checkpoint')\n", 0o644),
        ]:
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            entry.mode = mode
            archive.addfile(entry, io.BytesIO(content))
    archive_path.chmod(0o444)
    trajectory = frozen(tmp_path / "public.json", json.dumps({"version": 1, "kind": "public-trajectory",
        "purpose": "development-only", "records": [{"tool": "bash", "output": "public reference observation"}]}).encode())
    context = frozen(tmp_path / "context.md", b"This is a new six-hour continuation task, not an extension of the previous task.\n")
    return study.SeedSpec("owner__task.abc", archive_path, study._sha256(archive_path),
                          trajectory, study._sha256(trajectory), context, study._sha256(context))


class FakeDocker:
    """Exercise the real owned helper command and copy script with local files."""
    def __init__(self):
        self.commands = []
        self.fail = False
        self.events = []

    def __call__(self, command, timeout):
        self.commands.append((command, timeout))
        if command[1] == "inspect":
            raise DockerNotFoundError("not found")
        assert command[1] == "run"
        self.events.append("copy")
        assert timeout == 90
        assert command[command.index("--network") + 1] == "none"
        assert "--read-only" in command
        assert command[command.index("--cap-drop") + 1] == "ALL"
        assert command[command.index("--security-opt") + 1] == "no-new-privileges"
        assert "--label" in command
        mounts = [command[index + 1] for index, value in enumerate(command) if value == "-v"]
        assert len(mounts) == 2
        assert command[command.index("--user") + 1] != "root"
        assert "-e" not in command and "--env" not in command
        stage = next(value.removesuffix(":/seed:ro") for value in mounts if value.endswith(":/seed:ro"))
        candidate = next(value.removesuffix(":/candidate:rw") for value in mounts if value.endswith(":/candidate:rw"))
        if self.fail:
            (Path(candidate) / "partial").write_text("uncertain partial seed")
            raise RuntimeError("injected helper termination")
        subprocess.run([sys.executable, "-I", "-S", "-c", command[command.index("-c") + 1], stage, candidate], check=True, timeout=5)
        return ""


def workspace(tmp_path: Path, spec: study.SeedSpec, monkeypatch):
    root = tmp_path / "new-task"
    candidate = root / "candidate"
    candidate.mkdir(parents=True)
    (candidate / "reference").symlink_to("/workspace/executable")
    (candidate / ".git").mkdir()
    (candidate / ".git" / "config").write_text("preserved git state")
    (candidate / ".programbench").mkdir()
    (candidate / ".programbench" / "owner").write_text("owner")
    (candidate / ".programbench-cleanroom.json").write_text('{"owner":"new-task"}')
    (candidate / "README.md").write_text("Initial image documentation")
    (candidate / "stale-image-file").write_text("must not contaminate source checkpoint")
    state_root = root / "state"
    control = state_root / "provider-control"
    control.mkdir(parents=True)
    # Use the real gateway schema and its mandatory denied-readiness record,
    # not a hand-written empty DB that hides startup activity.
    import os
    from scripts.campaign_provider_gateway import Gateway
    gateway = Gateway(state_root / "provider-channel", control, "test-model", "max",
                      upstream="http://127.0.0.1:1/v1", api_key="unused-test-key")
    gateway.deny("caller_not_admitted_runtime", 403)
    gateway.server_close()
    gateway.db.close()
    os.close(gateway.directory_fd)
    docker = FakeDocker()
    manifest = SimpleNamespace(path_identities=(PathIdentity.capture("candidate", candidate),),
        candidate=candidate, platform="linux/amd64", image_digest="sha256:" + "a" * 64,
        container_name="programbench-test", state_root=state_root, campaign="study", job_id="a" * 64)
    result = SimpleNamespace(candidate=candidate, manifest=manifest, docker="docker", docker_runner=docker)
    result.stop_for_freeze = lambda: docker.events.append("stop")
    restarted = object()
    def restart():
        docker.events.append("restart")
        return restarted
    result.restart = restart
    admitted = []
    def ensure(request, lease):
        admitted.append((request, lease))
        return result
    monkeypatch.setattr(study, "ensure_workspace", ensure)
    request = SimpleNamespace(campaign="study", instance_id=spec.instance_id, arm="procontract", deadline_only=True,
        isolated=True, job=SimpleNamespace(root=root), state_root=state_root)
    return request, result, restarted, docker, admitted


def test_regular_exact_inputs_seed_once_with_owned_stopped_helper(tmp_path, monkeypatch):
    spec = seed(tmp_path)
    request, current, restarted, docker, admitted = workspace(tmp_path, spec, monkeypatch)
    identity = PathIdentity.capture("candidate", current.candidate)
    lease = object()
    assert study.workspace_factory(spec)(request, lease) is restarted
    identity.verify()
    assert admitted == [(request, lease)]
    assert docker.events == ["stop", "copy", "restart"]
    assert (current.candidate / ".git/config").read_text() == "preserved git state"
    assert (current.candidate / ".programbench/owner").read_text() == "owner"
    assert (current.candidate / ".programbench-cleanroom.json").read_text() == '{"owner":"new-task"}'
    assert (current.candidate / "reference").readlink() == Path("/workspace/executable")
    assert not (current.candidate / "stale-image-file").exists()
    assert (current.candidate / "README.md").read_bytes() == b"Original documentation\n"
    assert (current.candidate / "TRAJECTORY.public.json").read_bytes() == spec.trajectory.read_bytes()
    assert (current.candidate / "STUDY-CONTEXT.md").read_bytes() == spec.context.read_bytes()
    assert stat.S_IMODE((current.candidate / "compile.sh").stat().st_mode) == 0o755
    assert (current.candidate / "compile.sh").stat().st_uid == current.candidate.stat().st_uid
    receipt = study._read_json(request.job.root / "trajectory-seed.json")
    assert receipt["spec"] == spec.manifest()
    assert receipt["files"] == study._inventory(current.candidate)
    assert not (request.job.root / "procontract-issue.json").exists()
    assert study.workspace_factory(spec)(request, lease) is current
    assert docker.events == ["stop", "copy", "restart"]


def test_admitted_task_adoption_never_restores_old_candidate_or_deadline(tmp_path, monkeypatch):
    spec = seed(tmp_path)
    request, current, _, docker, _ = workspace(tmp_path, spec, monkeypatch)
    factory = study.workspace_factory(spec)
    factory(request, object())
    issue = request.job.root / "procontract-issue.json"
    issue.write_text('{"deadline":12345,"scope":"existing-new-study"}')
    (current.candidate / "src/main.py").write_text("new work after issue")
    factory(request, object())
    assert (current.candidate / "src/main.py").read_text() == "new work after issue"
    assert issue.read_text() == '{"deadline":12345,"scope":"existing-new-study"}'
    assert docker.events.count("copy") == 1


@pytest.mark.parametrize("name", ["procontract-issue.json", "procontract-issue-active.json", "procontract-issue-response.json", "submission.tar.gz"])
def test_existing_unseeded_issue_fails_before_workspace_admission(tmp_path, monkeypatch, name):
    spec = seed(tmp_path)
    request, current, _, _, admitted = workspace(tmp_path, spec, monkeypatch)
    (request.job.root / name).write_text("{}")
    with pytest.raises(study.StudyError, match="existing issue"):
        study.workspace_factory(spec)(request, object())
    assert admitted == []
    assert (current.candidate / "stale-image-file").exists()


def test_interrupted_partial_seed_is_never_retried_or_started(tmp_path, monkeypatch):
    spec = seed(tmp_path)
    request, current, _, docker, admitted = workspace(tmp_path, spec, monkeypatch)
    docker.fail = True
    with pytest.raises(DockerCommandError):
        study.workspace_factory(spec)(request, object())
    assert docker.events == ["stop", "copy"]
    assert (request.job.root / "trajectory-seed-pending.json").exists()
    assert not (request.job.root / "trajectory-seed.json").exists()
    docker.fail = False
    with pytest.raises(study.StudyError, match="ambiguous"):
        study.workspace_factory(spec)(request, object())
    assert len(admitted) == 1
    assert (current.candidate / "partial").exists()


@pytest.mark.parametrize("table", ["request", "execution_budget"])
def test_previous_gateway_admission_prevents_seeding(tmp_path, monkeypatch, table):
    spec = seed(tmp_path)
    request, _, _, docker, _ = workspace(tmp_path, spec, monkeypatch)
    with sqlite3.connect(request.state_root / "provider-control/requests.db") as connection:
        connection.execute("INSERT INTO request(id) VALUES (2)" if table == "request" else
                           "INSERT INTO execution_budget(identity) VALUES ('test')")
    with pytest.raises(study.StudyError, match="already admitted"):
        study.workspace_factory(spec)(request, object())
    assert docker.events == ["stop"]


def test_preissue_seed_changes_fail_closed_but_completed_receipt_can_restart(tmp_path, monkeypatch):
    spec = seed(tmp_path)
    request, current, _, docker, _ = workspace(tmp_path, spec, monkeypatch)
    factory = study.workspace_factory(spec)
    factory(request, object())
    (current.candidate / "src/main.py").write_text("different initial treatment")
    with pytest.raises(study.StudyError, match="starting point"):
        factory(request, object())
    assert docker.events.count("copy") == 1


def test_source_policy_change_is_not_silently_adopted(tmp_path, monkeypatch):
    spec = seed(tmp_path)
    request, _, _, _, _ = workspace(tmp_path, spec, monkeypatch)
    study.workspace_factory(spec)(request, object())
    other = frozen(tmp_path / "other-context", b"different facts")
    with pytest.raises(study.StudyError, match="conflicts"):
        study.workspace_factory(replace(spec, context=other, context_sha256=study._sha256(other)))(request, object())


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/../escape", "./x", "a//b", "a\\b", "reference", ".git/config", "a/.programbench/key", "executable", "TRAJECTORY.public.json", "a\nline"])
def test_unsafe_or_control_archive_paths_are_rejected_before_candidate_copy(tmp_path, name):
    spec = seed(tmp_path, [("compile.sh", b"true", 0o755), (name, b"bad", 0o644)])
    target = tmp_path / "stage"
    target.mkdir()
    with pytest.raises(study.StudyError, match="path"):
        study._stage(spec, target)
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE])
def test_archive_links_and_special_files_are_rejected(tmp_path, kind):
    spec = seed(tmp_path)
    spec.checkpoint.chmod(0o644)
    with tarfile.open(spec.checkpoint, "w:gz") as archive:
        entry = tarfile.TarInfo("bad")
        entry.type = kind
        entry.linkname = "../victim"
        archive.addfile(entry)
    spec.checkpoint.chmod(0o444)
    spec = replace(spec, checkpoint_sha256=study._sha256(spec.checkpoint))
    target = tmp_path / "stage"
    target.mkdir()
    with pytest.raises(study.StudyError, match="ordinary"):
        study._stage(spec, target)


def test_duplicate_paths_and_expansion_bound_fail_before_extract(tmp_path, monkeypatch):
    spec = seed(tmp_path, [("compile.sh", b"1", 0o755), ("compile.sh", b"2", 0o755)])
    target = tmp_path / "stage"
    target.mkdir()
    with pytest.raises(study.StudyError, match="duplicate"):
        study._stage(spec, target)
    monkeypatch.setattr(study, "MAX_EXPANDED_BYTES", 0)
    with pytest.raises(study.StudyError, match="bounded"):
        study._stage(spec, target)


@pytest.mark.parametrize("kind", ["writable", "hash", "hardlink", "symlink", "raw-trajectory", "credential"])
def test_frozen_input_validation(tmp_path, monkeypatch, kind):
    spec = seed(tmp_path)
    if kind == "writable":
        spec.context.chmod(0o644)
    elif kind == "hash":
        spec = replace(spec, context_sha256="0" * 64)
    elif kind == "hardlink":
        import os
        os.link(spec.context, tmp_path / "linked")
    elif kind == "symlink":
        path = tmp_path / "context-link"
        path.symlink_to(spec.context)
        spec = replace(spec, context=path)
    elif kind == "raw-trajectory":
        path = frozen(tmp_path / "raw.json", b'{"messages":{"data":[]}}')
        spec = replace(spec, trajectory=path, trajectory_sha256=study._sha256(path))
    else:
        monkeypatch.setenv("OPENAI_API_KEY", "LIVE_SECRET_ABC")
        path = frozen(tmp_path / "bad-context.md", b"LIVE_SECRET_ABC")
        spec = replace(spec, context=path, context_sha256=study._sha256(path))
    with pytest.raises(study.StudyError):
        spec.validate()


def test_seed_spec_roundtrip_binds_exact_paths_hashes_and_instance(tmp_path):
    spec = seed(tmp_path)
    path = tmp_path / "seed-spec.json"
    path.write_text(json.dumps(spec.manifest()))
    assert study.SeedSpec.load(path) == spec
    value = spec.manifest()
    value["authority"] = "self-promote"
    path.write_text(json.dumps(value))
    with pytest.raises(study.StudyError, match="exact"):
        study.SeedSpec.load(path)


def test_equal_arms_receive_equal_seed_facts_and_no_policy_injection(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    spec = seed(source)
    results = []
    for name in ("neutral", "research-policy"):
        directory = tmp_path / name
        directory.mkdir()
        request, current, _, _, _ = workspace(directory, spec, monkeypatch)
        study.workspace_factory(spec)(request, object())
        results.append(study._read_json(request.job.root / "trajectory-seed.json"))
        assert not (current.candidate / "policy.json").exists()
    assert results[0]["files"] == results[1]["files"]
    assert results[0]["spec"] == results[1]["spec"]
    assert results[0]["candidateIdentity"] != results[1]["candidateIdentity"]


@pytest.mark.parametrize("field,value", [("instance_id", "other__task"), ("arm", "vanilla"), ("deadline_only", False), ("isolated", False)])
def test_study_cannot_weaken_admission_profile(tmp_path, monkeypatch, field, value):
    spec = seed(tmp_path)
    request, _, _, _, admitted = workspace(tmp_path, spec, monkeypatch)
    setattr(request, field, value)
    with pytest.raises(study.StudyError, match="matching isolated"):
        study.workspace_factory(spec)(request, object())
    assert admitted == []


def test_receipt_cannot_be_transplanted_to_another_fresh_campaign(tmp_path, monkeypatch):
    import shutil
    source = tmp_path / "source"
    source.mkdir()
    spec = seed(source)
    first, _, _, _, _ = workspace(tmp_path / "first", spec, monkeypatch)
    study.workspace_factory(spec)(first, object())
    second, candidate, _, docker, admitted = workspace(tmp_path / "second", spec, monkeypatch)
    shutil.copyfile(first.job.root / "trajectory-seed.json", second.job.root / "trajectory-seed.json")
    with pytest.raises(study.StudyError, match="conflicts"):
        study.workspace_factory(spec)(second, object())
    assert admitted == []
    assert docker.events == []
    assert (candidate.candidate / "stale-image-file").exists()


def test_staged_tree_readable_under_restrictive_launcher_umask(tmp_path):
    import os
    spec = seed(tmp_path)
    target = tmp_path / "stage"
    target.mkdir()
    previous = os.umask(0o077)
    try:
        study._stage(spec, target)
    finally:
        os.umask(previous)
    assert stat.S_IMODE((target / "src").stat().st_mode) == 0o755
    assert stat.S_IMODE((target / "src/main.py").stat().st_mode) == 0o644


def test_cli_freezes_without_loading_hidden_tests_or_running_provider(tmp_path, monkeypatch):
    spec = seed(tmp_path)
    spec_path = tmp_path / "seed.json"
    spec_path.write_text(json.dumps(spec.manifest()))
    config = SimpleNamespace(limits=SimpleNamespace(deadline_only=True, wall_time_seconds=21600))
    monkeypatch.setattr(study, "load_config", lambda _: config)
    reads = []
    def instances(*, include_tests):
        reads.append(include_tests)
        return [{"instance_id": spec.instance_id}, {"instance_id": "unselected"}]
    monkeypatch.setattr(study, "load_all_instances", instances)
    output = tmp_path / "campaign"
    def plan(actual_config, actual_instances, **options):
        assert actual_config is config
        assert actual_instances == ({"instance_id": spec.instance_id},)
        assert options["arm_selection"] == "procontract" and options["limit"] == 1
        output.mkdir()
        manifest = output / "campaign.json"
        manifest.write_text('{"test":"immutable campaign"}')
        return SimpleNamespace(selected_ids=(spec.instance_id,), cleanup_only=False,
            output=output, manifest_path=manifest)
    monkeypatch.setattr(study, "plan_campaign", plan)
    monkeypatch.setattr(study, "run_campaign", lambda *_args, **_kwargs: pytest.fail("dry-run cannot execute a model"))
    assert study.main(["--config", str(tmp_path / "config.yaml"), "--output", str(output), "--seed-spec", str(spec_path), "--dry-run"]) == 0
    assert reads == [False]
    frozen = study._read_json(output / "trajectory-study.json")
    assert frozen["spec"] == spec.manifest()
    assert frozen["evaluation"] == "external-public-panel"
    assert frozen["automaticPromotion"] is False
    assert frozen["adapterHash"] == study._sha256(Path(study.__file__).resolve())
    assert "not an extension" in frozen["budget"]


@pytest.mark.parametrize("deadline_only, seconds", [(False, 21600), (True, 3600)])
def test_cli_cannot_change_six_hour_deadline_only_profile(tmp_path, monkeypatch, deadline_only, seconds):
    spec = seed(tmp_path)
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(spec.manifest()))
    monkeypatch.setattr(study, "load_config", lambda _: SimpleNamespace(limits=SimpleNamespace(
        deadline_only=deadline_only, wall_time_seconds=seconds)))
    monkeypatch.setattr(study, "plan_campaign", lambda *_args, **_kwargs: pytest.fail("invalid profile cannot be planned"))
    with pytest.raises(study.StudyError, match="six-hour"):
        study.main(["--config", str(tmp_path / "config.yaml"), "--output", str(tmp_path / "campaign"), "--seed-spec", str(path)])


@pytest.mark.parametrize("recover", [True, False])
def test_factory_stop_failure_has_local_bounded_cleanup_before_registration(tmp_path, recover):
    def fail():
        raise RuntimeError("initial stop failed")
    def force():
        (tmp_path / "force-attempted").write_text("yes")
        if not recover:
            raise RuntimeError("forced cleanup failed")
    workspace = SimpleNamespace(stop_for_freeze=fail, force_stop=force, cleanup_timeout_seconds=1,
        lease=SimpleNamespace(release_handle=lambda: (tmp_path / "released").write_text("yes")))
    with pytest.raises(RuntimeError, match="initial stop failed" if recover else "bounded cleanup failed"):
        study._stop_workspace(workspace)
    assert (tmp_path / "force-attempted").exists()
    assert (tmp_path / "released").exists() is recover


@pytest.mark.parametrize("field,value", [
    ("status", 200), ("status", None), ("outcome", "admitted"), ("outcome", None),
    ("peer_pid", 123), ("body_sha256", "digest"), ("usage", "{}"), ("finished", 1),
    ("metadata", "{}"), ("sent", 1), ("headers_at", 1), ("first_byte", 1),
    ("last_byte", 1), ("terminal_at", 1), ("close_reason", "eof"),
    ("error_name", "network-error"), ("error_code", 1),
])
def test_only_exact_preforward_readiness_denial_is_safe_before_seed(tmp_path, monkeypatch, field, value):
    spec = seed(tmp_path)
    request, _, _, docker, _ = workspace(tmp_path, spec, monkeypatch)
    path = request.state_root / "provider-control/requests.db"
    study._require_unused_gateway(path)
    with sqlite3.connect(path) as connection:
        connection.execute(f"UPDATE request SET {field} = ?", (value,))
    with pytest.raises(study.StudyError, match="unknown attempt"):
        study.workspace_factory(spec)(request, object())
    assert docker.events == ["stop"]


def test_initial_provider_body_even_without_request_row_blocks_seed(tmp_path, monkeypatch):
    spec = seed(tmp_path)
    request, _, _, _, _ = workspace(tmp_path, spec, monkeypatch)
    path = request.state_root / "provider-control/requests.db"
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO initial_request(request_id,body) VALUES (99,?)", (b"{}",))
    with pytest.raises(study.StudyError, match="already admitted"):
        study._require_unused_gateway(path)
