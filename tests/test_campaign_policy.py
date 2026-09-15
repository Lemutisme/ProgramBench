import hashlib
import os
from pathlib import Path

import pytest

from scripts.campaign_policy import PolicyArtifact, load_policy_artifact
from scripts.campaign_procontract import ProContractError, protocol_for_revision


def test_policy_artifact_preserves_unicode_and_line_endings(tmp_path: Path):
    data = "观察反例。\r\nKeep stderr bytes.\n".encode()
    path = tmp_path / "policy.txt"
    path.write_bytes(data)
    artifact = load_policy_artifact(path, hashlib.sha256(data).hexdigest())
    assert artifact.text.encode() == data
    assert protocol_for_revision("a" * 40, "artifact", runtime_protocol="policy-split-v1", policy_artifact=artifact) == artifact
    path.write_bytes(b"changed after configuration admission")
    assert artifact.text.encode() == data
    with pytest.raises(ValueError, match="SHA256"):
        load_policy_artifact(path, artifact.sha256)


@pytest.mark.parametrize("data", [b"", b" \n", b"a\0b", b"\xff", b"x" * 65537])
def test_policy_artifact_rejects_invalid_contents(tmp_path: Path, data: bytes):
    path = tmp_path / "policy.txt"
    path.write_bytes(data)
    with pytest.raises(ValueError):
        load_policy_artifact(path, hashlib.sha256(data).hexdigest())


def test_policy_artifact_read_does_not_block_on_fifo(tmp_path: Path):
    path = tmp_path / "policy.fifo"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular file"):
        load_policy_artifact(path, "a" * 64)


@pytest.mark.parametrize("runtime", [None, "current", "semantic-v0"])
def test_artifact_requires_explicit_native_policy_binding(runtime: str | None):
    artifact = PolicyArtifact("Probe", hashlib.sha256(b"Probe").hexdigest())
    with pytest.raises(ProContractError, match="policy-split"):
        protocol_for_revision("a" * 40, "artifact", runtime_protocol=runtime, policy_artifact=artifact)


def test_artifact_cannot_be_silently_ignored_or_substituted():
    artifact = PolicyArtifact("Probe", hashlib.sha256(b"Probe").hexdigest())
    with pytest.raises(ProContractError, match="conflicts"):
        protocol_for_revision("a" * 40, "behavioral", runtime_protocol="policy-split-v1", policy_artifact=artifact)
    with pytest.raises(ProContractError, match="exact bytes"):
        protocol_for_revision("a" * 40, "artifact", runtime_protocol="policy-split-v1")
