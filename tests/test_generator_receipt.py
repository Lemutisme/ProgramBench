"""Production receipt roundtrip with closed transport and actual offline revision builds."""

import json
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from scripts import campaign_install, campaign_procontract, campaign_workspace, growth_native
from scripts.agent_revision import artifact_bytes, build_revision, validate_revision
from scripts.growth_loop import GrowthError, encode, freeze, read, revision_binding, sha256
from tests.test_agent_revision import artifact, proposal
from tests.test_campaign_experiments import experiments
from tests.test_growth_native_revision import NativeHandoff
from tests.test_growth_revision import initial, study


@pytest.mark.parametrize("enabled", [False, True])
def test_generated_receipt_roundtrip_and_tampering(study, tmp_path, monkeypatch, enabled):
    specification, path, root = study
    freeze(path, root)
    parent = specification["initial"]["agent_revision"]
    if enabled:
        source = tmp_path / "enable"
        source.mkdir()
        artifacts = proposal(source, parent, validate_revision(parent)["solver_policy"])
        document = read(artifacts["revision_proposal"]["path"])
        document["runtime_options"] = {"experiments": experiments()}
        artifacts["revision_proposal"] = artifact(source, "enabled.json", encode(document))
        parent = build_revision(source / "revision", parent, artifacts, specification["revision_recipe"])
    revision = validate_revision(parent)
    binding = revision_binding(parent, specification["revision_recipe"])
    generation = {"binding": binding, "generation": 0, "mutable_objects": ["agent_revision"]}
    proposed = tmp_path / "proposed"
    proposed.mkdir()
    artifacts = proposal(proposed, parent, binding["solver_policy"])
    output = tmp_path / "generator"
    observed = []

    class Lease:
        def __init__(self, request):
            observed.append(request)
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False

    class Workspace:
        def __init__(self, request):
            self.container_id = "closed-transport-fixture"
            self.state_dir = request.state_root / "opencode-state"
            self.state_dir.mkdir(parents=True)
            self.base_url, self.port = "http://127.0.0.1:4096", 4096
            self.path = request.state_root / "workspace.json"
            self.manifest = SimpleNamespace(network_mode="none")
            self.record = {"binary_sha256": revision["binary"]["sha256"], "network_mode": "none",
                "image": specification["generator"]["image"], "phase": "running", "environment_names": ["PATH"],
                "container_id": self.container_id, "state_dir": str(self.state_dir)}
            self.path.write_bytes(encode(self.record))
        def stop(self):
            self.record["phase"] = "stopped"
            self.path.write_bytes(encode(self.record))

    class Protocol(NativeHandoff):
        """Independent fixed transport records; no native or model execution claim."""
        def issue(self, payload):
            self.payload = payload
            return {"data": self.get_contract(payload["id"])}
        def get_contract(self, identifier):
            assert identifier == self.payload["id"]
            terms = {key: self.payload[key] for key in (
                "goal", "brief", "requires", "trigger", "authority", "budget", "evidence", "resolution")}
            return {"id": identifier, "revision": 1, "scope": self.payload["scope"], "spec": terms,
                    "specHash": sha256(encode(terms)), "status": "discharged" if self.attestations else "verification",
                    "handoff": {"subjectHash": "b" * 64, "replay": {"passed": True}},
                    **({"attestationID": "closed-attestation"} if self.attestations else {})}
        def get_execution(self, identifier):
            return super().get_execution(identifier) | {"contractID": identifier, "revision": 1,
                "location": self.payload["location"], "model": self.payload["model"]}
        def attest(self, identifier, digest):
            super().attest(identifier, digest)
            return {"hash": digest, "frontier": 1}
        def export(self, workspace, identifier, destination):
            destination.mkdir()
            for key, (name, _, _) in growth_native.generator_artifacts(generation).items():
                (destination / name).write_bytes(artifact_bytes(artifacts[key]))
            check = self.payload["evidence"]["replay"]["checks"][0]
            subprocess.run(check["argv"], cwd=destination, check=True, capture_output=True, timeout=10)
            return {"contractID": identifier, "subjectHash": "b" * 64, "directory": "/state/growth-export"}

    api = Protocol()
    monkeypatch.setattr(campaign_install, "prepare_install", lambda *_: object())
    monkeypatch.setattr(campaign_workspace, "WorkspaceLease", Lease)
    monkeypatch.setattr(campaign_workspace, "ensure_workspace", lambda request, _: Workspace(request))
    monkeypatch.setattr(campaign_procontract, "HttpProContractApi", lambda *_, **__: api)
    monkeypatch.setattr(growth_native, "install_revision_source", lambda workspace, reference, **_: {
        "agent_revision_sha256": reference["sha256"], "source_archive_sha256": revision["source"]["archive"]["sha256"],
        "container": workspace.container_id, "destination": "/candidate/agent-source", "execution_user": "agent",
        "verified": {"uid": 1000, "passed": True, "tree_sha256": revision["source"]["tree_sha256"]},
        "host_checkout_mounted": False, "host_git_metadata_copied": False})
    backend = growth_native.ProgramBenchBackend(specification, root)
    result = backend.generate(output, generation, specification["rounds"][0])
    assert result["status"] == "completed"
    assert len(observed) == 1 and observed[0].reference_binding is False and observed[0].experiments is None
    assert backend.validate_generation(output, generation, result) == result
    assert backend.validate_generation(output, generation, result) == result
    assert len(observed) == 1 and len(api.attestations) == 1
    receipt = read(output / "generator-runtime.json")
    assert receipt["requested_runtime_options"] == {"experiments": experiments() if enabled else None}
    assert receipt["effective_runtime_options"] == {"experiments": None}

    substitutions = [
        ("requested", receipt | {"requested_runtime_options": {"experiments": None if enabled else experiments()}}),
        ("effective", receipt | {"effective_runtime_options": {"experiments": experiments()}}),
        ("reason", receipt | {"runtime_options_reason": "reference_allowed"}),
        ("parent", receipt | {"parent_revision": parent | {"sha256": "0" * 64}}),
        ("legacy", {key: receipt[key] for key in ["parent_revision", "solver_policy", "generator_policy"]}),
        *[(f"missing-{key}", {name: value for name, value in receipt.items() if name != key})
          for key in ["requested_runtime_options", "effective_runtime_options", "runtime_options_reason"]],
    ]
    if enabled:
        substitutions.append(("float", receipt | {"requested_runtime_options": {
            "experiments": experiments() | {"timeout": float(experiments()["timeout"])}}}))
    target = output / "generator-runtime.json"
    before = target.read_bytes()
    target.chmod(0o600)
    for _, value in substitutions:
        target.write_bytes(encode(value))
        with pytest.raises(GrowthError, match="effective runtime options"):
            backend.validate_generation(output, generation, result)
        target.write_bytes(before)
    target.chmod(0o400)
    withheld = target.with_suffix(".withheld")
    target.rename(withheld)
    with pytest.raises(FileNotFoundError):
        backend.validate_generation(output, generation, result)
    withheld.rename(target)

    target = observed[0].state_root / "workspace.json"
    before = target.read_bytes()
    workspace = json.loads(before)
    for environment in [None, ["PATH", "OPENCODE_REFERENCE_BINDING"], [True]]:
        changed = workspace | {"environment_names": environment}
        if environment is None:
            changed.pop("environment_names")
        target.write_bytes(encode(changed))
        with pytest.raises(GrowthError, match="isolation boundary"):
            backend.validate_generation(output, generation, result)
    target.write_bytes(before)
    if enabled:
        target = output / "generator-config.yaml"
        before = target.read_bytes()
        config = yaml.safe_load(before)
        config["experiments"]["timeout"] = float(config["experiments"]["timeout"])
        target.chmod(0o600)
        target.write_text(yaml.safe_dump(config))
        with pytest.raises(GrowthError, match="frozen runtime coordinates"):
            backend.validate_generation(output, generation, result)
        target.write_bytes(before)
        target.chmod(0o400)
    assert backend.validate_generation(output, generation, result) == result
