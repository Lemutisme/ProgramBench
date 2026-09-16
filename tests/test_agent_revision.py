import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from scripts.agent_revision import (
    GrowthError, _git, _links, _path, artifact_bytes, build_revision, changed_components,
    export_source, freeze_initial, runtime_config, validate_recipe, validate_revision,
)
from scripts.growth_loop import encode, seal, sha256
from tests.test_campaign_experiments import experiments


def artifact(root, name, data):
    path = root / name
    seal(path, data)
    return {"path": str(path), "sha256": sha256(data)}


def git(repo, *args):
    return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=repo).decode().strip()


@pytest.mark.parametrize("maintenance", ["gc", "loose-objects"])
def test_trusted_git_does_not_start_maintenance_before_source_copy(tmp_path, maintenance):
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    # Keep the unprotected control synchronous so this regression needs no sleeps.
    for key, value in {"gc.auto": "1", "gc.autoDetach": "false", "maintenance.auto": "true",
                       "maintenance.autoDetach": "false", "maintenance.gc.enabled": str(maintenance == "gc").lower(),
                       "maintenance.loose-objects.enabled": str(maintenance == "loose-objects").lower(),
                       "maintenance.loose-objects.auto": "1"}.items():
        git(repo, "config", "--local", key, value)
    for index in range(1024):
        (repo / f"item-{index}.txt").write_text(f"Source-copy maintenance regression {index}\n")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "Frozen source")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    assert not list((repo / ".git/objects/pack").iterdir())
    assert not (repo / ".git/gc.pid").exists()
    copied = tmp_path / "copied"
    subprocess.run(["cp", "-a", str(repo), str(copied)], check=True, capture_output=True, timeout=15)
    assert _git(copied, "rev-parse", "HEAD^{tree}") == tree
    _git(copied, "fsck", "--full")
    # The same repository actually packs objects when command-level protection is absent.
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit",
        "--quiet", "--allow-empty", "-m", "Unprotected maintenance control")
    assert list((repo / ".git/objects/pack").glob("*.pack"))
    assert _git(repo, "rev-parse", "HEAD^{tree}") == tree


@pytest.fixture(scope="module")
def initial(tmp_path_factory):
    if shutil.which("docker") is None:
        pytest.skip("actual offline Docker qualification requires Docker")
    image = subprocess.run(["docker", "image", "inspect", os.environ.get("AGENT_REVISION_TEST_IMAGE",
                            "procontract-policy-generator:neutral-git-20260915"), "--format", "{{.Id}}"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, text=True)
    if image.returncode:
        pytest.skip("set AGENT_REVISION_TEST_IMAGE to a local Python-capable offline image")
    root = tmp_path_factory.mktemp("agent-revision")
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet")
    (repo / "agent.sh").write_text("#!/bin/sh\nprintf 'agent-v1\\n'\n")
    (repo / "agent.sh").chmod(0o755)
    (repo / ".gitignore").write_text("ignored.txt\n")
    (repo / "ignored.txt").write_text("Tracked source must survive ignore rules\n")
    (repo / "linked-agent").symlink_to("agent.sh")
    (repo / "packages/core/src/contract").mkdir(parents=True)
    (repo / "packages/core/src/contract/authority.ts").write_text("trusted authority\n")
    git(repo, "add", "--force", ".")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "--quiet", "-m", "fixture")
    check = artifact(root, "check.py", b"""import os, subprocess
import json
assert not any(name.endswith(('_API_KEY', '_TOKEN')) for name in os.environ)
with open('/trusted/runtime-options.json') as stream:
    options = json.load(stream)
assert set(options) == {'experiments'}
print(json.dumps({'observed_runtime_options': options}, sort_keys=True), flush=True)
try:
    with open('/trusted/runtime-options.json', 'w') as stream:
        stream.write('untrusted')
except OSError:
    pass
else:
    raise AssertionError('qualification options input must be read-only')
result = subprocess.run(['/artifact/opencode'], capture_output=True, check=True)
assert result.stdout.startswith(b'agent-v'), result.stdout
assert os.path.exists('/source/agent.sh')
assert not os.path.exists('/var/run/docker.sock')
os.mkdir('/work/qualification-scratch')
with open('/work/qualification-scratch/evidence', 'w') as stream:
    stream.write('candidate check owns writable scratch')
""")
    recipe = {"version": 1, "image": image.stdout.strip(),
              "build": {"argv": ["/bin/sh", "-c", "mkdir /work/build && cp /source/agent.sh /output/opencode && chmod 755 /output/opencode"],
                        "timeout_seconds": 30},
              "qualifications": [{"name": "independent-smoke", "argv": ["python3", "/trusted/check"],
                                  "script": check, "timeout_seconds": 30}],
              "dependencies": {}, "output": "opencode", "memory_mb": 128, "tmpfs_mb": 32, "pids_limit": 32}
    policy = artifact(root, "policy.txt", b"Use allowed experiments to improve task quality.\n")
    ref = freeze_initial(root / "initial", {"repo": str(repo), "commit": git(repo, "rev-parse", "HEAD")},
                         policy, policy, recipe)
    return root, ref, recipe, policy


