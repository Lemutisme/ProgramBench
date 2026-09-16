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


@pytest.mark.parametrize('failure_type', [RuntimeError, TypeError, KeyError])
def test_parent_import_fault_is_infrastructure_and_retains_cause(tmp_path, monkeypatch, failure_type):
    from types import SimpleNamespace
    from scripts import agent_revision
    from scripts.growth_loop import GrowthInfrastructureError
    from scripts.growth_native import install_revision_source

    error = failure_type('trusted parent failed')
    def fail(_):
        raise error
    monkeypatch.setattr(agent_revision, 'validate_revision', fail)
    with pytest.raises(GrowthInfrastructureError) as caught:
        install_revision_source(SimpleNamespace(container_id='owned-container'), {'sha256': 'a' * 64}, output=tmp_path)
    assert caught.value.__cause__ is error
    assert read(tmp_path / 'failure.json')['phase'] == 'validate-parent'


def test_import_diagnostics_keep_both_streams_and_nonzero_exit(tmp_path):
    from scripts.growth_native import _source_import_process

    result, output = _source_import_process([sys.executable, '-c',
        "import sys;print('observed');print('source failure',file=sys.stderr);sys.exit(7)"], subprocess.DEVNULL, tmp_path)
    assert result['exit_code'] == 7 and not result['timed_out'] and not result['output_limited']
    assert output == (tmp_path / 'stdout.log').read_bytes() == b'observed\n'
    assert (tmp_path / 'stderr.log').read_bytes() == b'source failure\n'
    assert read(tmp_path / 'process.json') == result


@pytest.mark.parametrize('descriptor', [1, 2])
def test_import_diagnostics_bound_each_output_stream(tmp_path, descriptor):
    from scripts.growth_native import _source_import_process

    result, _ = _source_import_process([sys.executable, '-c', f'import os;os.write({descriptor},b"x"*1000000)'],
                                       subprocess.DEVNULL, tmp_path)
    assert result['output_limited'] and not result['timed_out']
    assert (tmp_path / ('stdout.log' if descriptor == 1 else 'stderr.log')).stat().st_size == 65536
    assert max((tmp_path / name).stat().st_size for name in ['stdout.log', 'stderr.log']) <= 65536


def test_import_watchdog_terminates_child_after_leader_exits(tmp_path):
    import time
    from scripts.growth_native import _source_import_process

    child = tmp_path / 'child-pid'
    script = 'import os,sys,time\nchild=os.fork()\nif child:\n open(sys.argv[1],"w").write(str(child))\n os._exit(0)\ntime.sleep(30)'
    result, _ = _source_import_process([sys.executable, '-c', script, str(child)], subprocess.DEVNULL, tmp_path, timeout=1)
    assert result['timed_out']
    pid = int(child.read_text())
    for _ in range(50):
        status = Path(f'/proc/{pid}/stat')
        if not status.exists() or status.read_text().split()[2] == 'Z':
            break
        time.sleep(0.01)
    else:
        pytest.fail('import watchdog left an executing descendant')


@pytest.mark.parametrize(('stop_fails', 'accounting_fails'), [(True, False), (False, True), (True, True)])
def test_generator_cleanup_fault_cannot_mask_import_infrastructure(tmp_path, monkeypatch, stop_fails, accounting_fails):
    from types import SimpleNamespace
    from scripts import growth_native
    from scripts.growth_loop import GrowthInfrastructureError

    calls = []
    def stop():
        calls.append('stop')
        if stop_fails:
            raise OSError('cannot confirm container stop')
    def account(_):
        calls.append('accounting')
        if accounting_fails:
            raise OSError('ledger unavailable')
        return {'gateway': {'present': True, 'open': 0, 'missing_usage': 0}}
    monkeypatch.setattr(growth_native, 'native_accounting', account)
    record(tmp_path / 'usage.json', {'known_cost': '0.03', 'complete': True})
    original = GrowthInfrastructureError('source import failed with retained evidence')
    with pytest.raises(GrowthInfrastructureError) as caught:
        growth_native._close_generator_workspace(SimpleNamespace(stop=stop), tmp_path, tmp_path, original=original)
    assert caught.value.__cause__ is original
    assert calls == ['stop', 'accounting']
    assert read(tmp_path / 'accounting.json')['known_cost'] == '0.03'
    assert read(tmp_path / 'accounting.json')['usage_incomplete'] is True
    failure = read(tmp_path / 'cleanup-failure.json')
    assert failure['original']['type'] == 'GrowthInfrastructureError'
    assert len(failure['errors']) == int(stop_fails) + int(accounting_fails)


def test_cleanup_evidence_publication_fault_remains_infrastructure(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from scripts import growth_native
    from scripts.growth_loop import GrowthInfrastructureError

    def fail(*_):
        raise OSError('evidence volume unavailable')
    monkeypatch.setattr(growth_native, 'record', fail)
    monkeypatch.setattr(growth_native, 'native_accounting', lambda _: {'gateway': {'present': True}})
    original = GrowthInfrastructureError('source import failed')
    with pytest.raises(GrowthInfrastructureError, match='publication also failed') as caught:
        growth_native._close_generator_workspace(SimpleNamespace(stop=lambda: None), tmp_path, tmp_path, original=original)
    assert caught.value.__cause__ is original


def test_stop_interruption_still_attempts_accounting_before_propagating(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from scripts import growth_native

    calls = []
    interruption = KeyboardInterrupt()
    def stop():
        calls.append('stop')
        raise interruption
    def account(_):
        calls.append('accounting')
        return {'gateway': {'present': True}}
    monkeypatch.setattr(growth_native, 'native_accounting', account)
    with pytest.raises(KeyboardInterrupt) as caught:
        growth_native._close_generator_workspace(SimpleNamespace(stop=stop), tmp_path, tmp_path)
    assert caught.value is interruption and calls == ['stop', 'accounting']
    assert (tmp_path / 'accounting.json').is_file()
    assert read(tmp_path / 'cleanup-failure.json')['errors'][0]['type'] == 'KeyboardInterrupt'


def test_workspace_lease_exit_fault_cannot_mask_import_infrastructure(tmp_path, monkeypatch):
    from scripts import campaign_workspace
    from scripts.growth_loop import GrowthInfrastructureError
    from scripts.growth_native import _generator_workspace_lease

    original = GrowthInfrastructureError('source import failed')
    cleanup = campaign_workspace.StartupGatewayCleanupError(original, OSError('cleanup not confirmed'), {'pid': 123})
    class Lease:
        def __init__(self, _):
            pass
        def __enter__(self):
            return self
        def __exit__(self, kind, error, traceback):
            assert error is original
            raise cleanup
    monkeypatch.setattr(campaign_workspace, 'WorkspaceLease', Lease)
    with pytest.raises(GrowthInfrastructureError, match='workspace boundary') as caught:
        with _generator_workspace_lease(object(), tmp_path):
            raise original
    assert caught.value.__cause__ is cleanup and cleanup.__context__ is original
    assert read(tmp_path / 'workspace-failure.json')['type'] == 'StartupGatewayCleanupError'
