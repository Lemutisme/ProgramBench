import json
import sys

import yaml

from scripts.growth_loop import policy, record, seal, sha256
from scripts.growth_native import ProgramBenchBackend


def test_production_evaluate_preserves_coordinates_and_namespaces_full_output(tmp_path):
    runner = tmp_path / "runner"
    (runner / "scripts").mkdir(parents=True)
    (runner / "scripts/run_opencode.py").write_text(
        "import json,os,pathlib,sys\n"
        "args=sys.argv[1:]; root=pathlib.Path(args[args.index('--output')+1]);root.mkdir(parents=True,exist_ok=True)\n"
        "phase='scoring' if '--evaluate-only' in args else 'inference'\n"
        "(root/(phase+'.json')).write_text(json.dumps(args))\n"
        "(root/(phase+'-environment.json')).write_text(json.dumps({k:os.environ.get(k) for k in ['PROGRAMBENCH_EVAL_WHEELHOUSE','PROGRAMBENCH_SAFE_REPORTING']}))\n"
    )
    original = policy(tmp_path, b"Original namespace test policy\n")
    changed = policy(tmp_path, b"Changed namespace test policy\n")
    config = {"model": "openai/gpt-5.6-luna", "variant": "max",
              "limits": {"wall_time_seconds": 21600, "provider_turns": None, "procontract_actions": None,
                         "total_cost_soft": None, "total_cost_hard": None},
              "cache": {"warmup_turns": 10, "minimum_read_ratio": 0, "stop_after_consecutive_failures": 2},
              "arms": {"procontract": {"repo": str(runner), "revision": "a" * 40, "mode": "procontract",
                                       "runtime_protocol": "policy-split-v1"},
                       "vanilla": {"repo": str(runner), "revision": "a" * 40, "mode": "vanilla"}}, "excluded": []}
    seal(tmp_path / "config.yaml", yaml.safe_dump(config).encode())
    record(tmp_path / "metadata.json", {})
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "fixture.whl").write_bytes(b"checksum fixture; never installed")
    evaluator = {"safe_reporting": True, "wheelhouse": {"path": str(wheels),
                 "files": {"fixture.whl": sha256((wheels / "fixture.whl").read_bytes())}}}
    backend = ProgramBenchBackend({"runner": {"path": str(runner), "python": sys.executable}, "workers": 3,
                                  "evaluator": evaluator}, tmp_path)
    cases = [("study-a/development/incumbent/951", original, 951),
             ("study-a/development/candidate/951", changed, 951),
             ("study-a/confirmation/candidate/952", changed, 952),
             ("study-b/development/incumbent/951", original, 951)]
    names = []
    for directory, artifact, seed in cases:
        output = tmp_path / directory
        result = backend.evaluate(output, {"solver_policy": artifact, "instances": ["same-task"], "seed": seed})
        assert result["status"] == "missing-evaluation"
        invocation = json.loads((output / "inference-invocation.json").read_text())
        arguments = invocation["argv"]
        campaign = output / arguments[arguments.index("--output") + 1]
        assert campaign.parent == output and campaign.name != "campaign"
        assert len(campaign.name) < 128
        assert json.loads((campaign / "inference.json").read_text()) == arguments[3:]
        scoring = json.loads((output / "scoring-invocation.json").read_text())["argv"]
        assert scoring == arguments + ["--resume", "--evaluate-only"]
        assert json.loads((campaign / "scoring-environment.json").read_text()) == {
            "PROGRAMBENCH_EVAL_WHEELHOUSE": str(wheels), "PROGRAMBENCH_SAFE_REPORTING": "1"}
        assert json.loads((output / "evaluator-inputs.json").read_text()) == evaluator
        assert arguments[arguments.index("--seed") + 1] == str(seed)
        assert arguments[arguments.index("--filter") + 1] == "^(same\\-task)$"
        assert arguments[arguments.index("--workers") + 1] == "3"
        actual = yaml.safe_load((output / "config.yaml").read_bytes())
        assert actual["limits"] == config["limits"] and actual["model"] == config["model"]
        assert actual["arms"]["procontract"]["policy_sha256"] == artifact["sha256"]
        names.append(campaign.name)
    assert len(set(names)) == len(cases)