def proposal(tmp_path, parent, policy, patch=b"", rationale="Improve runtime behavior"):
    return {"revision_proposal": artifact(tmp_path, "agent-revision.json", encode({
                "parent_sha256": parent["sha256"], "rationale": rationale, "metadata": {
                    "expected_effect": "Improve candidate output", "failure_modes": ["Output may regress"],
                    "permission_changes": [], "state_migration": None, "recovery": "Use previous frozen revision"}})),
            "source_patch": artifact(tmp_path, "source.patch", patch),
            "solver_policy": policy, "generator_policy": policy}


def test_actual_offline_revision_build_and_independent_qualification(initial, tmp_path):
    _, parent, recipe, policy = initial
    patch = b"diff --git a/agent.sh b/agent.sh\n--- a/agent.sh\n+++ b/agent.sh\n@@ -1,2 +1,2 @@\n #!/bin/sh\n-printf 'agent-v1\\n'\n+printf 'agent-v2\\n'\n"
    artifacts = proposal(tmp_path, parent, policy, patch)
    ref = build_revision(tmp_path / "built", parent, artifacts, recipe)
    candidate = validate_revision(ref, recipe)
    assert candidate["parent"] == parent
    assert candidate["status"] == "qualified"
    assert candidate["changed_paths"] == ["agent.sh"]
    assert candidate["binary"]["sha256"] != validate_revision(parent)["binary"]["sha256"]
    assert candidate["source"]["base"] == validate_revision(parent)["source"]["base"]
    assert candidate["adoption_authority"] is False
    assert build_revision(tmp_path / "built", parent, artifacts, recipe) == ref
    check = json.loads(artifact_bytes(candidate["qualification_receipts"][0]))
    assert check["binary_sha256"] == candidate["binary"]["sha256"]
    assert check["isolation"] == {"network": "none", "credentials": "none", "host_execution": False}
    export_source(ref, tmp_path / "exported")
    assert (tmp_path / "exported/linked-agent").readlink() == Path("agent.sh")
    assert b"agent-v2" in (tmp_path / "exported/agent.sh").read_bytes()
    assert not (tmp_path / "exported/.git").exists()
    assert (tmp_path / "exported/ignored.txt").read_text() == "Tracked source must survive ignore rules\n"
    original = {"arms": {"procontract": {"revision": "old"}}, "limits": {"wall_time_seconds": 21600}}
    configured = runtime_config(original, ref, recipe)
    assert configured["arms"]["procontract"]["binary_probe_image"] == recipe["image"]
    assert configured["arms"]["procontract"]["revision"] == candidate["source"]["commit"]
    assert configured["limits"] == original["limits"]
    assert original["arms"]["procontract"]["revision"] == "old"


