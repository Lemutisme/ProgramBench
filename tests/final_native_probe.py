"""Subprocess fixture for test_growth_final_native, never an experiment entrypoint."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import yaml

from scripts import growth_native
from scripts.agent_revision import runtime_config, validate_revision
from scripts.campaign_runner import _is_real_instance
from scripts.growth_final_native import FinalNativeInputs, MODEL, require_bound_backend, source_inventory
from scripts.growth_final_registry import ALGORITHM, EXECUTION, SCORE_ENCODING, paired_schedule
from scripts.growth_loop import GrowthError, encode, record, sha256
from programbench.constants import TASKS_DIR, image_name_from_instance_id
from programbench.candidate import DEFAULT_IMAGE_TAG
from programbench.utils.load_data import load_all_instances


def artifact(root, name, value, raw=False):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if raw else encode(value))
    return {"path": str(path), "sha256": sha256(path.read_bytes())}


def probe(fixture, root, scenario):
    root.mkdir(exist_ok=True)
    repository = fixture["repository"]
    revisions = fixture["revisions"]
    baseline = validate_revision(revisions["baseline"], fixture["recipe"])
    config = runtime_config({"model": "openai/gpt-5.6-luna", "variant": "max", "limits": {
        "wall_time_seconds": 21600, "provider_turns": None, "procontract_actions": None,
        "total_cost_soft": None, "total_cost_hard": None}, "cache": {"warmup_turns": 0,
        "minimum_read_ratio": 0, "stop_after_consecutive_failures": 3}, "excluded": [],
        "arms": {name: {"repo": baseline["source"]["repo"], "revision": baseline["source"]["commit"], "mode": name}
                 for name in ("procontract", "vanilla")}}, revisions["baseline"], fixture["recipe"])
    if scenario == "model": config["model"] = "openai/gpt-5.6-terra"
    if scenario == "provider": config["model"] = "other/gpt-5.6-luna"
    if scenario == "effort": config["variant"] = "high"
    for key, field in (("turn_cap", "provider_turns"), ("action_cap", "procontract_actions"),
                       ("soft_cost", "total_cost_soft"), ("hard_cost", "total_cost_hard"), ("deadline", "wall_time_seconds")):
        if scenario == key: config["limits"][field] = 1000
    config_ref = artifact(root, "config.yaml", yaml.safe_dump(config).encode(), raw=True)
    catalog = [item for item in load_all_instances(include_tests=False) if _is_real_instance(item)]
    if scenario == "catalog": catalog[0]["language"] = "Different"
    if scenario == "ids_catalog":
        catalog = [{"instance_id": item["instance_id"]} for item in catalog]
    catalog_ref = artifact(root, "catalog.json", catalog)
    metadata = {item["instance_id"]: item for item in load_all_instances() if _is_real_instance(item)}
    metadata_ref = artifact(root, "metadata.json", metadata)
    if scenario in {"extra_real_task", "missing_real_task"}:
        if scenario == "missing_real_task":
            (TASKS_DIR / catalog[0]["instance_id"]).rename(root / "removed-task")
        if scenario == "extra_real_task":
            task = TASKS_DIR / "synthetic-extra"
            task.mkdir()
            (task / "task.yaml").write_text("difficulty: easy\nlanguage: Python\n")
            (task / "tests.json").write_text('{"branches": {}}\n')
        subprocess.run(["git", "-C", repository["path"], "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", repository["path"], "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@localhost", "commit", "--quiet", "-m", "alter real task inventory"],
                       check=True, capture_output=True)
        repository["commit"] = subprocess.check_output(["git", "-C", repository["path"], "rev-parse", "HEAD"], text=True).strip()
    wheel = artifact(root, "wheelhouse/synthetic.whl", b"never loaded or installed", raw=True)
    study = {"version": 2, "workers": 2 if scenario == "inner_workers" else 1,
             "runner": {**repository, "python": sys.executable}, "initial": {"agent_revision": revisions["baseline"]},
             "config": config_ref, "metadata": metadata_ref, "revision_recipe": fixture["recipe"],
             "runtime_sha256": baseline["binary"]["sha256"], "test_map": "official-full-map-v1",
             "infrastructure_guard": {"minimum_disk_free_bytes": 1},
             "evaluator": {"safe_reporting": True, "wheelhouse": {"path": str(root / "wheelhouse"),
                 "files": {"synthetic.whl": wheel["sha256"]}}}}
    if scenario == "initial_revision": study["initial"]["agent_revision"] = revisions["candidate"]
    if scenario == "recipe": study["revision_recipe"] = {**fixture["recipe"], "memory_mb": 129}
    if scenario == "runner": study["runner"]["commit"] = "b" * 40
    study_ref = artifact(root, "study.json", study)
    blob = Path(fixture["blob_root"])
    resources = {"catalog": catalog_ref, "metadata": metadata_ref, "blob_root": str(blob),
                 "task_files": [{"path": str(path), "sha256": sha256(path.read_bytes())}
                                for path in sorted(TASKS_DIR.rglob("*")) if path.is_file() and path.parent.name in metadata],
                 "blobs": [{"path": str(path), "sha256": sha256(path.read_bytes())}
                           for path in sorted(blob.rglob("*")) if path.is_file()],
                 "images": {entry["instance_id"]: {"name": f"{image_name_from_instance_id(entry['instance_id'])}:{DEFAULT_IMAGE_TAG}",
                            "sha256": "sha256:" + "a" * 64} for entry in catalog}}
    if scenario == "task_resource": resources["task_files"].pop()
    if scenario == "blob": resources["blobs"].pop()
    if scenario == "image": resources["images"][catalog[0]["instance_id"]]["sha256"] = "sha256:" + "b" * 64
    if scenario == "image_tag": resources["images"][catalog[0]["instance_id"]]["name"] = image_name_from_instance_id(catalog[0]["instance_id"])
    sources = source_inventory(repository)
    if scenario == "source_inventory": sources.pop()
    native = {"schema_version": 1, "root": str(root), "study": study_ref, "controller": repository,
              "sources": sources, "python": {"path": sys.executable, "resolved": {
                  "path": str(Path(sys.executable).resolve()), "sha256": sha256(Path(sys.executable).read_bytes())}},
              "resources": artifact(root, "resources.json", resources),
              "repeat_seeds": {"1": 1, "2": 2}}
    if scenario == "seed_type": native["repeat_seeds"]["1"] = "1"
    if scenario == "seed_boolean": native["repeat_seeds"]["1"] = True
    if scenario == "python": native["python"]["resolved"]["sha256"] = "b" * 64
    registration = {"schema_version": 1, "attempt_id": "synthetic-unregistered", "revisions": revisions,
                    "catalog": catalog_ref, "analyzer": {"path": str(Path(repository["path"]) / "scripts/growth_final_analysis.py"),
                        "sha256": sha256((Path(repository["path"]) / "scripts/growth_final_analysis.py").read_bytes())},
                    "configuration": artifact(root, "native-inputs.json", native), "model": MODEL,
                    "execution": EXECUTION, "pairing": {"seed": "c" * 64, "algorithm": ALGORITHM,
                        "solver_workers": 3 if scenario == "outer_workers" else 2, "active_blocks": 1},
                    "task_ids": sorted(metadata), "repeats": 2, "direction": "candidate-baseline",
                    "score_encoding": SCORE_ENCODING, "schedule": paired_schedule(sorted(metadata), "c" * 64)}
    if scenario == "root_config": (root / "config.yaml").write_text("changed: actual root config\n")
    if scenario == "metadata": (root / "metadata.json").write_text("{}\n")
    if scenario == "module_origin": growth_native.__file__ = str(root / "foreign.py")
    if scenario == "module_code": growth_native.ProgramBenchBackend.evaluate = lambda *args: None
    try:
        boundary = FinalNativeInputs(registration, expected_catalog_sha256=catalog_ref["sha256"])
        if scenario.startswith("bulk"):
            specifications = boundary.specifications()
            assert len(specifications) == 800
            for registered_slot, derived in zip(registration["schedule"], specifications, strict=True):
                assert derived == boundary.specification(registered_slot)
            if scenario == "bulk_drift":
                boundary.study["workers"] = 2
                boundary.specifications()
        slot = deepcopy(registration["schedule"][0])
        specification = deepcopy(boundary.specification(slot))
        if scenario == "bulk_admission":
            specification = deepcopy(specifications[-1])
        output = root / f"slots/{slot['slot']:06d}"
        if scenario == "backend":
            require_bound_backend(growth_native.ProgramBenchBackend(study, root), boundary.qualify, root, registration)
        if scenario == "callback": require_bound_backend(boundary.backend, lambda *args: None, root, registration)
        if scenario == "backend_study": boundary.backend.study["workers"] = 2
        if scenario == "policy": specification["solver_policy"] = {**baseline["solver_policy"], "sha256": "b" * 64}
        if scenario == "runtime": specification["runtime_sha256"] = "b" * 64
        if scenario == "spec_runner": specification["runner"] = {**study["runner"], "commit": "b" * 40}
        if scenario == "seed": specification["seed"] = "other-repeat"
        if scenario == "test_map": specification["test_map"] = "strict"
        if scenario == "spec_study": specification["study_sha256"] = "b" * 64
        if scenario == "slot_repeat": slot["repeat"] = 2
        if scenario.startswith("contract_"):
            campaign = output / ("growth-eval-" + sha256(str(output.resolve()).encode()))
            issue = {"startedAt": 1700000000.123, "payload": {"budget": {"deadline": 1700021600123},
                     "model": {"providerID": "openai", "id": "gpt-5.6-luna", "variant": "max"}}}
            path = campaign / "procontract" / slot["task_id"] / "procontract-issue.json"
            if scenario == "contract_cap": issue["payload"]["budget"]["turns"] = 1000
            if scenario == "contract_model": issue["payload"]["model"]["variant"] = "high"
            if scenario == "contract_deadline": issue["payload"]["budget"]["deadline"] += 1000
            record(path, issue)
            if scenario == "contract_reset":
                boundary.qualify(slot, specification, output)
                issue["startedAt"] += 1
                issue["payload"]["budget"]["deadline"] += 1000
                path.chmod(0o600)
                path.write_bytes(encode(issue))
        if scenario == "cached_config":
            boundary.qualify(slot, specification, output)
            (root / "config.yaml").write_text("changed: between validation calls\n")
        boundary.qualify(slot, specification, output)
        require_bound_backend(boundary.backend, boundary.qualify, root, registration)
    except GrowthError as error:
        if scenario in {"valid", "ids_catalog", "bulk"}: raise
        expected = {
            **{key: "actual native config" for key in ("model", "provider", "effort", "turn_cap", "action_cap",
                                                       "soft_cost", "hard_cost", "deadline")},
            **{key: "native specification differs" for key in ("policy", "runtime", "spec_runner", "seed", "test_map", "spec_study")},
            **{key: "deadline-only envelope" for key in ("contract_cap", "contract_model", "contract_deadline")},
            "inner_workers": "one inner worker", "outer_workers": "two workers per active pair",
            "initial_revision": "registered baseline", "recipe": "externally frozen recipe",
            "root_config": "frozen input changed", "metadata": "frozen input changed", "catalog": "catalog/metadata differ",
            "task_resource": "metadata resource inventory", "blob": "blob inventory", "image": "resource identity",
            "extra_real_task": "real-task selection differs", "missing_real_task": "real-task selection differs",
            "image_tag": "image name differs",
            "seed_type": "integer native repeat seeds", "seed_boolean": "integer native repeat seeds",
            "python": "frozen input changed", "runner": "frozen commit", "source_inventory": "native sources omit",
            "module_origin": "outside its frozen", "module_code": "native method code",
            "backend": "were rebound", "callback": "constructor-bound qualifier", "backend_study": "were rebound",
            "slot_repeat": "registered task/arm/repeat", "contract_reset": "immutable record conflicts",
            "cached_config": "frozen input changed",
            "bulk_drift": "bound native input identity was mutated",
            "bulk_admission": "native specification differs",
        }[scenario]
        assert expected in str(error), f"wrong rejection for {scenario}: {error} (expected {expected})"
        return False
    if scenario not in {"valid", "ids_catalog", "bulk"}: raise AssertionError("invalid native inputs were accepted: " + scenario)
    assert not list(root.rglob("started.json"))
    return True


if __name__ == "__main__":
    accepted = probe(json.loads(Path(sys.argv[1]).read_bytes()), Path(sys.argv[2]), sys.argv[3])
    print(json.dumps({"scenario": sys.argv[3], "accepted": accepted, "native_starts": 0}))
