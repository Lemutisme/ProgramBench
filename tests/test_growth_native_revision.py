import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.growth_loop import GrowthError, encode, policy, read, record, seal, sha256
from scripts.growth_native import REVISION_ARTIFACTS, closed_feedback, generator_payload, native_generation


def proposal(tmp_path):
    artifact = policy(tmp_path / "policies", b"Use observed task behavior.\n")
    specification = {"mutable_objects": ["agent_revision"],
                     "binding": {"solver_policy": artifact, "generator_policy": artifact,
                                 "agent_revision": {"path": "/trusted/parent.json", "sha256": "a" * 64}}}
    destination = tmp_path / "subject"
    destination.mkdir()
    (destination / "agent-revision.json").write_text(json.dumps({
        "parent_sha256": "a" * 64, "rationale": "Recover confirmed uncalled tool input failures.",
        "metadata": {"expected_effect": "Reduce repeated uncalled failures.", "failure_modes": ["False detection."],
                     "permission_changes": [], "state_migration": None, "recovery": "Use parent for new tasks."}}))
    (destination / "source.patch").write_bytes(b"")
    (destination / "solver-policy.txt").write_text("Use differential observations.\n")
    (destination / "generator-policy.txt").write_text("Test a falsifiable mechanism.\n")
    payload = generator_payload(tmp_path / "operation", specification, "Produce an AgentRevision.",
                                "Closed observations only.", "openai/gpt-5.6-luna", "max", started_at=1234.5)
    return specification, destination, payload


def replay(payload, directory):
    check = payload["evidence"]["replay"]["checks"][0]
    return subprocess.run([sys.executable, *check["argv"][1:]], cwd=directory, capture_output=True, timeout=10)


class NativeHandoff:
    """A closed transport fixture; generation itself remains native-owned."""

    def __init__(self):
        self.payload = None
        self.attestations = []
        self.interrupted = []

    def issue(self, payload):
        self.payload = payload
        return {"data": {"id": payload["id"]}}

    def get_execution(self, identifier):
        assert identifier == self.payload["id"]
        return {"sessionID": "native-session", "executionPolicy": self.payload["executionPolicy"]}

    def get_contract(self, identifier):
        assert identifier == self.payload["id"]
        return {"status": "discharged" if self.attestations else "verification", "revision": 1,
                "spec": {"budget": self.payload["budget"]}, "handoff": {
                    "subjectHash": "b" * 64, "replay": {"passed": True}}}

    def attest(self, identifier, digest):
        self.attestations.append([identifier, digest])
        return {"data": {"id": identifier}}

    def _request(self, method, endpoint):
        assert method == "GET" and endpoint.startswith("/api/session/native-session/message?")
        return {"data": [], "cursor": {"next": None}}

    def _mapping(self, response):
        return response

    def interrupt(self, identifier):
        self.interrupted.append(identifier)


def handoff(tmp_path, specification, destination, payload):
    api = NativeHandoff()
    result = native_generation(api, tmp_path / "operation", specification, payload,
        lambda identifier: (destination, {"contractID": identifier, "subjectHash": "b" * 64}))
    return result, api


def test_revision_replay_and_export_preserve_typed_artifacts_and_parent(tmp_path):
    specification, destination, payload = proposal(tmp_path)
    assert replay(payload, destination).returncode == 0
    assert payload["budget"] == {"deadline": 22834500}
    assert payload["executionPolicy"] == "Use observed task behavior.\n"
    assert "/trusted/parent.json" not in payload["brief"]
    assert "/candidate/agent-source" in payload["brief"]
    assert "source.patch" in payload["evidence"]["replay"]["artifacts"]
    result, api = handoff(tmp_path, specification, destination, payload)
    assert set(result["artifacts"]) == set(REVISION_ARTIFACTS)
    assert result["parent_revision"] == specification["binding"]["agent_revision"]
    assert result["artifacts"]["source_patch"]["sha256"] == sha256(b"")
    assert len(api.attestations) == 1 and api.interrupted == ["native-session"]
    assert read(tmp_path / "operation/handoff-evidence.json")["artifacts"] == result["artifacts"]
    assert not (tmp_path / "operation/revision").exists()