def test_runtime_options_bind_inheritance_disable_and_qualification_input(initial, tmp_path):
    _, parent, recipe, policy = initial
    root = validate_revision(parent, recipe)
    assert root["runtime_options"] == {"experiments": None}
    records = []
    for index, options in enumerate([{"experiments": experiments()}, "inherit", {"experiments": None}]):
        directory = tmp_path / str(index)
        directory.mkdir()
        artifacts = proposal(directory, parent, policy)
        if options != "inherit":
            document = json.loads(artifact_bytes(artifacts["revision_proposal"])) | {"runtime_options": options}
            artifacts["revision_proposal"] = artifact(directory, "selected.json", encode(document))
        reference = build_revision(directory / "revision", parent, artifacts, recipe)
        selected = validate_revision(reference, recipe)
        expected = {"experiments": experiments()} if index < 2 else {"experiments": None}
        assert selected["runtime_options"] == expected
        assert artifact_bytes(selected["runtime_options_file"]) == encode(expected)
        receipt = json.loads(artifact_bytes(selected["qualification_receipts"][0]))
        assert receipt["runtime_options_sha256"] == selected["runtime_options_file"]["sha256"]
        assert json.loads(artifact_bytes(receipt["log"])) == {"observed_runtime_options": expected}
        configured = runtime_config({"arms": {"procontract": {}}}, reference, recipe)
        assert configured.get("experiments") == expected["experiments"]
        records.append((reference, selected))
        parent = reference
    enabled, disabled = records[0][0], records[2][0]
    with pytest.raises(GrowthError, match="conflicts"):
        runtime_config({"arms": {"procontract": {}}, "experiments": None}, enabled, recipe)
    with pytest.raises(GrowthError, match="conflicts"):
        runtime_config({"arms": {"procontract": {}}, "experiments": experiments()}, disabled, recipe)

    from scripts.growth_native import runtime_configuration
    evaluation = tmp_path / "evaluation"
    evaluation.mkdir()
    configured = runtime_config({"arms": {"procontract": {}}}, enabled, recipe)
    frozen = yaml.safe_dump(configured)
    (evaluation / "config.yaml").write_text(frozen)
    study = {"initial": {"agent_revision": enabled}, "revision_recipe": recipe}
    binding = {"agent_revision": disabled, "solver_policy": records[2][1]["solver_policy"],
               "generator_policy": records[2][1]["generator_policy"]}
    assert runtime_configuration(study, evaluation, binding).get("experiments") is None
    assert (evaluation / "config.yaml").read_text() == frozen
    configured["experiments"]["timeout"] = 9999
    (evaluation / "config.yaml").write_text(yaml.safe_dump(configured))
    with pytest.raises(GrowthError, match="initial configuration"):
        runtime_configuration(study, evaluation, binding)
    configured["experiments"]["timeout"] = float(experiments()["timeout"])
    (evaluation / "config.yaml").write_text(yaml.safe_dump(configured))
    with pytest.raises(GrowthError, match="invalid initial configuration"):
        runtime_configuration(study, evaluation, binding)

    changed = dict(records[1][1])
    changed["runtime_options"] = {"experiments": None}
    changed["runtime_options_file"] = artifact(tmp_path, "wrong-options.json", encode(changed["runtime_options"]))
    with pytest.raises(GrowthError, match="inherited parent"):
        validate_revision(artifact(tmp_path, "wrong-inheritance.json", encode(changed)), recipe)

    changed = dict(records[0][1])
    receipt = json.loads(artifact_bytes(changed["qualification_receipts"][0]))
    receipt["runtime_options_sha256"] = "0" * 64
    changed["qualification_receipts"] = [artifact(tmp_path, "wrong-receipt.json", encode(receipt))]
    with pytest.raises(GrowthError, match="qualify these exact bytes"):
        validate_revision(artifact(tmp_path, "wrong-qualification.json", encode(changed)), recipe)
    changed = dict(records[0][1])
    changed["runtime_options_file"] = None
    with pytest.raises(GrowthError, match="artifact requires"):
        validate_revision(artifact(tmp_path, "null-options-file.json", encode(changed)), recipe)


def test_legacy_manifest_without_options_remains_disabled(initial, tmp_path):
    _, parent, recipe, _ = initial
    legacy = dict(validate_revision(parent, recipe))
    legacy.pop("runtime_options")
    legacy.pop("runtime_options_file")
    receipts = []
    for index, ref in enumerate(legacy["qualification_receipts"]):
        receipt = json.loads(artifact_bytes(ref))
        receipt.pop("runtime_options_sha256")
        receipts.append(artifact(tmp_path, f"legacy-check-{index}.json", encode(receipt)))
    legacy["qualification_receipts"] = receipts
    reference = artifact(tmp_path, "legacy-manifest.json", encode(legacy))
    assert "runtime_options" not in validate_revision(reference, recipe)
    assert "experiments" not in runtime_config({"arms": {"procontract": {}}}, reference, recipe)
    with pytest.raises(GrowthError, match="conflicts"):
        runtime_config({"arms": {"procontract": {}}, "experiments": experiments()}, reference, recipe)


