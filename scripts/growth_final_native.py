"""Host-only binding of final coordinates to the existing native backend.

This is an input qualifier, not a launcher or an authority to register/unblind a
final attempt. The caller designates the official catalog independently. Frozen
sources, resources and configuration are authenticated here; owner isolation,
live deadline qualification and evaluation-only recovery remain external.
"""

from copy import deepcopy
import importlib
import inspect
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import CodeType

import yaml

from scripts import agent_revision, growth_native
from scripts.campaign_admission import disk_admission, study_disk_minimum
from scripts.growth_final_registry import _keys, _path, _reference, validate_registration
from scripts.growth_loop import GrowthError, GrowthInfrastructureError, encode, evaluator_environment, frozen_input, read, record, sha256
from scripts.run_opencode import load_config


MODEL = {"provider_id": "openai", "model_id": "gpt-5.6-luna", "effort": "max"}
MODULES = ("scripts.growth_final_native", "scripts.growth_final_execution", "scripts.growth_final_collect",
           "scripts.growth_final_registry", "scripts.growth_final_analysis", "scripts.growth_native",
           "scripts.growth_loop", "scripts.agent_revision", "scripts.run_opencode", "scripts.campaign_runner",
           "scripts.campaign_evaluate", "programbench.submission", "programbench.eval.eval",
           "programbench.utils.load_data", "programbench.constants", "programbench.utils.blob_store")


def source_inventory(repository):
    """Derive the complete executable/dependency inventory, never a supplied subset."""
    root = _directory(repository["path"])
    if (_git(root, "rev-parse", "HEAD").strip() != repository["commit"]
            or _git(root, "status", "--porcelain", "--untracked-files=all").strip()):
        raise GrowthError("native repository must be clean at its frozen commit")
    return [{"path": str(_path(root / name)), "sha256": sha256((root / name).read_bytes())}
            for name in sorted(_git(root, "ls-files", "-z").split("\0")) if name and (
                (name.startswith(("scripts/", "src/")) and name.endswith(".py"))
                or name in {"pyproject.toml", "uv.lock", "requirements.txt", "poetry.lock", "setup.cfg", "setup.py"})]


