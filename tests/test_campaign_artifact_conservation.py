import hashlib
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from programbench.candidate import package_workspace, sha256
from scripts.campaign_runner import CampaignJob, _conserve_candidate_artifact
from scripts.campaign_state import JobPaths, JobState, StateStore


@pytest.mark.parametrize(("published",), [(False,), (True,)])
def test_conservation_keeps_sealed_package_after_delivery_failure(tmp_path: Path, published: bool) -> None:
    paths = JobPaths(tmp_path / "job")
    subject = tmp_path / "subject"
    subject.mkdir()
    subject.joinpath("compile.sh").write_text("#!/bin/sh\nexit 0\n")
    subject.joinpath("source.txt").write_text("frozen candidate\n")
    candidate = CampaignJob("test__candidate.abc", "procontract", "unused")
    archive = package_workspace(subject, paths.root, candidate.instance_id)
    sealed = archive.read_bytes()
    record = paths.root / "procontract-package.json"
    record.write_text(
        json.dumps({"version": 1, "path": str(archive.relative_to(paths.root)), "sha256": sha256(archive)})
    )
    if published:
        paths.submission.hardlink_to(archive)
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    workspace.joinpath("source.txt").write_text("different live workspace\n")
    store = StateStore(paths.state)
    store.write(JobState("planned"))
    store.transition("failed", command_identity="test", expected_generation=0, exit_class="delivery_failed")

    for _ in range(2):
        assert _conserve_candidate_artifact(
            paths, store, SimpleNamespace(candidate=workspace), candidate, "delivery_failed"
        )
        assert archive.read_bytes() == paths.submission.read_bytes() == sealed
        assert json.loads(record.read_text())["sha256"] == sha256(archive)
        receipt = json.loads(paths.root.joinpath("candidate-artifact.json").read_text())
        assert receipt["sha256"] == hashlib.sha256(sealed).hexdigest()
        assert receipt["partial"] is True
        assert store.read().phase == "failed"
        assert store.read().exit_class == "delivery_failed"
        assert not paths.root.joinpath("procontract-preflight.json").exists()
        assert not paths.root.joinpath("procontract-attestation.json").exists()


@pytest.mark.parametrize(("damage",), [("hash",), ("missing",)])
def test_conservation_does_not_replace_invalid_sealed_package(tmp_path: Path, damage: str) -> None:
    paths = JobPaths(tmp_path / "job")
    paths.root.mkdir()
    candidate = CampaignJob("test__candidate.abc", "procontract", "unused")
    archive = paths.root / candidate.instance_id / "submission.tar.gz"
    archive.parent.mkdir()
    if damage == "hash":
        archive.write_bytes(b"damaged package")
    record = paths.root / "procontract-package.json"
    record.write_text(json.dumps({"version": 1, "path": str(archive.relative_to(paths.root)), "sha256": "a" * 64}))
    before = record.read_bytes()
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    workspace.joinpath("source.txt").write_text("unsealed candidate\n")
    store = StateStore(paths.state)
    store.write(JobState("planned"))
    store.transition("failed", command_identity="test", expected_generation=0, exit_class="delivery_failed")

    assert not _conserve_candidate_artifact(
        paths, store, SimpleNamespace(candidate=workspace), candidate, "delivery_failed"
    )
    assert record.read_bytes() == before
    assert (archive.read_bytes() if archive.exists() else None) == (b"damaged package" if damage == "hash" else None)
    assert not paths.submission.exists()
    assert not paths.root.joinpath("candidate-artifact.json").exists()
    assert paths.root.joinpath("candidate-artifact-failure.json").is_file()


def test_partial_conservation_does_not_overwrite_ambiguous_protocol_package(tmp_path: Path) -> None:
    paths = JobPaths(tmp_path / "job")
    candidate = CampaignJob("test__candidate.abc", "procontract", "unused")
    archive = paths.root / candidate.instance_id / "submission.tar.gz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"package written before response loss")
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    workspace.joinpath("source.txt").write_text("partial candidate\n")
    store = StateStore(paths.state)
    store.write(JobState("planned"))
    store.transition("failed", command_identity="test", expected_generation=0, exit_class="execution_failed")

    assert _conserve_candidate_artifact(
        paths, store, SimpleNamespace(candidate=workspace), candidate, "execution_failed"
    )
    assert archive.read_bytes() == b"package written before response loss"
    with tarfile.open(paths.submission) as submission:
        assert submission.getnames() == ["source.txt"]
        assert submission.extractfile("source.txt").read() == b"partial candidate\n"
    assert json.loads(paths.root.joinpath("candidate-artifact.json").read_text())["partial"] is True