def test_initial_runtime_options_are_explicitly_frozen_into_qualification(initial, tmp_path):
    _, parent, recipe, policy = initial
    selected = freeze_initial(tmp_path / "enabled", validate_revision(parent)["source"]["base"], policy, policy,
                              recipe, runtime_options={"experiments": experiments()})
    manifest = validate_revision(selected, recipe)
    assert manifest["parent"] is None
    assert manifest["runtime_options"] == {"experiments": experiments()}
    receipt = json.loads(artifact_bytes(manifest["qualification_receipts"][0]))
    assert receipt["runtime_options_sha256"] == manifest["runtime_options_file"]["sha256"]


def test_sensitive_change_is_archived_without_build_or_execution(initial, tmp_path):
    _, parent, recipe, policy = initial
    patch = (b"diff --git a/packages/core/src/contract/authority.ts b/packages/core/src/contract/authority.ts\n"
             b"--- a/packages/core/src/contract/authority.ts\n+++ b/packages/core/src/contract/authority.ts\n"
             b"@@ -1 +1 @@\n-trusted authority\n+candidate authority\n")
    ref = build_revision(tmp_path / "blocked", parent, proposal(tmp_path, parent, policy, patch), recipe)
    manifest = validate_revision(ref, recipe, executable=False)
    assert manifest["status"] == "proposal-only"
    assert manifest["binary"] is None
    assert not (tmp_path / "blocked/build.log").exists()
    with pytest.raises(GrowthError, match="proposal-only"):
        runtime_config({"arms": {"procontract": {}}}, ref)


def test_candidate_cannot_report_qualification_or_replace_parent(initial, tmp_path):
    _, parent, recipe, policy = initial
    artifacts = proposal(tmp_path, parent, policy)
    artifacts["revision_proposal"] = artifact(tmp_path, "forged.json", encode({
        "parent_sha256": parent["sha256"], "rationale": "self approval", "passed": True}))
    with pytest.raises(GrowthError, match="only name exact parent"):
        build_revision(tmp_path / "bad", parent, artifacts, recipe)
    artifacts["revision_proposal"] = artifact(tmp_path, "wrong-parent.json", encode({
        "parent_sha256": "0" * 64, "rationale": "other parent"}))
    with pytest.raises(GrowthError, match="only name exact parent"):
        build_revision(tmp_path / "other", parent, artifacts, recipe)


def test_failed_build_retains_receipt_and_is_not_retried(initial, tmp_path):
    _, _, recipe, policy = initial
    recipe = {**recipe, "build": {"argv": ["/bin/sh", "-c", "echo observed-build-failure; exit 7"], "timeout_seconds": 30}}
    parent = validate_revision(initial[1])
    with pytest.raises(GrowthError, match="isolated build failed"):
        freeze_initial(tmp_path / "failed", parent["source"]["base"], policy, policy, recipe)
    receipt = json.loads((tmp_path / "failed/build-receipt.json").read_bytes())
    assert receipt["exit_code"] == 7
    assert b"observed-build-failure" in artifact_bytes(receipt["log"])
    with pytest.raises(GrowthError, match="explicit recovery"):
        freeze_initial(tmp_path / "failed", parent["source"]["base"], policy, policy, recipe)


def test_actual_timeout_stops_container_and_preserves_original_operation_limit(initial, tmp_path):
    _, parent, recipe, policy = initial
    recipe = {**recipe, "build": {"argv": ["/bin/sh", "-c", "sleep 30"], "timeout_seconds": 0.2}}
    with pytest.raises(GrowthError, match="isolated build failed"):
        freeze_initial(tmp_path / "timeout", validate_revision(parent)["source"]["base"], policy, policy, recipe)
    receipt = json.loads((tmp_path / "timeout/build-receipt.json").read_bytes())
    assert receipt["timed_out"] is True
    assert receipt["cancelled"] is False
    assert receipt["ended_at"] - receipt["started_at"] < 20


def test_cancel_before_admission_creates_no_container(initial, tmp_path):
    _, parent, recipe, policy = initial
    with pytest.raises(GrowthError, match="cancelled before reconstruction"):
        freeze_initial(tmp_path / "cancelled", validate_revision(parent)["source"]["base"], policy, policy,
                       recipe, cancelled=lambda: True)
    assert not (tmp_path / "cancelled/started.json").exists()