class FinalNativeInputs:
    """One immutable input identity, one exact ProgramBenchBackend, one qualifier.

    registration.configuration references the native-inputs manifest described
    in docs/final-confirmation.md. Construction performs no native operation.
    """

    def __init__(self, registration, *, expected_catalog_sha256):
        self.registration = deepcopy(validate_registration(registration,
            expected_catalog_sha256=expected_catalog_sha256))
        self.inputs = json.loads(_reference(self.registration["configuration"], verify=True))
        _keys(self.inputs, {"schema_version", "root", "study", "controller", "sources", "python", "resources",
                           "repeat_seeds"}, "final native inputs")
        if type(self.inputs["schema_version"]) is not int or self.inputs["schema_version"] != 1:
            raise GrowthError("unsupported final native inputs schema")
        self.root = _directory(self.inputs["root"])
        self.study = json.loads(_reference(self.inputs["study"], verify=True))
        self._identity = encode({"inputs": self.inputs, "study": self.study, "registration": self.registration})
        self.check()
        self.backend = growth_native.ProgramBenchBackend(deepcopy(self.study), self.root)
        self._methods = (self.backend.evaluate.__func__, self.backend.validate_evaluation.__func__)

    def check(self, task=None):
        if encode({"inputs": self.inputs, "study": self.study, "registration": self.registration}) != self._identity:
            raise GrowthError("bound native input identity was mutated")
        if frozen_input(self.registration["configuration"]) != encode(self.inputs):
            raise GrowthError("native inputs must retain canonical encoding")
        if frozen_input(self.inputs["study"]) != encode(self.study) or read(_path(self.root / "study.json")) != self.study:
            raise GrowthError("actual native study differs from frozen inputs")
        if (self.registration["model"] != MODEL or self.study.get("version") != 2
                or self.study.get("test_map") != "official-full-map-v1"
                or type(self.study.get("workers")) is not int or self.study["workers"] != 1):
            raise GrowthError("final native study requires Luna Max, official map, and one inner worker")
        pairing = self.registration["pairing"]
        if pairing["solver_workers"] != 2 * pairing["active_blocks"]:
            raise GrowthError("final outer workers must equal twice active blocks")
        _keys(self.inputs["repeat_seeds"], {"1", "2"}, "repeat seeds")
        seeds = list(self.inputs["repeat_seeds"].values())
        if any(not isinstance(seed, str) or not seed.strip() for seed in seeds) or seeds[0] == seeds[1]:
            raise GrowthError("two distinct native repeat seeds are required")
        if study_disk_minimum(self.study) is None:
            raise GrowthError("native study requires the existing per-instance disk admission guard")
        self._provenance()
        if hasattr(self, "backend"):
            self.require_pair(self.backend, self.qualify, self.root)
        if self.study["initial"]["agent_revision"] != self.registration["revisions"]["baseline"]:
            raise GrowthError("actual initial revision differs from the registered baseline")
        revisions = {arm: agent_revision.validate_revision(ref, self.study["revision_recipe"])
                     for arm, ref in self.registration["revisions"].items()}
        if self.study["runtime_sha256"] != revisions["baseline"]["binary"]["sha256"]:
            raise GrowthError("initial runtime hash differs from the qualified baseline")
        raw = frozen_input(self.study["config"])
        if _path(self.root / "config.yaml").read_bytes() != raw:
            raise GrowthError("backend root/config.yaml differs from the frozen configuration")
        self._config(self.root / "config.yaml")
        configured = yaml.safe_load(raw)
        if agent_revision.runtime_config(configured, self.registration["revisions"]["baseline"],
                                         self.study["revision_recipe"]) != configured:
            raise GrowthError("initial config must bind the complete baseline revision")
        evaluator_environment(self.study)
        self._resources(task)
        return revisions

    def specification(self, slot):
        """Derive a native spec from an existing registered slot, without scheduling."""
        if type(slot.get("slot")) is not int or not 1 <= slot["slot"] <= 800:
            raise GrowthError("unknown final slot")
        if encode(slot) != encode(self.registration["schedule"][slot["slot"] - 1]):
            raise GrowthError("slot differs from its registered task/arm/repeat mapping")
        ref = self.registration["revisions"][slot["arm"]]
        revision = agent_revision.validate_revision(ref, self.study["revision_recipe"])
        return {"instances": [slot["task_id"]], "seed": self.inputs["repeat_seeds"][str(slot["repeat"])],
                "agent_revision": ref, "solver_policy": revision["solver_policy"],
                "runtime_sha256": revision["binary"]["sha256"], "runner": self.study["runner"],
                "study_sha256": sha256(encode(self.study)), "test_map": self.study["test_map"]}

    def require_pair(self, backend, qualify, root, registration=None):
        if (backend is not self.backend or type(backend) is not growth_native.ProgramBenchBackend
                or getattr(qualify, "__self__", None) is not self
                or getattr(qualify, "__func__", None) is not FinalNativeInputs.qualify
                or Path(root) != self.root or backend.root != self.root or backend.study != self.study
                or tuple(getattr(getattr(backend, name, None), "__func__", None)
                         for name in ("evaluate", "validate_evaluation")) != self._methods
                or (registration is not None and encode(registration) != encode(self.registration))):
            raise GrowthError("native backend, qualifier, root or registered inputs were rebound")

    def qualify(self, slot, specification, output):
        self.check(slot.get("task_id"))
        if encode(specification) != encode(self.specification(slot)):
            raise GrowthError("native specification differs from its derived registered binding")
        if Path(output) != self.root / f"slots/{slot['slot']:06d}" or Path(output).resolve() != Path(output):
            raise GrowthError("native output differs from the bound slot")
        # Use the actual native derivation and loader, not declarations from JSON.
        raw = growth_native.runtime_configuration(self.backend.study, self.backend.root, specification)
        with tempfile.TemporaryDirectory(prefix="final-native-config-") as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(yaml.safe_dump(raw))
            self._config(path)
        self._contract(Path(output), slot)
        if not (Path(output) / "started.json").exists() and not disk_admission(
                self.root, study_disk_minimum(self.study), self.root / "native-disk-admission.json"):
            raise GrowthInfrastructureError("insufficient disk before final native admission")

    def _config(self, path):
        config = load_config(path)
        if (config.model != f"{MODEL['provider_id']}/{MODEL['model_id']}" or config.variant != MODEL["effort"]
                or not config.limits.deadline_only or config.limits.wall_time_seconds != 21600
                or config.observations or config.quality or config.excluded or config.vanilla_workflow):
            raise GrowthError("actual native config requires Luna Max and six-hour deadline only without task overrides")
        for arm in config.arms:
            for path in (arm.repo, arm.binary):
                if path is not None and (not path.is_absolute() or path.resolve() != path):
                    raise GrowthError("native runtime paths must be canonical and absolute")

    def _provenance(self):
        runner = self.study["runner"]
        python = self.inputs["python"]
        _keys(python, {"path", "resolved"}, "native Python identity")
        if (runner.get("python") != python["path"] or sys.executable != python["path"]
                or str(Path(python["path"]).resolve()) != python["resolved"]["path"]):
            raise GrowthError("native runner Python differs from the frozen executable")
        _reference(python["resolved"], verify=True)
        expected = {item["path"]: item["sha256"] for repo in (self.inputs["controller"], runner)
                    for item in source_inventory(repo)}
        if (not isinstance(self.inputs["sources"], list)
                or len({item["path"] for item in self.inputs["sources"]}) != len(self.inputs["sources"])
                or {item["path"]: item["sha256"] for item in self.inputs["sources"]} != expected):
            raise GrowthError("frozen native sources omit or change required implementation/dependency files")
        code = Path(self.inputs["controller"]["path"])
        if self.registration["analyzer"] != {"path": str(code / "scripts/growth_final_analysis.py"),
                "sha256": expected[str(code / "scripts/growth_final_analysis.py")]}:
            raise GrowthError("registered analyzer differs from the frozen native analyzer")
        # Runner subprocess and loaded scoring/config code must agree. Final
        # host orchestration may evolve independently of these native functions.
        for name in _git(code, "ls-files", "-z").split("\0"):
            if (name.startswith("scripts/campaign_") or name == "scripts/run_opencode.py"
                    or name.startswith("src/") and name.endswith(".py")):
                if expected.get(str(code / name)) != expected.get(str(Path(runner["path"]) / name)):
                    raise GrowthError("loaded native implementation differs from the subprocess runner")
        for name in MODULES:
            importlib.import_module(name)
        modules = [module for name, module in tuple(sys.modules.items()) if module is not None
                   and (name.startswith("scripts.") or name.startswith("programbench."))]
        for module in modules:
            path = Path(module.__file__)
            if path != path.resolve() or not path.is_relative_to(code) or str(path) not in expected:
                raise GrowthError("loaded native module resolved outside its frozen controller source")
            _loaded_code(module, path)
        # Also reject replacement of imported native function aliases, including
        # a same-file __code__ replacement after Python imported the module.
        for module in modules:
            for value in vars(module).values():
                if inspect.isfunction(value) and value.__module__.startswith(("scripts.", "programbench.")):
                    owner = sys.modules.get(value.__module__)
                    if owner is None or getattr(owner, value.__name__, None) is not value:
                        raise GrowthError("loaded native function alias differs from its approved owner")

    def _resources(self, selected_task=None):
        resources = json.loads(_reference(self.inputs["resources"], verify=True))
        _keys(resources, {"catalog", "metadata", "task_files", "blob_root", "blobs", "images"}, "native resources")
        if resources["catalog"] != self.registration["catalog"] or resources["metadata"] != self.study["metadata"]:
            raise GrowthError("native resources differ from the designated catalog or metadata")
        catalog = json.loads(_reference(resources["catalog"], verify=True))
        metadata = _reference(resources["metadata"], verify=True)
        if _path(self.root / "metadata.json").read_bytes() != metadata:
            raise GrowthError("backend metadata differs from frozen scoring metadata")
        from programbench.constants import TASKS_DIR, image_name_from_instance_id
        from programbench.utils.load_data import load_all_instances
        runner_tasks = Path(self.study["runner"]["path"]) / "src/programbench/data/tasks"
        if TASKS_DIR != Path(self.inputs["controller"]["path"]) / "src/programbench/data/tasks":
            raise GrowthError("loaded ProgramBench catalog directory changed")
        catalog_tasks = self.registration["task_ids"]
        if sorted(path.name for path in runner_tasks.iterdir() if (path / "task.yaml").is_file()) != catalog_tasks:
            raise GrowthError("actual runner does not contain exactly the designated 200 tasks")
        required = {str(root / task / name) for root in (TASKS_DIR, runner_tasks) for task in catalog_tasks
                    for name in ("task.yaml", "tests.json")}
        if {item["path"] for item in resources["task_files"]} != required:
            raise GrowthError("task metadata resource inventory is incomplete")
        for item in resources["task_files"]:
            _reference(item, verify=True)
        for directory in {TASKS_DIR, runner_tasks}:
            if (encode(load_all_instances(directory, include_tests=False)) != encode(catalog)
                    or encode({item["instance_id"]: item for item in load_all_instances(directory)}) != encode(json.loads(metadata))):
                raise GrowthError("actual native catalog/metadata differ from the designated resources")
        blob = _directory(resources["blob_root"])
        if os.environ.get("PROGRAMBENCH_BLOB_DIR") != str(blob):
            raise GrowthError("native evaluator must use the frozen offline blob root")
        from programbench.utils import blob_store
        if blob_store.BLOB_LOCAL_DIR != str(blob):
            raise GrowthError("loaded evaluator captured a different blob root")
        if selected_task is not None and selected_task not in catalog_tasks:
            raise GrowthError("resource task is outside the registered catalog")
        tasks = [selected_task] if selected_task is not None else catalog_tasks
        required = set()
        for task in tasks:
            directory = _directory(str(blob / task))
            entries = list(directory.rglob("*"))
            if any(path.is_symlink() for path in entries) or not any(path.is_file() for path in entries):
                raise GrowthError("opaque task blobs are absent or contain symlinks")
            required.update(str(path) for path in entries if path.is_file())
        blobs = [item for item in resources["blobs"] if selected_task is None
                 or Path(item["path"]).is_relative_to(blob / selected_task)]
        if {item["path"] for item in blobs} != required:
            raise GrowthError("opaque task blob inventory is incomplete")
        for item in blobs:
            _reference(item, verify=True)  # Opaque bytes only: never unpack grader archives.
        images = resources["images"]
        if set(images) != set(catalog_tasks):
            raise GrowthError("task image inventory does not exactly cover the designated catalog")
        for task in tasks:
            item = images[task]
            _keys(item, {"name", "sha256"}, "task image")
            if item["name"] != image_name_from_instance_id(task):
                raise GrowthError("task image name differs from the actual native loader")
            actual = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", item["name"]],
                                             text=True, timeout=30).strip()
            if actual != item["sha256"] or not actual.startswith("sha256:") or len(actual) != 71:
                raise GrowthError("task image differs from its frozen resource identity")

    def _contract(self, output, slot):
        campaign = output / ("growth-eval-" + sha256(str(output.resolve()).encode()))
        path = campaign / "procontract" / slot["task_id"] / "procontract-issue.json"
        receipt = output / "native-contract.json"
        if not path.exists():
            if receipt.exists():
                raise GrowthError("previously observed native Contract issue disappeared")
            return  # Infrastructure can fail before Contract admission. Do not invent an issue.
        issue = read(_path(path))
        started = issue.get("startedAt")
        if (type(started) not in (int, float) or not math.isfinite(started) or started <= 0
                or issue.get("payload", {}).get("budget") != {"deadline": int(started * 1000) + 21600000}
                or issue["payload"].get("model") != {"providerID": MODEL["provider_id"],
                    "id": MODEL["model_id"], "variant": MODEL["effort"]}):
            raise GrowthError("native Contract did not retain its original six-hour deadline-only envelope")
        record(receipt, {"issue": {"path": str(path), "sha256": sha256(path.read_bytes())},
                         "startedAt": started, "deadline": issue["payload"]["budget"]["deadline"]})


