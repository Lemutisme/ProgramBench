import copy
from dataclasses import replace
import json
from types import SimpleNamespace
import subprocess

import pytest
import yaml

from scripts.campaign_experiments import validate_experiments, validate_runtime_options
from scripts.campaign_offline import ensure_gateway, prepare_reference_binding
from scripts.campaign_runner import _config_manifest
from scripts.campaign_workspace import WorkspaceError, _validate_request
from scripts.run_opencode import ConfigError, _canonical_config, freeze_campaign_config, load_config


def experiments():
    return {"candidate": "executable", "interpreter": "/usr/bin/python3", "timeout": 10000,
            "maxDuration": 120000, "prepare": {"argv": ["/bin/sh", "./compile.sh"], "timeout": 30000}}


@pytest.mark.parametrize("change", [
    {"candidate": "../executable"}, {"candidate": "/candidate/executable"},
    {"interpreter": "/bin/sh"}, {"env": {"PATH": "/candidate"}},
    {"timeout": True}, {"timeout": 0}, {"timeout": -1}, {"timeout": 10.5},
    {"maxDuration": 600001}, {"timeout": 120001},
    {"prepare": {"argv": ["/bin/sh", "./other.sh"], "timeout": 30000}},
    {"prepare": {"argv": ["/bin/sh", "./compile.sh"], "timeout": 120001}},
    {"prepare": {"argv": ["/bin/sh", "./compile.sh"], "timeout": 30000, "env": {}}},
])
def test_experiment_configuration_rejects_unbound_execution_paths_and_unbounded_operations(change):
    with pytest.raises(ValueError):
        validate_experiments(experiments() | change)


@pytest.mark.parametrize("options", [None, {}, {"experiment_tools": True}, {"experiments": True},
                                    {"experiments": None, "env": {}}, {"experiments": None, "deadline": 1}])
def test_runtime_options_only_admit_explicit_experiments(options):
    with pytest.raises(ValueError):
        validate_runtime_options(options)


def test_valid_options_are_copied_and_null_explicitly_disables():
    value = experiments()
    accepted = validate_runtime_options({"experiments": value})
    value["prepare"]["argv"].append("unexpected")
    assert accepted == {"experiments": experiments()}
    assert validate_runtime_options({"experiments": None}) == {"experiments": None}


def test_config_freezes_all_parameters_and_rejects_drift(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    raw = {"model": "test/model", "variant": "max", "excluded": [],
           "limits": {"wall_time_seconds": 21600, "provider_turns": None, "procontract_actions": None,
                      "total_cost_soft": None, "total_cost_hard": None},
           "cache": {"warmup_turns": 2, "minimum_read_ratio": 0.8, "stop_after_consecutive_failures": 2},
           "arms": {name: {"repo": str(repo), "revision": "dev", "mode": name}
                    for name in ("procontract", "vanilla")}}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    baseline = load_config(path)
    assert baseline.experiments is None
    assert "experiments" not in _canonical_config(baseline)
    assert "experiments" not in _config_manifest(baseline)
    raw["experiments"] = experiments()
    path.write_text(yaml.safe_dump(raw))
    enabled = load_config(path)
    assert _canonical_config(enabled)["experiments"] == experiments()
    assert _config_manifest(enabled)["experiments"] == experiments()
    frozen = tmp_path / "campaign.json"
    freeze_campaign_config(enabled, frozen)
    before = frozen.read_bytes()
    with pytest.raises(ConfigError, match="conflicts"):
        freeze_campaign_config(replace(enabled, experiments=experiments() | {"timeout": 9000}), frozen)
    assert frozen.read_bytes() == before
    raw["limits"].update(provider_turns=1000, procontract_actions=1000)
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="deadline-only"):
        load_config(path)


def test_reference_binding_freezes_configuration_and_rejects_unbound_changes(tmp_path, monkeypatch):
    calls = []
    def image_hash(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "a" * 64 + "  /workspace/executable\n", "")
    monkeypatch.setattr(subprocess, "run", image_hash)
    manifest = SimpleNamespace(campaign="probe", job_id="job", image_digest="sha256:fixed", state_root=tmp_path)
    config = experiments()
    binding = json.loads(prepare_reference_binding(manifest, experiments=config))
    assert binding["experiments"] == config
    frozen = (tmp_path / "reference-binding.json").read_bytes()
    assert json.loads(prepare_reference_binding(manifest, experiments=copy.deepcopy(config))) == binding
    with pytest.raises(ValueError, match="binding changed"):
        prepare_reference_binding(manifest)
    with pytest.raises(ValueError, match="binding changed"):
        prepare_reference_binding(manifest, experiments=config | {"maxDuration": 130000})
    assert (tmp_path / "reference-binding.json").read_bytes() == frozen
    assert all(call[call.index("--network") + 1] == "none" for call in calls)


def test_gateway_profile_freezes_configuration_before_any_provider_process(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    request = SimpleNamespace(state_root=tmp_path, provider_model="test/model", provider_effort="max",
                              job=SimpleNamespace(root=tmp_path / "job"), arm="procontract", deadline_only=True,
                              experiments=experiments())
    with pytest.raises(ValueError, match="provider credential missing"):
        ensure_gateway(request)
    path = tmp_path / "provider-control/profile.json"
    frozen = path.read_bytes()
    assert json.loads(frozen)["experiments"] == experiments()
    assert json.loads(frozen)["budget"] == "6h-only"
    assert not (tmp_path / "provider-control/gateway.json").exists()
    request.experiments = experiments() | {"maxDuration": 130000}
    with pytest.raises(ValueError, match="profile changed"):
        ensure_gateway(request)
    request.experiments = None
    with pytest.raises(ValueError, match="profile changed"):
        ensure_gateway(request)
    assert path.read_bytes() == frozen


def test_ambient_flags_cannot_enable_experiments(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("PROGRAMBENCH_EXPERIMENT_TOOLS", "1")
    request = SimpleNamespace(state_root=tmp_path, provider_model="test/model", provider_effort="max",
                              job=SimpleNamespace(root=tmp_path / "job"), arm="procontract", deadline_only=True)
    with pytest.raises(ValueError, match="provider credential missing"):
        ensure_gateway(request)
    assert "experiments" not in json.loads((tmp_path / "provider-control/profile.json").read_text())


@pytest.mark.parametrize("values", [
    {"arm": "vanilla", "deadline_only": True, "isolated": True},
    {"arm": "procontract", "deadline_only": False, "isolated": True},
    {"arm": "procontract", "deadline_only": True, "isolated": False},
])
def test_workspace_rejects_experiments_outside_its_isolated_deadline_profile(values):
    with pytest.raises(WorkspaceError, match="isolated deadline-only"):
        _validate_request(SimpleNamespace(experiments=experiments(), reference_binding=True, **values))


def test_workspace_cannot_request_experiments_without_a_reference_role():
    with pytest.raises(WorkspaceError, match="workspace role"):
        _validate_request(SimpleNamespace(experiments=experiments(), reference_binding=False,
                                         isolated=True, arm="procontract", deadline_only=True))