def test_failed_independent_qualification_cannot_publish_manifest(initial, tmp_path):
    _, parent, recipe, policy = initial
    failed = artifact(tmp_path, "check.py", b"raise SystemExit(19)\n")
    recipe = {**recipe, "qualifications": [{**recipe["qualifications"][0], "script": failed}]}
    with pytest.raises(GrowthError, match="isolated check-independent-smoke failed"):
        freeze_initial(tmp_path / "rejected", validate_revision(parent)["source"]["base"], policy, policy, recipe)
    assert not (tmp_path / "rejected/manifest.json").exists()
    assert json.loads((tmp_path / "rejected/check-independent-smoke-receipt.json").read_bytes())["exit_code"] == 19


def test_cache_rechecks_binary_source_recipe_and_receipts(initial):
    _, ref, recipe, _ = initial
    manifest = validate_revision(ref)
    targets = [Path(manifest["binary"]["path"]), Path(manifest["source"]["repo"]) / "agent.sh",
               Path(manifest["qualification_receipts"][0]["path"]), Path(recipe["qualifications"][0]["script"]["path"])]
    for path in targets:
        before = path.read_bytes()
        mode = path.stat().st_mode & 0o777
        path.chmod(0o700)
        path.write_bytes(before + b"tampered\n")
        try:
            with pytest.raises(GrowthError):
                validate_revision(ref, recipe)
        finally:
            path.write_bytes(before)
            path.chmod(mode)
    assert validate_revision(ref)["status"] == "qualified"


def test_candidate_symlink_patch_cannot_escape_host(initial, tmp_path):
    _, parent, recipe, policy = initial
    patch = (b"diff --git a/escape b/escape\nnew file mode 120000\nindex 0000000..c137216\n"
             b"--- /dev/null\n+++ b/escape\n@@ -0,0 +1 @@\n+/tmp\n\\ No newline at end of file\n")
    with pytest.raises(GrowthError, match="symlink|symbolic"):
        build_revision(tmp_path / "escape", parent, proposal(tmp_path, parent, policy, patch), recipe)
    assert not (tmp_path / "escape/build.log").exists()


@pytest.mark.parametrize("name", ["../escape", "/root/file", "a/../../escape", ".git/hooks/pre-commit", "a//b", "a/./b"])
def test_source_paths_reject_escape_and_git_metadata(name):
    with pytest.raises(GrowthError):
        _path(name)


@pytest.mark.parametrize("target", ["../../outside", "/etc/passwd", "link"])
def test_baseline_symlinks_cannot_escape_or_cycle(target):
    with pytest.raises(GrowthError):
        _links({"link": {"mode": "120000", "target": target}, "agent": {"mode": "100644"}})


def test_artifact_symlink_and_substituted_bytes_are_rejected(tmp_path):
    ref = artifact(tmp_path, "input", b"frozen")
    (tmp_path / "link").symlink_to("input")
    with pytest.raises(GrowthError):
        artifact_bytes({**ref, "path": str(tmp_path / "link")})
    with pytest.raises(GrowthError):
        artifact_bytes({**ref, "sha256": "0" * 64})


def test_classification_is_derived_from_paths_not_candidate_claims():
    assert "sensitive" in changed_components(["packages/core/src/session/session-store.ts"])
    assert "sensitive" in changed_components(["packages/core/src/pro-contract/reducer.ts"])
    assert "sensitive" in changed_components(["packages/schema/src/pro-contract.ts"])
    assert "sensitive" in changed_components(["packages/core/src/permission.ts"])
    assert "sensitive" in changed_components(["packages/core/src/permission/index.ts"])
    assert "tools" in changed_components(["packages/core/src/tool/experiment.ts"])


def test_recipe_cannot_use_unpinned_image_or_self_qualification(initial):
    recipe = initial[2]
    with pytest.raises(GrowthError, match="digest pinned"):
        validate_recipe({**recipe, "image": "untrusted:latest"})
    with pytest.raises(GrowthError, match="trusted check"):
        validate_recipe({**recipe, "qualifications": [{**recipe["qualifications"][0], "argv": ["/source/tests.sh"]}]})