def require_bound_backend(backend, qualify, root, registration):
    """Reject arbitrary callbacks for real native execution; retain synthetic seams."""
    owner = getattr(qualify, "__self__", None)
    if isinstance(backend, growth_native.ProgramBenchBackend) or isinstance(owner, FinalNativeInputs):
        if type(owner) is not FinalNativeInputs:
            raise GrowthError("native execution requires its constructor-bound qualifier")
        owner.require_pair(backend, qualify, root, registration)
        owner.check()


def _directory(value):
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or path.is_symlink() or not path.is_dir():
        raise GrowthError("native input directory must be canonical and existing")
    return path


def _git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True, timeout=30)


def _loaded_code(module, path):
    compiled = compile(path.read_bytes(), str(path), "exec", dont_inherit=True)

    def codes(code, prefix=""):
        return {**({prefix: code} if prefix else {}), **{key: value for nested in code.co_consts
                if isinstance(nested, CodeType) for key, value in codes(nested, prefix + "." + nested.co_name).items()}}

    expected = codes(compiled)
    for name, value in vars(module).items():
        if inspect.isfunction(value) and value.__module__ == module.__name__:
            function = inspect.unwrap(value)
            if function.__code__ != expected.get("." + function.__qualname__):
                raise GrowthError("loaded native function code differs from frozen source")
        if inspect.isclass(value) and value.__module__ == module.__name__:
            for method, function in vars(value).items():
                if isinstance(function, (staticmethod, classmethod)):
                    function = function.__func__
                if isinstance(function, property):
                    function = function.fget
                key = "." + name + "." + method
                if inspect.isfunction(function) and key in expected and inspect.unwrap(function).__code__ != expected[key]:
                    raise GrowthError("loaded native method code differs from frozen source")