@pytest.mark.parametrize("malformation", ["parent", "authority", "rationale", "metadata", "recovery", "symlink", "nul", "large", "policy"])
def test_native_replay_and_host_both_reject_malformed_revision_proposals(tmp_path, malformation):
    specification, destination, payload = proposal(tmp_path)
    path = destination / "agent-revision.json"
    value = json.loads(path.read_bytes())
    if malformation == "parent":
        value["parent_sha256"] = "c" * 64
    if malformation == "authority":
        value["authority"] = ["adopt"]
    if malformation == "rationale":
        value["rationale"] = []
    if malformation == "metadata":
        value.pop("metadata")
    if malformation == "recovery":
        value["metadata"]["recovery"] = ""
    path.write_text(json.dumps(value))
    if malformation == "symlink":
        target = tmp_path / "outside"
        path.rename(target)
        path.symlink_to(target)
    if malformation == "nul":
        (destination / "source.patch").write_bytes(b"\0")
    if malformation == "large":
        with (destination / "source.patch").open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
    if malformation == "policy":
        (destination / "generator-policy.txt").write_text("   \n")
    assert replay(payload, destination).returncode != 0
    api = NativeHandoff()
    with pytest.raises((GrowthError, OSError, ValueError)):
        native_generation(api, tmp_path / "operation", specification, payload,
            lambda identifier: (destination, {"contractID": identifier, "subjectHash": "b" * 64}))
    assert api.attestations == [] and api.interrupted == ["native-session"]


def test_policy_only_native_contract_keeps_existing_artifact_shape(tmp_path):
    artifact = policy(tmp_path, b"A policy.\n")
    specification = {"mutable_objects": ["solver_policy"],
                     "binding": {"solver_policy": artifact, "generator_policy": artifact}}
    payload = generator_payload(tmp_path / "operation", specification, "Improve policy.", "D feedback.",
                                "openai/gpt-5.6-luna", "max", started_at=1234.5)
    destination = tmp_path / "subject"
    destination.mkdir()
    (destination / "solver-policy.txt").write_text("A changed policy.\n")
    assert replay(payload, destination).returncode == 0
    result, api = handoff(tmp_path, specification, destination, payload)
    assert set(result["artifacts"]) == {"solver_policy"}
    assert "parent_revision" not in result
    assert len(api.attestations) == 1 and api.interrupted == ["native-session"]
    assert Path(result["artifacts"]["solver_policy"]["path"]).read_text() == "A changed policy.\n"


def test_revision_feedback_exposes_source_change_without_host_build_or_receipt_paths(tmp_path):
    def ref(name, data):
        path = tmp_path / name
        seal(path, data)
        return {"path": str(path), "sha256": sha256(data)}

    proposal_ref = ref("proposal.json", encode({"parent_sha256": "a" * 64, "rationale": "A concrete runtime change.",
        "metadata": {"expected_effect": "Better recovery.", "failure_modes": ["False interruption."],
                     "permission_changes": [], "state_migration": None, "recovery": "Use the parent."}}))
    patch = ref("source.patch", b"diff --git a/runtime.py b/runtime.py\n")
    revision = ref("manifest.json", encode({"parent": {"path": "/private/parent.json", "sha256": "a" * 64},
        "source": {"repo": "/private/source", "tree_sha256": "b" * 64}, "recipe": {"path": "/private/recipe"},
        "qualification_receipts": [{"path": "/private/qualification"}], "proposal": proposal_ref,
        "source_patch": patch, "changed_paths": ["runtime.py"]}))
    record(tmp_path / "g0/decision.json", {"winner": None, "generation_results": {"candidate-000": {
        "status": "completed", "artifacts": {"agent_revision": revision}}}, "screen": {"comparisons": {}},
        "comparisons": {}, "confirmation_consumed": False})
    result = closed_feedback({"development_instances": ["d"], "rounds": [{"id": "g0",
        "confirmation_instances": ["p0"]}]}, tmp_path, 1)
    artifact = result["generations"][0]["proposals_including_rejected"]["candidate-000"]["artifacts"]["agent_revision"]
    assert artifact["source_sha256"] == "b" * 64 and artifact["changed_paths"] == ["runtime.py"]
    assert artifact["source_patch"]["text"] == "diff --git a/runtime.py b/runtime.py\n"
    assert artifact["metadata"]["expected_effect"] == "Better recovery."
    assert "/private/" not in json.dumps(result) and str(tmp_path) not in json.dumps(result)