def test_native_generator_imports_exact_source_without_host_git_metadata(initial, tmp_path):
    from types import SimpleNamespace
    from scripts.growth_native import install_revision_source

    _, reference, recipe, _ = initial
    revision = validate_revision(reference, recipe)
    files = json.loads(artifact_bytes(revision["source"]["files"]))
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    candidate.chmod(0o777)
    container = subprocess.check_output(["docker", "run", "-d", "--network=none", "--cap-drop=ALL",
                                         "--security-opt=no-new-privileges", "--user", "agent",
                                         "--mount", f"type=bind,src={candidate},dst=/candidate",
                                         "--entrypoint", "/bin/sleep", recipe["image"], "300"], text=True, timeout=30).strip()
    try:
        receipt = install_revision_source(SimpleNamespace(container_id=container), reference, output=tmp_path / "import")
        assert receipt["verified"]["tree_sha256"] == revision["source"]["tree_sha256"]
        assert receipt["host_git_metadata_copied"] is False and receipt["host_checkout_mounted"] is False
        assert receipt["execution_user"] == "agent" and receipt["verified"]["uid"] > 0
        ownership = subprocess.check_output(["docker", "exec", container, "python3", "-I", "-S", "-c",
            "import os,pathlib; assert all(p.lstat().st_uid==os.getuid() for p in pathlib.Path('/candidate/agent-source').rglob('*')); "
            "assert 'CapEff:\\t0000000000000000' in pathlib.Path('/proc/self/status').read_text()"], timeout=15)
        assert ownership == b""
        tracked = subprocess.check_output(["docker", "exec", "--user", "agent", container,
                                            "git", "-C", "/candidate/agent-source", "ls-files", "-z"], timeout=15)
        assert set(tracked.decode().strip("\0").split("\0")) == set(files)
        git_config = subprocess.check_output(["docker", "exec", container, "cat", "/candidate/agent-source/.git/config"],
                                             text=True, timeout=15)
        assert "remote" not in git_config and str(initial[0]) not in git_config
        from scripts.growth_loop import GrowthInfrastructureError

        with pytest.raises(GrowthInfrastructureError, match="source import failed"):
            install_revision_source(SimpleNamespace(container_id=container), reference, output=tmp_path / "repeat-import")
        failure = json.loads((tmp_path / "repeat-import/process.json").read_text())
        assert failure["exit_code"] != 0 and not failure["timed_out"]
        assert b"FileExistsError" in (tmp_path / "repeat-import/stderr.log").read_bytes()
        assert (tmp_path / "repeat-import/failure.json").is_file()
    finally:
        try:
            cleaned = subprocess.run(["docker", "exec", "--user", "agent", container, "python3", "-I", "-S", "-c",
                "import pathlib,shutil; path=pathlib.Path('/candidate/agent-source'); "
                "shutil.rmtree(path) if path.exists() else None"], capture_output=True, timeout=30)
        finally:
            subprocess.run(["docker", "rm", "--force", container], capture_output=True, check=True, timeout=30)
        assert cleaned.returncode == 0, cleaned.stderr.decode(errors="replace")


def test_clean_alternate_head_does_not_requalify_old_source(initial):
    manifest = validate_revision(initial[1])
    repo = Path(manifest["source"]["repo"])
    commit = manifest["source"]["commit"]
    (repo / "extra.txt").write_text("clean but different source\n")
    git(repo, "add", "extra.txt")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "--quiet", "-m", "other")
    try:
        with pytest.raises(GrowthError, match="source"):
            validate_revision(initial[1])
    finally:
        git(repo, "checkout", "--quiet", "--detach", commit)
    assert validate_revision(initial[1])["status"] == "qualified"


def test_declared_permissions_or_migration_remain_proposal_only(initial, tmp_path):
    _, parent, recipe, policy = initial
    for field, value in (("permission_changes", ["Request broader filesystem access"]),
                         ("state_migration", "Rewrite previous session state")):
        directory = tmp_path / field
        directory.mkdir()
        artifacts = proposal(directory, parent, policy)
        document = json.loads(artifact_bytes(artifacts["revision_proposal"]))
        document["metadata"][field] = value
        artifacts["revision_proposal"] = artifact(directory, "declared.json", encode(document))
        ref = build_revision(directory / "revision", parent, artifacts, recipe)
        assert validate_revision(ref, executable=False)["status"] == "proposal-only"
        assert not (directory / "revision/build.log").exists()


def test_single_operation_log_guard_retains_bounded_failure_evidence(initial, tmp_path):
    _, parent, recipe, policy = initial
    recipe = {**recipe, "build": {"argv": ["python3", "-c", "print('x' * (17 * 1024 * 1024))"],
                                   "timeout_seconds": 30}}
    with pytest.raises(GrowthError, match="isolated build failed"):
        freeze_initial(tmp_path / "output-limit", validate_revision(parent)["source"]["base"], policy, policy, recipe)
    receipt = json.loads((tmp_path / "output-limit/build-receipt.json").read_bytes())
    assert receipt["output_limited"] is True
    assert receipt["cleanup_confirmed"] is True
    assert len(artifact_bytes(receipt["log"])) == 16 * 1024 * 1024


def test_child_revalidates_parent_manifest_and_source(initial, tmp_path):
    _, parent, recipe, policy = initial
    ref = build_revision(tmp_path / "child", parent, proposal(tmp_path, parent, policy), recipe)
    path = Path(parent["path"])
    before = path.read_bytes()
    path.chmod(0o600)
    path.write_bytes(before + b" ")
    try:
        with pytest.raises(GrowthError, match="artifact bytes changed"):
            validate_revision(ref)
    finally:
        path.write_bytes(before)
        path.chmod(0o400)
    assert validate_revision(ref)["parent"] == parent


def test_failed_docker_admission_also_has_receipt_and_confirmed_cleanup(initial, tmp_path):
    _, parent, recipe, policy = initial
    recipe = {**recipe, "image": "sha256:" + "0" * 64}
    with pytest.raises(GrowthError, match="isolated build failed"):
        freeze_initial(tmp_path / "admission", validate_revision(parent)["source"]["base"], policy, policy, recipe)
    receipt = json.loads((tmp_path / "admission/build-receipt.json").read_bytes())
    assert receipt["exit_code"] != 0
    assert "admission failed" in receipt["phase_error"]
    assert receipt["cleanup_confirmed"] is True


def test_active_build_cancellation_reaps_container_and_retains_evidence(initial, tmp_path):
    _, parent, recipe, policy = initial
    recipe = {**recipe, "build": {"argv": ["python3", "-c", "import time; print('active', flush=True); time.sleep(30)"],
                                   "timeout_seconds": 30}}
    output = tmp_path / "active-cancellation"

    def cancelled():
        return (output / "build.log").exists() and b"active" in (output / "build.log").read_bytes()

    with pytest.raises(GrowthError, match="isolated build failed"):
        freeze_initial(output, validate_revision(parent)["source"]["base"], policy, policy, recipe, cancelled)
    receipt = json.loads((output / "build-receipt.json").read_bytes())
    assert receipt["cancelled"] is True
    assert receipt["timed_out"] is False
    assert receipt["cleanup_confirmed"] is True


def test_candidate_cannot_export_symlink_as_executable(initial, tmp_path):
    _, parent, recipe, policy = initial
    recipe = {**recipe, "build": {"argv": ["/bin/sh", "-c", "ln -s /source/agent.sh /output/opencode"],
                                   "timeout_seconds": 30}}
    with pytest.raises(GrowthError, match="isolated build failed"):
        freeze_initial(tmp_path / "symlink-output", validate_revision(parent)["source"]["base"], policy, policy, recipe)
    receipt = json.loads((tmp_path / "symlink-output/build-receipt.json").read_bytes())
    assert "regular executable" in receipt["export_error"]
    assert receipt["cleanup_confirmed"] is True
    assert not (tmp_path / "symlink-output/opencode").exists()


def test_git_index_hints_cannot_hide_mounted_source_byte_changes(initial):
    manifest = validate_revision(initial[1])
    repo = Path(manifest["source"]["repo"])
    path = repo / "agent.sh"
    before = path.read_bytes()
    git(repo, "update-index", "--assume-unchanged", "agent.sh")
    path.write_bytes(before.replace(b"\n", b"\r\n"))
    try:
        assert git(repo, "status", "--porcelain") == ""
        with pytest.raises(GrowthError, match="mounted source"):
            validate_revision(initial[1])
    finally:
        path.write_bytes(before)
        git(repo, "update-index", "--no-assume-unchanged", "agent.sh")
    assert validate_revision(initial[1])["status"] == "qualified"
