import json
import io
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from programbench.eval.eval import EvaluationResult, TestBranchError, TestResult
from scripts.campaign_state import JobState


def _instance(instance_id: str = "one", tests: tuple[str, ...] = ("t1", "t2")) -> dict[str, object]:
    return {
        "instance_id": instance_id,
        "image_name": f"{instance_id}-image",
        "branches": {"b1": {"tests": list(tests)}},
    }


def _write_eval(
    path: Path,
    *,
    statuses: tuple[str, ...] = ("passed", "failure"),
    error_code: str | None = None,
    branch_error: bool = False,
    executable_hash: str = "e" * 64,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        EvaluationResult(
            test_results=[
                TestResult(name=f"t{index}", branch="b1", status=status, extra={})
                for index, status in enumerate(statuses, 1)
            ],
            error_code=error_code,
            executable_hash=executable_hash,
            solution_branch="submission",
            test_branches=["b1"],
            test_branch_errors={
                "b1": [TestBranchError(error_code="branch_failed", error_details="details")]
            }
            if branch_error
            else {},
        ).model_dump_json()
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True))


def _campaign(
    root: Path,
    *,
    instance_ids: tuple[str, ...] = ("one",),
    missing: frozenset[tuple[str, str]] = frozenset(),
    submissions: dict[tuple[str, str], bytes] | None = None,
    executable_hashes: dict[tuple[str, str], str] | None = None,
) -> tuple[Path, list[dict[str, object]]]:
    root.mkdir()
    jobs = [
        {"instance_id": instance_id, "arm": arm, "image": f"{instance_id}-image"}
        for instance_id in instance_ids
        for arm in ("procontract", "vanilla")
    ]
    _write_json(
        root / "campaign.json",
        {
            "version": 1,
            "selected_ids": list(instance_ids),
            "jobs": jobs,
            "installs": {
                "procontract": {"revision": "pro-rev", "binary_sha256": "a" * 64},
                "vanilla": {"revision": "van-rev", "binary_sha256": "b" * 64},
            },
            "programbench": {"sha": "c" * 40, "dirty": False},
            "selection": {"limit": len(instance_ids), "seed": 42, "filter": None, "all": False},
            "seed": 42,
            "images": {instance_id: {"image": f"{instance_id}-image"} for instance_id in instance_ids},
            "config": {"model": "test-model", "variant": "test-variant"},
        },
    )
    for job in jobs:
        instance_id = str(job["instance_id"])
        arm = str(job["arm"])
        job_root = root / arm / instance_id
        archive = None
        if (instance_id, arm) not in missing:
            archive = job_root / instance_id / "submission.tar.gz"
            archive.parent.mkdir(parents=True)
            archive.write_bytes((submissions or {}).get((instance_id, arm), f"{instance_id}-{arm}".encode()))
        result = {
            "version": 1,
            "completion": "discharged" if arm == "procontract" else "idle",
            "provider_turns": 3,
            "actions": 4,
            "cost": "1.25",
            "elapsed_seconds": 12.5,
            "input_tokens": 2,
            "cache_read": 6,
            "cache_write": 2,
            "exit_class": "discharged" if arm == "procontract" else "idle",
            "executable_hash": executable_hashes.get((instance_id, arm))
            if executable_hashes is not None
            else "e" * 64,
        }
        if arm == "procontract":
            result.update(
                {
                    "status": "discharged",
                    "quiet": True,
                    "outstanding": [],
                    "package_hash": hashlib.sha256(archive.read_bytes()).hexdigest() if archive else None,
                }
            )
        result_path = job_root / f"{arm}-result.json"
        _write_json(result_path, result)
        _write_json(
            job_root / "state.json",
            JobState(
                "done",
                phase_timestamps={"planned": 1, "done": 3},
                provider_turns=3,
                actions=4,
                cost=1.25,
                exit_class="discharged" if arm == "procontract" else "idle",
                command_identity=f"{arm}-command",
                artifact_hashes={result_path.name: hashlib.sha256(result_path.read_bytes()).hexdigest()},
            ).to_dict(),
        )
    return root, [_instance(instance_id, ("t1", "t2", "t3")) for instance_id in instance_ids]


def _runner(
    calls: list[tuple[tuple[str, ...], Path, dict[str, str]]],
    statuses: dict[tuple[str, str], tuple[str, ...]] | None = None,
    *,
    stdout: str = "evaluated",
):
    def run(command, cwd, _timeout, _grace, _output_limit, environment, *, on_start):
        argv = tuple(command)
        arm_root = Path(argv[4])
        calls.append((argv, cwd, dict(environment)))
        on_start(os.getpid(), os.getpid(), "test-process")
        for submission in sorted(arm_root.glob("*/submission.tar.gz")):
            instance_id = submission.parent.name
            _write_eval(
                submission.parent / f"{instance_id}.eval.json",
                statuses=(statuses or {}).get((arm_root.name, instance_id), ("passed", "failure", "failure")),
            )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    return run


def test_validate_evaluation_uses_official_score_and_exact_ratio(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import validate_evaluation

    path = tmp_path / "one.eval.json"
    _write_eval(path)

    result = validate_evaluation(path, _instance())

    assert result.valid
    assert result.utility == Fraction(1, 2)
    assert result.resolved == 1
    assert result.total == 2
    assert result.reasons == ()
    assert len(result.eval_sha256) == 64


def test_validate_evaluation_rejects_top_level_error(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import validate_evaluation

    path = tmp_path / "one.eval.json"
    _write_eval(path, error_code="compile_failed")

    assert validate_evaluation(path, _instance()).reasons == ("top_error:compile_failed",)


def test_validate_evaluation_rejects_branch_error(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import validate_evaluation

    path = tmp_path / "one.eval.json"
    _write_eval(path, branch_error=True)

    assert validate_evaluation(path, _instance()).reasons == ("branch_error:b1:branch_failed",)


def test_validate_evaluation_rejects_foreign_branch_error(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import validate_evaluation

    path = tmp_path / "one.eval.json"
    _write_eval(path)
    raw = json.loads(path.read_text())
    raw["test_branch_errors"] = {"foreign": [{"error_code": "boom", "error_details": "detail"}]}
    _write_json(path, raw)

    assert validate_evaluation(path, _instance()).reasons == ("foreign_branch_error:foreign:boom",)


def test_validate_evaluation_rejects_system_error(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import validate_evaluation

    path = tmp_path / "one.eval.json"
    _write_eval(path, statuses=("passed", "system_error"))

    assert validate_evaluation(path, _instance()).reasons == ("system_error:b1/t2",)


def test_validate_evaluation_rejects_missing_expected_test(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import validate_evaluation

    path = tmp_path / "one.eval.json"
    _write_eval(path, statuses=("passed",))

    assert validate_evaluation(path, _instance()).reasons == ("missing_expected:b1/t2",)


def test_validate_evaluation_rejects_not_run_test(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import validate_evaluation

    path = tmp_path / "one.eval.json"
    _write_eval(path, statuses=("passed", "not_run"))

    assert validate_evaluation(path, _instance()).reasons == ("not_run:b1/t2",)


def test_validate_evaluation_rejects_malformed_json_with_hash(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import validate_evaluation

    path = tmp_path / "one.eval.json"
    path.write_text("{not json")

    result = validate_evaluation(path, _instance())

    assert not result.valid
    assert result.reasons == ("malformed_eval_json",)
    assert len(result.eval_sha256) == 64


def test_validate_evaluation_rejects_missing_json(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import validate_evaluation

    result = validate_evaluation(tmp_path / "missing.eval.json", _instance())

    assert not result.valid
    assert result.reasons == ("missing_eval_json",)
    assert result.eval_sha256 is None


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_validate_evaluation_rejects_nonstandard_json_numeric(tmp_path: Path, constant: str) -> None:
    from scripts.campaign_evaluate import validate_evaluation

    path = tmp_path / "one.eval.json"
    _write_eval(path)
    raw = path.read_text().replace('"extra":{}', f'"extra":{{"nonstandard":{constant}}}', 1)
    path.write_text(raw)

    assert validate_evaluation(path, _instance()).reasons == ("malformed_eval_json",)


def test_validate_evaluation_hashes_parse_oversize_json_before_rejecting_it(tmp_path: Path) -> None:
    from scripts import campaign_evaluate

    path = tmp_path / "one.eval.json"
    path.write_bytes(b" " * (campaign_evaluate.MAX_EVAL_JSON_BYTES + 1))
    result = campaign_evaluate.validate_evaluation(path, _instance())

    assert result.reasons == ("oversize_eval_json",)
    assert result.eval_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_validate_evaluation_rejects_deeply_nested_json_without_crashing(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import validate_evaluation

    path = tmp_path / "one.eval.json"
    path.write_text("[" * 10_000 + "]" * 10_000)

    assert validate_evaluation(path, _instance()).reasons == ("malformed_eval_json",)


def test_validate_evaluation_rejects_standard_numeric_overflow(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import validate_evaluation

    path = tmp_path / "one.eval.json"
    _write_eval(path)
    path.write_text(path.read_text().replace('"extra":{}', '"extra":{"overflow":1e999999}', 1))

    assert validate_evaluation(path, _instance()).reasons == ("malformed_eval_json",)


def test_validate_evaluation_rejects_duplicate_top_error_key(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import validate_evaluation

    path = tmp_path / "one.eval.json"
    _write_eval(path)
    path.write_text('{"error_code":"compile_failed",' + path.read_text().lstrip()[1:])

    assert validate_evaluation(path, _instance()).reasons == ("malformed_eval_json",)


def test_validate_evaluation_rejects_empty_top_error_code(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import validate_evaluation

    path = tmp_path / "one.eval.json"
    _write_eval(path, error_code="")

    assert validate_evaluation(path, _instance()).reasons == ("top_error:<empty>",)


def test_validate_evaluation_requires_submission_solution_provenance(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import validate_evaluation

    path = tmp_path / "one.eval.json"
    _write_eval(path)
    raw = json.loads(path.read_text())
    raw["solution_branch"] = "gold"
    _write_json(path, raw)

    assert validate_evaluation(path, _instance()).reasons == ("unexpected_solution_branch:gold",)


def test_validate_evaluation_rejects_error_details_without_code(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import validate_evaluation

    path = tmp_path / "one.eval.json"
    _write_eval(path)
    raw = json.loads(path.read_text())
    raw["error_details"] = "compile failed"
    _write_json(path, raw)

    assert validate_evaluation(path, _instance()).reasons == ("error_details_without_code",)


def test_campaign_invokes_each_arm_once_with_exact_batch_argv(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []

    result = evaluate_campaign(root, instances=instances, command_runner=_runner(calls))

    assert result.status == "completed"
    tag = calls[0][0][-1]
    assert tag.startswith("campaign-")
    assert calls[1][0][-1] == tag
    assert [call[0] for call in calls] == [
        (
            "uv",
            "run",
            "programbench",
            "eval",
            str(root / "evaluation/procontract"),
            "-w",
            "8",
            "-b",
            "1",
            "--docker-cpus",
            "4",
            "--image-tag",
            tag,
        ),
        (
            "uv",
            "run",
            "programbench",
            "eval",
            str(root / "evaluation/vanilla"),
            "-w",
            "8",
            "-b",
            "1",
            "--docker-cpus",
            "4",
            "--image-tag",
            tag,
        ),
    ]
    assert all("--force" not in call[0] for call in calls)
    assert all(call[1] == Path(__file__).parents[1] for call in calls)
    assert all("OPENAI_API_KEY" not in call[2] for call in calls)


def test_done_evaluation_is_reused_without_another_call(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []
    first = evaluate_campaign(root, instances=instances, command_runner=_runner(calls))
    comparison = first.comparison_path.read_bytes()

    second = evaluate_campaign(
        root,
        instances=instances,
        command_runner=lambda *_args, **_kwargs: pytest.fail("done evaluator must not run again"),
    )

    assert second.status == "completed"
    assert len(calls) == 2
    assert second.comparison_path.read_bytes() == comparison


def test_campaign_lock_prevents_concurrent_duplicate_evaluators(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []
    started = threading.Event()
    release = threading.Event()
    base = _runner(calls)

    def blocking(*args, **kwargs):
        if not calls:
            started.set()
            assert release.wait(2)
        return base(*args, **kwargs)

    outcomes = []
    first = threading.Thread(
        target=lambda: outcomes.append(evaluate_campaign(root, instances=instances, command_runner=blocking))
    )
    second = threading.Thread(
        target=lambda: outcomes.append(evaluate_campaign(root, instances=instances, command_runner=blocking))
    )
    first.start()
    assert started.wait(1)
    second.start()
    release.set()
    first.join(3)
    second.join(3)

    assert len(outcomes) == 2
    assert len(calls) == 2
    assert {outcome.status for outcome in outcomes} == {"completed"}


def test_invalid_pair_is_excluded_from_exact_aggregate(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(
        tmp_path / "campaign",
        instance_ids=("one", "two"),
        missing=frozenset({("two", "vanilla")}),
    )
    calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []
    statuses = {
        ("procontract", "one"): ("passed", "passed", "failure"),
        ("vanilla", "one"): ("passed", "failure", "failure"),
        ("procontract", "two"): ("passed", "failure", "failure"),
    }

    outcome = evaluate_campaign(root, instances=instances, command_runner=_runner(calls, statuses))
    report = json.loads(outcome.comparison_path.read_text())

    assert outcome.status == "invalid_protocol"
    assert report["aggregate"] == {
        "invalid_pairs": 1,
        "mean_delta": {"decimal": "0.3333333333333333333333333333", "denominator": 3, "numerator": 1},
        "valid_pairs": 1,
        "worst_delta": {"decimal": "0.3333333333333333333333333333", "denominator": 3, "numerator": 1},
    }
    assert report["pairs"][0]["delta"]["numerator"] == 1
    assert report["pairs"][1]["delta"] is None
    assert report["pairs"][1]["vanilla"]["errors"] == ["missing_submission"]


def test_outputs_are_atomic_deterministic_and_sanitized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    secret = "evaluation-secret-value"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []

    first = evaluate_campaign(root, instances=instances, command_runner=_runner(calls, stdout=f"ok {secret}"))
    comparison = first.comparison_path.read_bytes()
    result_md = first.result_path.read_bytes()
    second = evaluate_campaign(root, instances=instances, command_runner=_runner(calls))

    assert second.comparison_path.read_bytes() == comparison
    assert second.result_path.read_bytes() == result_md
    assert not list(root.rglob("*.tmp"))
    assert secret.encode() not in b"".join(path.read_bytes() for path in root.rglob("*") if path.is_file())


def test_eval_derived_errors_are_sanitized_in_control_reports_but_raw_eval_is_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    secret = "sk-evaluator-secret-123456"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    root, instances = _campaign(tmp_path / "campaign")

    def secret_error(command, _cwd, _timeout, _grace, _limit, _environment, *, on_start):
        on_start(os.getpid(), os.getpid(), "secret-error")
        for submission in Path(command[4]).glob("*/submission.tar.gz"):
            _write_eval(
                submission.parent / f"{submission.parent.name}.eval.json",
                statuses=("passed", "failure", "failure"),
                error_code=f"api_key={secret}",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    outcome = evaluate_campaign(root, instances=instances, command_runner=secret_error)

    assert secret not in outcome.comparison_path.read_text()
    assert secret not in outcome.result_path.read_text()
    assert "[REDACTED]" in outcome.comparison_path.read_text()
    assert secret in (root / "evaluation/procontract/one/one.eval.json").read_text()


def test_frozen_coordinate_values_and_keys_are_sanitized_in_published_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    secret = "coordinate-secret-123456"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    root, instances = _campaign(tmp_path / "campaign")
    manifest_path = root / "campaign.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["installs"]["procontract"][f"token={secret}"] = f"password={secret}"
    _write_json(manifest_path, manifest)

    outcome = evaluate_campaign(root, instances=instances, command_runner=_runner([]))

    assert secret not in outcome.comparison_path.read_text()
    assert secret not in (root / "comparison.bundle.json").read_text()
    assert "[REDACTED]" in outcome.comparison_path.read_text()


def test_malformed_and_parse_oversize_eval_hashes_survive_state_and_report(tmp_path: Path) -> None:
    from scripts import campaign_evaluate

    root, instances = _campaign(tmp_path / "campaign")

    def invalid_results(command, _cwd, _timeout, _grace, _limit, _environment, *, on_start):
        on_start(os.getpid(), os.getpid(), "invalid-results")
        arm_root = Path(command[4])
        path = arm_root / "one/one.eval.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            b"{malformed"
            if arm_root.name == "procontract"
            else b" " * (campaign_evaluate.MAX_EVAL_JSON_BYTES + 1)
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    first = campaign_evaluate.evaluate_campaign(root, instances=instances, command_runner=invalid_results)
    comparison = first.comparison_path.read_bytes()
    report = json.loads(comparison)

    assert first.status == "invalid_protocol"
    for arm, reason in (("procontract", "malformed_eval_json"), ("vanilla", "oversize_eval_json")):
        path = root / f"evaluation/{arm}/one/one.eval.json"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        state = json.loads((root / f"evaluation-state/{arm}.json").read_text())
        assert state["artifact_hashes"]["one.eval.json"] == digest
        assert report["pairs"][0][arm]["artifact_hashes"]["one.eval.json"] == digest
        assert reason in report["pairs"][0][arm]["errors"]

    second = campaign_evaluate.evaluate_campaign(
        root,
        instances=instances,
        command_runner=lambda *_args, **_kwargs: pytest.fail("done invalid results must be reused"),
    )

    assert second.comparison_path.read_bytes() == comparison


def test_eval_beyond_artifact_hash_cap_records_size_reason_and_reuses_deterministically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import campaign_evaluate

    monkeypatch.setattr(campaign_evaluate, "MAX_EVAL_JSON_BYTES", 32)
    monkeypatch.setattr(campaign_evaluate, "MAX_EVAL_ARTIFACT_HASH_BYTES", 64)
    root, instances = _campaign(tmp_path / "campaign")

    def too_large(command, _cwd, _timeout, _grace, _limit, _environment, *, on_start):
        on_start(os.getpid(), os.getpid(), "oversize-artifact")
        path = Path(command[4]) / "one/one.eval.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b" " * 65)
        return subprocess.CompletedProcess(command, 0, "", "")

    first = campaign_evaluate.evaluate_campaign(root, instances=instances, command_runner=too_large)
    comparison = first.comparison_path.read_bytes()
    report = json.loads(comparison)

    assert first.status == "evaluator_failed"
    for arm in ("procontract", "vanilla"):
        state = json.loads((root / f"evaluation-state/{arm}.json").read_text())
        pair = report["pairs"][0][arm]
        assert state["eval_artifact_sizes"] == {"one": 65}
        assert state["eval_hash_unavailable"] == {"one": "artifact_size_limit"}
        assert "one.eval.json" not in state["artifact_hashes"]
        assert pair["eval_artifact_size_bytes"] == 65
        assert pair["eval_hash_unavailable_reason"] == "artifact_size_limit"
        assert "hash_unavailable:artifact_size_limit" in pair["errors"]

    second = campaign_evaluate.evaluate_campaign(
        root,
        instances=instances,
        command_runner=lambda *_args, **_kwargs: pytest.fail("unhashable done result must be reused"),
    )
    assert second.comparison_path.read_bytes() == comparison


def test_interrupted_evaluator_requires_explicit_retry_and_skips_done_arm(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    first_calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []
    complete = _runner(first_calls)

    def interrupt_procontract(*args, **kwargs):
        if Path(args[0][4]).name == "procontract":
            kwargs["on_start"](os.getpid(), os.getpid(), "test-process")
            raise InterruptedError("lost controller")
        return complete(*args, **kwargs)

    first = evaluate_campaign(root, instances=instances, command_runner=interrupt_procontract)
    assert first.status == "evaluator_failed"

    with pytest.raises(CampaignEvaluationError, match="--retry-evaluator"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must reject"))

    retry_calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []
    retried = evaluate_campaign(
        root,
        instances=instances,
        retry_evaluator=True,
        command_runner=_runner(retry_calls),
    )

    assert retried.status == "completed"
    assert [Path(call[0][4]).name for call in retry_calls] == ["procontract"]


def test_exit_zero_missing_eval_is_interrupted_and_explicit_retry_completes_missing_only(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(
        tmp_path / "campaign",
        instance_ids=("one", "two"),
        missing=frozenset({("one", "vanilla"), ("two", "vanilla")}),
    )
    first_calls = []

    def partial(command, cwd, _timeout, _grace, _limit, environment, *, on_start):
        first_calls.append(tuple(command))
        on_start(os.getpid(), os.getpid(), "partial-process")
        arm_root = Path(command[4])
        _write_eval(
            arm_root / "one/one.eval.json",
            statuses=("passed", "failure", "failure"),
            branch_error=True,
        )
        return subprocess.CompletedProcess(command, 0, "partial", "")

    first = evaluate_campaign(root, instances=instances, command_runner=partial)
    interrupted = json.loads((root / "evaluation-state/procontract.json").read_text())

    assert first.status == "evaluator_failed"
    assert len(first_calls) == 1
    assert interrupted["status"] == "interrupted"
    assert interrupted["error"] == "failed_incomplete"
    assert interrupted["missing_eval_ids"] == ["two"]
    assert "one.eval.json" in interrupted["artifact_hashes"]

    with pytest.raises(CampaignEvaluationError, match="two"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must reject"))

    retry_calls = []
    preserved = (root / "evaluation/procontract/one/one.eval.json").read_bytes()

    def finish(command, cwd, _timeout, _grace, _limit, environment, *, on_start):
        retry_calls.append(tuple(command))
        on_start(os.getpid(), os.getpid(), "retry-process")
        arm_root = Path(command[4])
        assert (arm_root / "one/one.eval.json").is_file()
        _write_eval(arm_root / "one/one.eval.json", statuses=("passed", "passed", "passed"))
        _write_eval(arm_root / "two/two.eval.json", statuses=("failure", "failure", "failure"))
        return subprocess.CompletedProcess(command, 0, "complete", "")

    evaluate_campaign(root, instances=instances, retry_evaluator=True, command_runner=finish)
    done = json.loads((root / "evaluation-state/procontract.json").read_text())

    assert len(retry_calls) == 1
    assert done["status"] == "done"
    assert done["missing_eval_ids"] == []
    assert (root / "evaluation/procontract/one/one.eval.json").read_bytes() == preserved
    assert {name for name in done["artifact_hashes"] if name.endswith(".eval.json")} == {
        "one.eval.json",
        "two.eval.json",
    }


def test_retry_preservation_is_bounded_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import campaign_evaluate

    root, instances = _campaign(
        tmp_path / "campaign",
        instance_ids=("one", "two"),
        missing=frozenset({("one", "vanilla"), ("two", "vanilla")}),
    )

    def partial(command, _cwd, _timeout, _grace, _limit, _environment, *, on_start):
        on_start(os.getpid(), os.getpid(), "partial")
        path = Path(command[4]) / "one/one.eval.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 65)
        return subprocess.CompletedProcess(command, 0, "", "")

    campaign_evaluate.evaluate_campaign(root, instances=instances, command_runner=partial)
    monkeypatch.setattr(campaign_evaluate, "MAX_PRESERVED_EVAL_FILE_BYTES", 64)

    with pytest.raises(campaign_evaluate.CampaignEvaluationError, match="preservation.*limit"):
        campaign_evaluate.evaluate_campaign(
            root,
            instances=instances,
            retry_evaluator=True,
            command_runner=lambda *_args, **_kwargs: pytest.fail("limit must fail before launch"),
        )
    assert not (root / "evaluation-state/procontract.preserved").exists()


def test_response_loss_with_complete_artifacts_reconciles_without_retry(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []
    complete = _runner(calls)

    def lose_response(*args, **kwargs):
        completed = complete(*args, **kwargs)
        if Path(args[0][4]).name == "procontract":
            raise OSError("response lost")
        return completed

    first = evaluate_campaign(root, instances=instances, command_runner=lose_response)
    second = evaluate_campaign(
        root,
        instances=instances,
        command_runner=lambda *_args, **_kwargs: pytest.fail("complete artifacts must be adopted"),
    )

    assert first.status == "evaluator_failed"
    assert second.status == "evaluator_failed"


@pytest.mark.parametrize("durable_status", ["planned", "interrupted"])
def test_ambiguous_dead_state_rejects_unrecorded_eval_artifacts(
    tmp_path: Path,
    durable_status: str,
) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []

    def interrupt(command, cwd, timeout, grace, limit, environment, *, on_start):
        if Path(command[4]).name == "procontract":
            on_start(os.getpid(), os.getpid(), "test-process")
            raise InterruptedError("lost")
        return _runner(calls)(command, cwd, timeout, grace, limit, environment, on_start=on_start)

    evaluate_campaign(root, instances=instances, command_runner=interrupt)
    _write_eval(root / "evaluation/procontract/one/one.eval.json", statuses=("passed", "failure", "failure"))
    state_path = root / "evaluation-state/procontract.json"
    state = json.loads(state_path.read_text())
    state["status"] = durable_status
    _write_json(state_path, state)

    with pytest.raises(CampaignEvaluationError, match="artifact conflict"):
        evaluate_campaign(
            root,
            instances=instances,
            command_runner=lambda *_args, **_kwargs: pytest.fail("foreign artifacts must not run"),
        )


def test_running_exact_process_is_adopted_then_complete_artifacts_reconciled(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []
    complete = _runner(calls)

    def lose_controller(*args, **kwargs):
        completed = complete(*args, **kwargs)
        if Path(args[0][4]).name == "procontract":
            raise InterruptedError("controller died")
        return completed

    evaluate_campaign(root, instances=instances, command_runner=lose_controller)
    state_path = root / "evaluation-state/procontract.json"
    state = json.loads(state_path.read_text())
    state.update({"status": "running", "pid": 123, "pgid": 123, "process_start": "exact-start"})
    _write_json(state_path, state)
    Path(state["completion_path"]).write_text("0\n")
    observations = iter((True, False))

    result = evaluate_campaign(
        root,
        instances=instances,
        command_runner=lambda *_args, **_kwargs: pytest.fail("adopted evaluator must not duplicate"),
        process_probe=lambda pid, started: (pid, started, next(observations))[-1],
        sleeper=lambda _delay: None,
        adoption_timeout=1,
    )

    assert result.status == "completed"


def test_dead_running_process_reconciles_complete_eval_when_output_pipes_were_lost(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")

    def interrupt(command, _cwd, _timeout, _grace, _limit, _environment, *, on_start):
        on_start(os.getpid(), os.getpid(), "test-process")
        if Path(command[4]).name == "procontract":
            raise InterruptedError("controller died")
        return _runner([])(command, _cwd, _timeout, _grace, _limit, _environment, on_start=on_start)

    evaluate_campaign(root, instances=instances, command_runner=interrupt)
    _write_eval(root / "evaluation/procontract/one/one.eval.json", statuses=("passed", "failure", "failure"))
    state_path = root / "evaluation-state/procontract.json"
    state = json.loads(state_path.read_text())
    state.update({"status": "running", "pid": 999999, "pgid": 999999, "process_start": "dead"})
    _write_json(state_path, state)
    Path(state["completion_path"]).write_text("0\n")
    (root / "evaluation-state/procontract.stdout").unlink()
    (root / "evaluation-state/procontract.stderr").unlink()

    outcome = evaluate_campaign(
        root,
        instances=instances,
        command_runner=lambda *_args, **_kwargs: pytest.fail("complete eval must not rerun"),
        process_probe=lambda _pid, _start: False,
    )

    assert outcome.status == "completed"
    assert "unavailable" in (root / "evaluation-state/procontract.stderr").read_text()


def test_surviving_evaluator_process_group_blocks_retry_after_root_pid_loss(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")

    def interrupt(command, _cwd, _timeout, _grace, _limit, _environment, *, on_start):
        on_start(os.getpid(), os.getpid(), "test-process")
        raise InterruptedError("controller died")

    evaluate_campaign(root, instances=instances, command_runner=interrupt)
    state_path = root / "evaluation-state/procontract.json"
    state = json.loads(state_path.read_text())
    state.update({"status": "running", "pid": 999999, "pgid": 12345, "process_start": "dead"})
    _write_json(state_path, state)

    with pytest.raises(CampaignEvaluationError, match="still running"):
        evaluate_campaign(
            root,
            instances=instances,
            command_runner=lambda *_args, **_kwargs: pytest.fail("surviving group must not duplicate"),
            process_probe=lambda _pid, _start: False,
            process_group_probe=lambda pgid: pgid == 12345,
            sleeper=lambda _delay: None,
            adoption_timeout=0,
        )


def test_interrupted_retry_refuses_launch_while_retained_cgroup_is_alive(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")

    def interrupt(command, _cwd, _timeout, _grace, _limit, _environment, *, on_start):
        cgroup = tmp_path / "retained-cgroup"
        cgroup.mkdir(exist_ok=True)
        on_start(999999, 999999, "dead-root", cgroup)
        raise InterruptedError("controller lost")

    evaluate_campaign(root, instances=instances, command_runner=interrupt)

    with pytest.raises(CampaignEvaluationError, match="still running"):
        evaluate_campaign(
            root,
            instances=instances,
            retry_evaluator=True,
            command_runner=lambda *_args, **_kwargs: pytest.fail("live cgroup must block duplicate"),
            process_probe=lambda _pid, _start: False,
            process_group_probe=lambda _pgid: False,
            cgroup_probe=lambda path: path.name == "retained-cgroup",
            sleeper=lambda _delay: None,
            adoption_timeout=0,
        )


def test_interrupted_retry_cleans_scoped_orphan_containers_after_host_ownership_dies(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")

    def interrupt(command, _cwd, _timeout, _grace, _limit, _environment, *, on_start):
        on_start(999999, 999999, "dead-root")
        raise InterruptedError("controller lost")

    evaluate_campaign(root, instances=instances, command_runner=interrupt)
    containers = {"orphan-1"}
    cleanup_calls = []

    def cleanup(prefix):
        cleanup_calls.append(prefix)
        found = tuple(containers)
        containers.clear()
        return found

    outcome = evaluate_campaign(
        root,
        instances=instances,
        retry_evaluator=True,
        command_runner=_runner([]),
        process_probe=lambda _pid, _start: False,
        process_group_probe=lambda _pgid: False,
        cgroup_probe=lambda _path: False,
        container_probe=lambda _prefix: bool(containers),
        container_cleanup=cleanup,
    )

    assert outcome.status == "completed"
    assert len(cleanup_calls) == 1
    assert json.loads((root / "evaluation-state/procontract.json").read_text())["recovered_container_ids"] == [
        "orphan-1"
    ]


def test_nonterminal_job_blocks_all_evaluator_invocations(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    state_path = root / "procontract/one/state.json"
    state = json.loads(state_path.read_text())
    state["phase"] = "executing"
    state["phase_timestamps"] = {"planned": 1, "executing": 2}
    _write_json(state_path, state)

    with pytest.raises(CampaignEvaluationError, match="nonterminal"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))


def test_unresolved_breaker_pause_blocks_all_evaluator_invocations(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign
    from scripts.campaign_usage import CircuitBreaker

    root, instances = _campaign(tmp_path / "campaign")
    breaker = CircuitBreaker(cache_warmup_turns=0)
    breaker.observe_error(401)
    _write_json(root / "breaker.json", breaker.snapshot())

    with pytest.raises(CampaignEvaluationError, match="paused"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))


def test_credential_leak_terminal_state_blocks_all_evaluator_invocations(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    state_path = root / "vanilla/one/state.json"
    state = json.loads(state_path.read_text())
    state["exit_class"] = "credential_leak"
    _write_json(state_path, state)

    with pytest.raises(CampaignEvaluationError, match="credential leak"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))


def test_nonzero_evaluator_exit_has_distinct_failure_status(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")

    def fail(command, _cwd, _timeout, _grace, _limit, _environment, *, on_start):
        on_start(os.getpid(), os.getpid(), "test-process")
        return subprocess.CompletedProcess(command, 2, "", "evaluator failed")

    result = evaluate_campaign(root, instances=instances, command_runner=fail)

    assert result.status == "evaluator_failed"


def test_failed_evaluator_state_rejects_recorded_artifact_mutation(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")

    def fail_with_eval(command, _cwd, _timeout, _grace, _limit, _environment, *, on_start):
        on_start(os.getpid(), os.getpid(), "failed")
        path = Path(command[4]) / "one/one.eval.json"
        _write_eval(path, statuses=("passed", "failure", "failure"))
        raise OSError("lost response")

    evaluate_campaign(root, instances=instances, command_runner=fail_with_eval)
    path = root / "evaluation/procontract/one/one.eval.json"
    path.write_bytes(path.read_bytes() + b" ")

    with pytest.raises(CampaignEvaluationError, match="artifact conflict"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))


def test_nonzero_evaluator_with_complete_json_still_invalidates_arm(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    complete = _runner([])

    def fail_after_results(*args, **kwargs):
        result = complete(*args, **kwargs)
        if Path(args[0][4]).name == "vanilla":
            return subprocess.CompletedProcess(result.args, 2, result.stdout, "evaluator failed")
        return result

    outcome = evaluate_campaign(root, instances=instances, command_runner=fail_after_results)
    pair = json.loads(outcome.comparison_path.read_text())["pairs"][0]

    assert outcome.status == "evaluator_failed"
    assert not pair["vanilla"]["valid"]
    assert pair["vanilla"]["errors"] == ["evaluator_error:evaluator_exit_2"]
    assert pair["vanilla"]["utility"] == {
        "decimal": "0.3333333333333333333333333333",
        "denominator": 3,
        "numerator": 1,
    }
    assert pair["vanilla"]["resolved"] == 1
    assert pair["vanilla"]["total"] == 3
    assert pair["delta"] is None


def test_campaign_process_reports_exact_process_identity_before_wait() -> None:
    from scripts import campaign_process

    observed = []
    result = campaign_process.run_bounded(
        (sys.executable, "-c", "print('ok')"),
        None,
        5,
        1,
        1024,
        on_start=lambda pid, pgid, started: observed.append((pid, pgid, started)),
    )

    assert result.returncode == 0
    assert len(observed) == 1
    assert observed[0][0] == observed[0][1]
    assert observed[0][0] > 0
    assert observed[0][2]


def test_campaign_process_does_not_execute_command_until_identity_is_durable(tmp_path: Path) -> None:
    from scripts import campaign_process

    marker = tmp_path / "executed"
    identity_started = threading.Event()
    release_identity = threading.Event()
    results = []

    def on_start(_pid, _pgid, _started):
        identity_started.set()
        assert release_identity.wait(2)

    thread = threading.Thread(
        target=lambda: results.append(
            campaign_process.run_bounded(
                (sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"),
                None,
                5,
                1,
                1024,
                on_start=on_start,
            )
        )
    )
    thread.start()
    assert identity_started.wait(1)
    time.sleep(0.1)
    executed_before_identity = marker.exists()
    release_identity.set()
    thread.join(2)

    assert not executed_before_identity
    assert results[0].returncode == 0
    assert marker.read_text() == "ran"


def test_contained_process_does_not_execute_until_cgroup_identity_is_durable(tmp_path: Path) -> None:
    from scripts import campaign_process

    marker = tmp_path / "executed"
    observed = []

    def on_start(pid, pgid, started, cgroup):
        observed.append((pid, pgid, started, cgroup, set(cgroup.joinpath("cgroup.procs").read_text().split())))
        assert not marker.exists()

    result = campaign_process.run_checked(
        (sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"),
        None,
        5,
        1,
        1024,
        on_start=on_start,
    )

    assert result.returncode == 0
    assert len(observed) == 1
    assert observed[0][0] == observed[0][1]
    assert str(observed[0][0]) in observed[0][4]
    assert marker.read_text() == "ran"


def test_contained_timeout_kills_double_fork_setsid_descendant_without_late_artifact(tmp_path: Path) -> None:
    from scripts import campaign_process

    pid_path = tmp_path / "grandchild.pid"
    late = tmp_path / "late"
    source = (
        "import os,time\n"
        "from pathlib import Path\n"
        "if os.fork() == 0:\n"
        " os.setsid()\n"
        " if os.fork() == 0:\n"
        f"  Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"
        "  time.sleep(2)\n"
        f"  Path({str(late)!r}).write_text('late')\n"
        " os._exit(0)\n"
        "time.sleep(5)\n"
    )

    with pytest.raises(TimeoutError):
        campaign_process.run_checked((sys.executable, "-c", source), None, 0.3, 0.1, 4096)

    grandchild = int(pid_path.read_text())
    assert not Path(f"/proc/{grandchild}").exists()
    time.sleep(0.4)
    assert not late.exists()


def test_contained_keyboard_interrupt_kills_double_fork_setsid_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import campaign_process

    pid_path = tmp_path / "grandchild.pid"
    late = tmp_path / "late"
    source = (
        "import os,time\n"
        "from pathlib import Path\n"
        "if os.fork() == 0:\n"
        " os.setsid()\n"
        " if os.fork() == 0:\n"
        f"  Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"
        "  time.sleep(2)\n"
        f"  Path({str(late)!r}).write_text('late')\n"
        " os._exit(0)\n"
        "time.sleep(5)\n"
    )

    def interrupt(_process, _cgroup, _timeout):
        deadline = time.monotonic() + 2
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise KeyboardInterrupt

    monkeypatch.setattr(campaign_process, "_wait_contained", interrupt)
    with pytest.raises(KeyboardInterrupt):
        campaign_process.run_checked((sys.executable, "-c", source), None, 5, 0.1, 4096)

    grandchild = int(pid_path.read_text())
    assert not Path(f"/proc/{grandchild}").exists()
    time.sleep(0.4)
    assert not late.exists()


@pytest.mark.parametrize("failure", [TimeoutError(), None])
def test_exception_with_eval_files_never_marks_evaluator_done(
    tmp_path: Path,
    failure: BaseException | None,
) -> None:
    from scripts import campaign_evaluate, campaign_process

    root, instances = _campaign(tmp_path / "campaign")
    error = failure or campaign_process.ProcessOutputLimitError("too much output")

    def fail_with_file(command, _cwd, _timeout, _grace, _limit, _environment, *, on_start):
        on_start(os.getpid(), os.getpid(), "failed-evaluator")
        arm_root = Path(command[4])
        _write_eval(arm_root / "one/one.eval.json", statuses=("passed", "failure", "failure"))
        raise error

    outcome = campaign_evaluate.evaluate_campaign(root, instances=instances, command_runner=fail_with_file)

    assert outcome.status == "evaluator_failed"
    for arm in ("procontract", "vanilla"):
        assert json.loads((root / f"evaluation-state/{arm}.json").read_text())["status"] != "done"
    if json.loads((root / "evaluation-state/procontract.json").read_text())["status"] == "interrupted":
        with pytest.raises(campaign_evaluate.CampaignEvaluationError, match="interrupted"):
            campaign_evaluate.evaluate_campaign(
                root,
                instances=instances,
                command_runner=lambda *_args, **_kwargs: pytest.fail("exception output must not reconcile"),
            )
    else:
        reused = campaign_evaluate.evaluate_campaign(
            root,
            instances=instances,
            command_runner=lambda *_args, **_kwargs: pytest.fail("failed output must not rerun"),
        )
        assert reused.status == "evaluator_failed"


def test_explicit_retry_quarantines_exception_partial_eval_and_sidecar(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")

    def timeout_with_partial(command, _cwd, _timeout, _grace, _limit, _environment, *, on_start):
        on_start(999999, 999999, "timeout")
        path = Path(command[4]) / "one/one.eval.json"
        _write_eval(path, statuses=("passed", "failure", "failure"))
        raise TimeoutError

    evaluate_campaign(root, instances=instances, command_runner=timeout_with_partial)
    partial = root / "evaluation/procontract/one/one.eval.partial.provenance.json"
    assert json.loads(partial.read_text())["state"] == "partial"

    def retry(command, _cwd, _timeout, _grace, _limit, _environment, *, on_start):
        on_start(os.getpid(), os.getpid(), "retry")
        path = Path(command[4]) / "one/one.eval.json"
        assert not path.exists()
        assert not path.with_name("one.eval.partial.provenance.json").exists()
        _write_eval(path, statuses=("passed", "failure", "failure"))
        return subprocess.CompletedProcess(command, 0, "", "")

    outcome = evaluate_campaign(
        root,
        instances=instances,
        retry_evaluator=True,
        command_runner=retry,
        process_probe=lambda _pid, _started: False,
        process_group_probe=lambda _pgid: False,
        cgroup_probe=lambda _path: False,
    )

    assert outcome.status == "completed"
    quarantine = root / "evaluation-state/procontract.partial"
    assert len(list(quarantine.glob("attempt-*/*.one.eval.json"))) == 1
    assert len(list(quarantine.glob("attempt-*/*.one.eval.partial.provenance.json"))) == 1


def test_more_than_sixteen_explicit_retries_are_allowed_while_quarantine_is_within_cap(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")

    def fail(command, _cwd, _timeout, _grace, _limit, _environment, *, on_start):
        on_start(999999, 999999, "retry")
        path = Path(command[4]) / "one/one.eval.json"
        _write_eval(path, statuses=("passed", "failure", "failure"))
        raise TimeoutError

    evaluate_campaign(root, instances=instances, command_runner=fail)
    for _ in range(16):
        evaluate_campaign(
            root,
            instances=instances,
            retry_evaluator=True,
            command_runner=fail,
            process_probe=lambda _pid, _started: False,
            process_group_probe=lambda _pgid: False,
            cgroup_probe=lambda _path: False,
        )

    state = json.loads((root / "evaluation-state/procontract.json").read_text())
    assert state["attempt"] == 17
    assert [record["attempt"] for record in state["partial_attempt_records"]] == list(range(1, 17))
    assert all(len(record["record_hash"]) == 64 for record in state["partial_attempt_records"])
    assert sum(record["raw_retained"] for record in state["partial_attempt_records"]) <= 1


def test_keyboard_interrupt_with_eval_file_persists_interrupted_then_reraises(tmp_path: Path) -> None:
    from scripts import campaign_evaluate

    root, instances = _campaign(tmp_path / "campaign")

    def interrupt_with_file(command, _cwd, _timeout, _grace, _limit, _environment, *, on_start):
        on_start(os.getpid(), os.getpid(), "interrupted-evaluator")
        arm_root = Path(command[4])
        _write_eval(arm_root / "one/one.eval.json", statuses=("passed", "failure", "failure"))
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        campaign_evaluate.evaluate_campaign(root, instances=instances, command_runner=interrupt_with_file)

    state = json.loads((root / "evaluation-state/procontract.json").read_text())
    assert state["status"] == "interrupted"
    assert state["error"] == "KeyboardInterrupt"


def test_evaluator_default_runner_persists_contained_cgroup_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import campaign_evaluate, campaign_process

    root, instances = _campaign(tmp_path / "campaign")
    calls = []
    blob = tmp_path / "blobs/one/tests/b1.tar.gz"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"tests")
    monkeypatch.setenv("PROGRAMBENCH_BLOB_DIR", str(tmp_path / "blobs"))

    def contained(command, cwd, timeout, grace, limit, *, environment, on_start=None, completion_path=None):
        if command[:3] == ("docker", "image", "inspect"):
            return subprocess.CompletedProcess(command, 0, "sha256:" + "d" * 64 + "\n", "")
        if command[:2] in {("docker", "tag"), ("docker", "rmi")}:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ("docker", "ps"):
            return subprocess.CompletedProcess(command, 0, "", "")
        calls.append(tuple(command))
        cgroup = tmp_path / f"cgroup-{len(calls)}"
        cgroup.mkdir()
        (cgroup / "cgroup.procs").write_text(str(os.getpid()))
        on_start(os.getpid(), os.getpid(), "contained-start", cgroup)
        for submission in Path(command[4]).glob("*/submission.tar.gz"):
            _write_eval(
                submission.parent / f"{submission.parent.name}.eval.json",
                statuses=("passed", "failure", "failure"),
            )
        completion_path.write_text("0\n")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(campaign_process, "run_checked", contained)
    monkeypatch.setattr(
        campaign_process,
        "run_bounded",
        lambda *_args, **_kwargs: pytest.fail("evaluator must use cgroup-contained runner"),
    )
    outcome = campaign_evaluate.evaluate_campaign(root, instances=instances)

    assert outcome.status == "completed"
    assert len(calls) == 2
    for arm in ("procontract", "vanilla"):
        state = json.loads((root / f"evaluation-state/{arm}.json").read_text())
        assert state["cgroup_path"].endswith(f"cgroup-{1 if arm == 'procontract' else 2}")
        assert state["evaluator_images"]["one"]["digest"] == "sha256:" + "d" * 64


def test_contained_evaluator_timeout_removes_scoped_docker_containers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import campaign_evaluate, campaign_process

    root, instances = _campaign(tmp_path / "campaign")
    containers = set()
    blob = tmp_path / "blobs/one/tests/b1.tar.gz"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"tests")
    monkeypatch.setenv("PROGRAMBENCH_BLOB_DIR", str(tmp_path / "blobs"))

    def contained(command, cwd, timeout, grace, limit, *, environment, on_start=None, completion_path=None):
        if command[:3] == ("docker", "image", "inspect"):
            return subprocess.CompletedProcess(command, 0, "sha256:" + "d" * 64 + "\n", "")
        if command[:2] == ("docker", "ps"):
            return subprocess.CompletedProcess(command, 0, "\n".join(sorted(containers)), "")
        if command[:3] == ("docker", "rm", "-f"):
            containers.difference_update(command[3:])
            return subprocess.CompletedProcess(command, 0, "", "")
        cgroup = tmp_path / f"cgroup-{len(containers)}"
        cgroup.mkdir(exist_ok=True)
        (cgroup / "cgroup.procs").write_text(str(os.getpid()))
        on_start(os.getpid(), os.getpid(), "timeout", cgroup)
        containers.add("container-" + Path(command[4]).name)
        raise TimeoutError

    monkeypatch.setattr(campaign_process, "run_checked", contained)
    outcome = campaign_evaluate.evaluate_campaign(root, instances=instances)

    assert outcome.status == "evaluator_failed"
    assert containers == set()
    for arm in ("procontract", "vanilla"):
        state = json.loads((root / f"evaluation-state/{arm}.json").read_text())
        assert state["status"] == "interrupted"
        assert state["evaluator_container_ids"] == ["container-" + arm]


def test_cli_accepts_evaluate_and_requires_it_for_retry() -> None:
    from scripts.campaign_runner import parse_args

    args = parse_args(["--config", "campaign.yaml", "--output", "out", "--evaluate"])

    assert args.evaluate
    assert not args.retry_evaluator
    with pytest.raises(SystemExit, match="2"):
        parse_args(["--config", "campaign.yaml", "--output", "out", "--retry-evaluator"])


def test_cli_evaluates_only_after_inference_is_terminal_and_returns_distinct_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import campaign_evaluate, campaign_runner
    from scripts.campaign_usage import CircuitBreaker

    root, _ = _campaign(tmp_path / "campaign")
    _write_json(root / "breaker.json", CircuitBreaker().snapshot())
    config = object()
    plan = SimpleNamespace(
        output=root,
        config=config,
        jobs=(campaign_runner.CampaignJob("fresh", "vanilla", "fresh-image"),),
        dry_run=False,
        cleanup_only=False,
    )
    events = []
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "test-password")
    monkeypatch.setattr(campaign_runner, "load_config", lambda _path: config)
    monkeypatch.setattr(campaign_runner, "load_all_instances", lambda **_kwargs: [])
    monkeypatch.setattr(campaign_runner, "plan_campaign", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(
        campaign_runner,
        "run_campaign",
        lambda *_args, **_kwargs: (
            events.append("inference_terminal"),
            campaign_runner.CampaignResult(1, 0, (), 0, None),
        )[1],
    )
    monkeypatch.setattr(
        campaign_evaluate,
        "evaluate_campaign",
        lambda campaign_root, *, retry_evaluator: (
            events.append(("evaluate", campaign_root, retry_evaluator)),
            campaign_evaluate.CampaignEvaluationResult(
                "invalid_protocol",
                0,
                1,
                root / "comparison.json",
                root / "RESULT.md",
            ),
        )[1],
    )

    status = campaign_runner.main(
        ["--config", str(tmp_path / "campaign.yaml"), "--output", str(root), "--resume", "--evaluate"]
    )

    assert status == 1
    assert events == ["inference_terminal", ("evaluate", root, False)]


def test_evaluator_state_is_planned_before_launch_and_running_before_wait(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    base = _runner([])

    def inspect(command, *args, on_start, **kwargs):
        arm = Path(command[4]).name
        state_path = root / f"evaluation-state/{arm}.json"
        planned = json.loads(state_path.read_text())
        assert planned["status"] == "planned"
        assert planned["timeout_seconds"] == 12 * 60 * 60
        assert planned["grace_seconds"] == 30
        assert planned["output_limit_bytes"] == 4 * 1024 * 1024

        def started(pid, pgid, process_start):
            on_start(pid, pgid, process_start)
            running = json.loads(state_path.read_text())
            assert running["status"] == "running"
            assert running["pid"] == running["pgid"] == os.getpid()
            assert running["stdout_path"].endswith(f"{arm}.stdout")
            assert running["stderr_path"].endswith(f"{arm}.stderr")

        return base(command, *args, on_start=started, **kwargs)

    assert evaluate_campaign(root, instances=instances, command_runner=inspect).status == "completed"


def test_done_state_rejects_tampered_eval_artifact(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    evaluate_campaign(root, instances=instances, command_runner=_runner([]))
    path = root / "evaluation/procontract/one/one.eval.json"
    path.write_text(path.read_text() + " ")

    with pytest.raises(CampaignEvaluationError, match="artifact"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))


def test_done_state_rejects_submission_content_change(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    evaluate_campaign(root, instances=instances, command_runner=_runner([]))
    with (root / "procontract/one/one/submission.tar.gz").open("ab") as archive:
        archive.write(b"changed")

    with pytest.raises(CampaignEvaluationError, match="conflict|mutable"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))


def test_done_state_rejects_frozen_manifest_coordinate_change(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    evaluate_campaign(root, instances=instances, command_runner=_runner([]))
    manifest_path = root / "campaign.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["installs"]["procontract"]["revision"] = "changed-revision"
    _write_json(manifest_path, manifest)

    with pytest.raises(CampaignEvaluationError, match="conflict|mutable"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))


def test_done_state_rejects_scoring_metadata_change(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    evaluate_campaign(root, instances=instances, command_runner=_runner([]))
    changed = [
        {
            **instances[0],
            "branches": {"b1": {"tests": ["t1", "t2", "t3", "new-test"]}},
        }
    ]

    with pytest.raises(CampaignEvaluationError, match="conflicts"):
        evaluate_campaign(root, instances=changed, command_runner=lambda *_a, **_k: pytest.fail("must not run"))


def test_preseeded_eval_without_matching_state_is_quarantined_as_foreign(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    foreign = root / "evaluation/procontract/one/one.eval.json"
    _write_eval(foreign, statuses=("passed", "passed", "passed"))
    original = foreign.read_bytes()

    with pytest.raises(CampaignEvaluationError, match="foreign"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))

    quarantined = list((root / "evaluation-state/procontract.foreign").glob("one.*.eval.json"))
    assert not foreign.exists()
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == original


def test_foreign_submission_directory_is_quarantined_before_any_arm_launch(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    foreign = root / "evaluation/vanilla/foreign-instance"
    foreign.mkdir(parents=True)
    (foreign / "submission.tar.gz").write_bytes(b"foreign")
    calls = []

    with pytest.raises(CampaignEvaluationError, match="foreign"):
        evaluate_campaign(
            root,
            instances=instances,
            command_runner=lambda *_args, **_kwargs: calls.append("launched"),
        )

    assert calls == []
    assert not foreign.exists()
    assert (root / "evaluation-state/vanilla.foreign/foreign-instance").is_dir()


def test_executable_hash_mismatch_invalidates_eval_result(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")

    def mismatch(command, _cwd, _timeout, _grace, _limit, _environment, *, on_start):
        on_start(os.getpid(), os.getpid(), "mismatch")
        for submission in Path(command[4]).glob("*/submission.tar.gz"):
            _write_eval(
                submission.parent / f"{submission.parent.name}.eval.json",
                statuses=("passed", "failure", "failure"),
                executable_hash="f" * 64,
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    outcome = evaluate_campaign(root, instances=instances, command_runner=mismatch)
    pair = json.loads(outcome.comparison_path.read_text())["pairs"][0]

    assert outcome.status == "invalid_protocol"
    assert pair["procontract"]["errors"] == ["executable_hash_mismatch"]
    assert pair["vanilla"]["errors"] == ["executable_hash_mismatch"]


def test_done_eval_has_invocation_provenance_sidecar(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    evaluate_campaign(root, instances=instances, command_runner=_runner([]))

    for arm in ("procontract", "vanilla"):
        state = json.loads((root / f"evaluation-state/{arm}.json").read_text())
        sidecar = root / f"evaluation/{arm}/one/one.eval.provenance.json"
        payload = json.loads(sidecar.read_text())
        assert payload["invocation_hash"] == state["invocation_hash"]
        assert payload["eval_sha256"] == state["artifact_hashes"]["one.eval.json"]
        assert state["artifact_hashes"]["one.eval.provenance.json"] == hashlib.sha256(
            sidecar.read_bytes()
        ).hexdigest()


def test_done_state_rejects_local_test_blob_content_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    blob = tmp_path / "blobs/one/tests/b1.tar.gz"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"first-tests")
    monkeypatch.setenv("PROGRAMBENCH_BLOB_DIR", str(tmp_path / "blobs"))
    evaluate_campaign(root, instances=instances, command_runner=_runner([]))
    planned = json.loads((root / "evaluation-inputs.json").read_text())
    materialized = Path(planned["blob_root"]) / "one/tests/b1.tar.gz"
    materialized.chmod(0o644)
    materialized.write_bytes(b"changed-tests")

    with pytest.raises(CampaignEvaluationError, match="conflict|mutable"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))


def test_hf_main_is_resolved_once_to_immutable_commit_and_mutation_conflicts(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    calls = []

    def resolve(repository, revision):
        calls.append((repository, revision))
        return "a" * 40

    def sync(commit, instance_ids):
        blob_root = tmp_path / "hf-blobs"
        for instance_id in instance_ids:
            path = blob_root / instance_id / "tests/b1.tar.gz"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"tests")
        return blob_root

    evaluate_campaign(
        root,
        instances=instances,
        command_runner=_runner([]),
        hf_resolver=resolve,
        blob_syncer=sync,
    )
    evaluate_campaign(
        root,
        instances=instances,
        command_runner=lambda *_a, **_k: pytest.fail("done evaluator must reuse pinned commit"),
        hf_resolver=lambda *_args: pytest.fail("main must not resolve on resume"),
        blob_syncer=lambda *_args: pytest.fail("blobs must not resync on resume"),
    )
    assert len(calls) == 1
    plan_path = root / "evaluation-inputs.json"
    plan = json.loads(plan_path.read_text())
    plan["commit"] = "b" * 40
    _write_json(plan_path, plan)

    with pytest.raises(CampaignEvaluationError, match="conflict"):
        evaluate_campaign(
            root,
            instances=instances,
            command_runner=lambda *_a, **_k: pytest.fail("mutated pin must not run"),
        )


def test_blob_mutation_by_first_arm_stops_before_second_arm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    source = tmp_path / "source-blobs/one/tests/b1.tar.gz"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"tests")
    monkeypatch.setenv("PROGRAMBENCH_BLOB_DIR", str(tmp_path / "source-blobs"))
    root, instances = _campaign(tmp_path / "campaign")
    calls = []

    def mutate(command, _cwd, _timeout, _grace, _limit, environment, *, on_start):
        calls.append(Path(command[4]).name)
        on_start(os.getpid(), os.getpid(), "mutate")
        blob = Path(environment["PROGRAMBENCH_BLOB_DIR"]) / "one/tests/b1.tar.gz"
        blob.chmod(0o644)
        blob.write_bytes(b"mutated")
        for submission in Path(command[4]).glob("*/submission.tar.gz"):
            _write_eval(
                submission.parent / f"{submission.parent.name}.eval.json",
                statuses=("passed", "failure", "failure"),
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(CampaignEvaluationError, match="blob"):
        evaluate_campaign(root, instances=instances, command_runner=mutate)

    assert calls == ["procontract"]


def test_preexisting_blob_target_symlink_rejects_without_external_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    source = tmp_path / "source-blobs/one/tests/b1.tar.gz"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"tests")
    monkeypatch.setenv("PROGRAMBENCH_BLOB_DIR", str(tmp_path / "source-blobs"))
    root, instances = _campaign(tmp_path / "campaign")
    victim = tmp_path / "victim"
    victim.mkdir()
    marker = victim / "keep"
    marker.write_bytes(b"safe")
    (root / "evaluation-blobs").symlink_to(victim, target_is_directory=True)

    with pytest.raises(CampaignEvaluationError, match="foreign HuggingFace blob target"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))

    assert marker.read_bytes() == b"safe"


def test_blob_publish_recovers_crash_after_rename_before_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import campaign_evaluate

    source = tmp_path / "source-blobs/one/tests/b1.tar.gz"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"tests")
    monkeypatch.setenv("PROGRAMBENCH_BLOB_DIR", str(tmp_path / "source-blobs"))
    root, instances = _campaign(tmp_path / "campaign")
    marker = campaign_evaluate._write_blob_owner_marker
    crashed = False

    def crash_marker(*args, **kwargs):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise OSError("after rename")
        return marker(*args, **kwargs)

    monkeypatch.setattr(campaign_evaluate, "_write_blob_owner_marker", crash_marker)
    with pytest.raises(OSError, match="after rename"):
        campaign_evaluate.evaluate_campaign(root, instances=instances, command_runner=_runner([]))
    assert (root / "evaluation-blobs").is_dir()
    assert json.loads((root / "evaluation-blobs.owner.json").read_text())["status"] in {
        "ready_to_publish",
        "published",
    }

    monkeypatch.setattr(campaign_evaluate, "_write_blob_owner_marker", marker)
    outcome = campaign_evaluate.evaluate_campaign(root, instances=instances, command_runner=_runner([]))

    assert outcome.status == "completed"
    assert json.loads((root / "evaluation-blobs.owner.json").read_text())["status"] == "complete"


def test_complete_blob_marker_recovers_missing_input_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import campaign_evaluate

    source = tmp_path / "source-blobs/one/tests/b1.tar.gz"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"tests")
    monkeypatch.setenv("PROGRAMBENCH_BLOB_DIR", str(tmp_path / "source-blobs"))
    root, instances = _campaign(tmp_path / "campaign")
    atomic = campaign_evaluate._atomic_json
    crashed = False

    def crash_plan(path, value):
        nonlocal crashed
        if path.name == "evaluation-inputs.json" and not crashed:
            crashed = True
            raise OSError("after complete marker")
        return atomic(path, value)

    monkeypatch.setattr(campaign_evaluate, "_atomic_json", crash_plan)
    with pytest.raises(OSError, match="after complete marker"):
        campaign_evaluate.evaluate_campaign(root, instances=instances, command_runner=_runner([]))
    assert json.loads((root / "evaluation-blobs.owner.json").read_text())["status"] == "complete"
    assert not (root / "evaluation-inputs.json").exists()

    monkeypatch.setattr(campaign_evaluate, "_atomic_json", atomic)
    outcome = campaign_evaluate.evaluate_campaign(root, instances=instances, command_runner=_runner([]))

    assert outcome.status == "completed"
    assert json.loads((root / "evaluation-inputs.json").read_text())["commit"].startswith("local-")


def test_unjournaled_empty_blob_staging_is_recovered_but_nonempty_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import campaign_evaluate

    source = tmp_path / "source-blobs/one/tests/b1.tar.gz"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"tests")
    monkeypatch.setenv("PROGRAMBENCH_BLOB_DIR", str(tmp_path / "source-blobs"))
    root, instances = _campaign(tmp_path / "campaign")
    empty = root / ".evaluation-blobs.crashed.staging"
    empty.mkdir()

    outcome = campaign_evaluate.evaluate_campaign(root, instances=instances, command_runner=_runner([]))
    assert outcome.status == "completed"
    assert not empty.exists()


def test_hf_keyboard_interrupt_propagates_without_wrapping(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")

    with pytest.raises(KeyboardInterrupt):
        evaluate_campaign(
            root,
            instances=instances,
            command_runner=_runner([]),
            hf_resolver=lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt()),
        )


def test_done_state_rejects_invocation_configuration_conflict(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    evaluate_campaign(root, instances=instances, command_runner=_runner([]))
    state_path = root / "evaluation-state/vanilla.json"
    state = json.loads(state_path.read_text())
    state["argv"].extend(("--force",))
    _write_json(state_path, state)

    with pytest.raises(CampaignEvaluationError, match="conflicts"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))


def test_canonical_invocation_mutation_and_rehashed_forgery_both_reject(tmp_path: Path) -> None:
    from scripts import campaign_evaluate

    root, instances = _campaign(tmp_path / "campaign")
    campaign_evaluate.evaluate_campaign(root, instances=instances, command_runner=_runner([]))
    state_path = root / "evaluation-state/procontract.json"
    original = json.loads(state_path.read_text())
    mutated = json.loads(state_path.read_text())
    mutated["invocation"]["hf_commit"] = "forged"
    _write_json(state_path, mutated)

    with pytest.raises(campaign_evaluate.CampaignEvaluationError, match="invocation"):
        campaign_evaluate.evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: None)

    mutated["invocation_hash"] = hashlib.sha256(
        campaign_evaluate._canonical_bytes(mutated["invocation"])
    ).hexdigest()
    _write_json(state_path, mutated)
    with pytest.raises(campaign_evaluate.CampaignEvaluationError, match="invocation"):
        campaign_evaluate.evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: None)
    _write_json(state_path, original)


def test_done_state_rejects_output_path_configuration_conflict(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    evaluate_campaign(root, instances=instances, command_runner=_runner([]))
    state_path = root / "evaluation-state/procontract.json"
    state = json.loads(state_path.read_text())
    state["stdout_path"] = str(root / "different.stdout")
    _write_json(state_path, state)

    with pytest.raises(CampaignEvaluationError, match="conflicts"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))


def test_retry_flag_requires_an_explicit_interrupted_state(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")

    with pytest.raises(CampaignEvaluationError, match="interrupted state"):
        evaluate_campaign(root, instances=instances, retry_evaluator=True, command_runner=_runner([]))


def test_unknown_durable_evaluator_status_refuses_duplicate_launch(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    evaluate_campaign(root, instances=instances, command_runner=_runner([]))
    state_path = root / "evaluation-state/procontract.json"
    state = json.loads(state_path.read_text())
    state["status"] = "unknown"
    _write_json(state_path, state)

    with pytest.raises(CampaignEvaluationError, match="status"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))


def test_duplicate_manifest_json_key_is_a_campaign_evaluation_error(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root = tmp_path / "campaign"
    root.mkdir()
    (root / "campaign.json").write_text('{"selected_ids": [], "jobs": [], "jobs": []}')

    with pytest.raises(CampaignEvaluationError, match="malformed"):
        evaluate_campaign(root, instances=[], command_runner=lambda *_a, **_k: pytest.fail("must not run"))


def test_manifest_must_preserve_frozen_install_and_programbench_coordinates(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    manifest_path = root / "campaign.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("installs")
    _write_json(manifest_path, manifest)

    with pytest.raises(CampaignEvaluationError, match="coordinates"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))


def test_manifest_job_image_must_match_frozen_image_coordinates(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    manifest_path = root / "campaign.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["jobs"][0]["image"] = "conflicting-image"
    _write_json(manifest_path, manifest)

    with pytest.raises(CampaignEvaluationError, match="image"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))


def test_credential_value_in_published_result_blocks_evaluation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    secret = "credential-inside-result"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    result_path = root / "vanilla/one/vanilla-result.json"
    result = json.loads(result_path.read_text())
    result["error"] = secret
    _write_json(result_path, result)

    with pytest.raises(CampaignEvaluationError, match="credential leak"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))


def test_submission_symlink_is_refused_before_evaluator_launch(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    submission = root / "vanilla/one/one/submission.tar.gz"
    external = tmp_path / "external-submission.tar.gz"
    submission.replace(external)
    submission.symlink_to(external)

    with pytest.raises(CampaignEvaluationError, match="symlink"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))


def test_submission_symlinked_parent_is_refused_before_evaluator_launch(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    parent = root / "vanilla/one/one"
    external = tmp_path / "external-parent"
    parent.replace(external)
    parent.symlink_to(external, target_is_directory=True)

    with pytest.raises(CampaignEvaluationError, match="symlink"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))


def test_symlinked_evaluation_root_rejects_without_mutating_external_victim(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    victim = tmp_path / "victim"
    foreign = victim / "procontract/foreign/submission.tar.gz"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"victim-data")
    (root / "evaluation").symlink_to(victim, target_is_directory=True)

    with pytest.raises(CampaignEvaluationError, match="symlink"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))

    assert foreign.read_bytes() == b"victim-data"
    assert not (root / "evaluation-state").exists()


def test_symlinked_campaign_root_is_rejected_before_lock_or_manifest_access(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    link = tmp_path / "campaign-link"
    link.symlink_to(root, target_is_directory=True)

    with pytest.raises(CampaignEvaluationError, match="symlink"):
        evaluate_campaign(link, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))

    assert not (root / "evaluation.lock").exists()


def test_terminal_result_must_match_job_artifact_seal(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import CampaignEvaluationError, evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    result_path = root / "procontract/one/procontract-result.json"
    result = json.loads(result_path.read_text())
    result["completion"] = "challenged"
    _write_json(result_path, result)

    with pytest.raises(CampaignEvaluationError, match="artifact"):
        evaluate_campaign(root, instances=instances, command_runner=lambda *_a, **_k: pytest.fail("must not run"))


def test_malformed_trajectory_metrics_make_pair_invalid(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    result_path = root / "vanilla/one/vanilla-result.json"
    result = json.loads(result_path.read_text())
    result.pop("provider_turns")
    _write_json(result_path, result)
    state_path = root / "vanilla/one/state.json"
    state = json.loads(state_path.read_text())
    state["artifact_hashes"]["vanilla-result.json"] = hashlib.sha256(result_path.read_bytes()).hexdigest()
    _write_json(state_path, state)

    outcome = evaluate_campaign(root, instances=instances, command_runner=_runner([]))
    report = json.loads(outcome.comparison_path.read_text())

    assert outcome.status == "invalid_protocol"
    assert report["pairs"][0]["vanilla"]["errors"] == ["malformed_result_metrics"]
    assert report["pairs"][0]["delta"] is None


def test_report_freezes_evaluator_and_image_identities(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    outcome = evaluate_campaign(root, instances=instances, command_runner=_runner([]))
    coordinates = json.loads(outcome.comparison_path.read_text())["coordinates"]

    assert coordinates["images"] == {"one": {"image": "one-image"}}
    assert coordinates["evaluator"]["identity"]["programbench_version"]
    assert len(coordinates["evaluator"]["identity"]["programbench_source_sha256"]) == 64
    assert len(coordinates["evaluator"]["identity"]["uv_lock_sha256"]) == 64
    assert coordinates["evaluator"]["identity"]["uv"]["path"]
    assert len(coordinates["evaluator"]["identity"]["uv"]["sha256"]) == 64
    assert set(coordinates["evaluator"]["arms"]) == {"procontract", "vanilla"}


def test_all_missing_submissions_reuse_done_skip_states_without_calls(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(
        tmp_path / "campaign",
        missing=frozenset({("one", "procontract"), ("one", "vanilla")}),
    )
    first = evaluate_campaign(
        root,
        instances=instances,
        command_runner=lambda *_args, **_kwargs: pytest.fail("missing submissions must be skipped"),
    )
    second = evaluate_campaign(
        root,
        instances=instances,
        command_runner=lambda *_args, **_kwargs: pytest.fail("done skips must be reusable"),
    )

    assert first.status == second.status == "invalid_protocol"
    assert first.invalid_pairs == second.invalid_pairs == 1


def test_human_result_names_each_pair_and_validity(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    outcome = evaluate_campaign(root, instances=instances, command_runner=_runner([]))

    assert "one" in outcome.result_path.read_text()
    assert "valid" in outcome.result_path.read_text().lower()
    assert all(
        field in outcome.result_path.read_text()
        for field in (
            "Turns",
            "Actions",
            "Wall",
            "Cost",
            "Cache",
            "Completion",
            "Package",
            "Preflight",
            "Contract",
            "Quiet",
            "Errors",
        )
    )


def test_report_bundle_repairs_crash_between_comparison_and_markdown_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import campaign_evaluate

    root, instances = _campaign(tmp_path / "campaign")
    atomic = campaign_evaluate._atomic_bytes
    crashed = False

    def crash_result(path, value):
        nonlocal crashed
        if path.name == "RESULT.md" and not crashed:
            crashed = True
            raise OSError("publish crash")
        return atomic(path, value)

    monkeypatch.setattr(campaign_evaluate, "_atomic_bytes", crash_result)
    with pytest.raises(OSError, match="publish crash"):
        campaign_evaluate.evaluate_campaign(root, instances=instances, command_runner=_runner([]))

    assert (root / "comparison.bundle.json").is_file()
    assert (root / "comparison.publish.json").is_file()
    monkeypatch.setattr(campaign_evaluate, "_atomic_bytes", atomic)
    outcome = campaign_evaluate.evaluate_campaign(
        root,
        instances=instances,
        command_runner=lambda *_args, **_kwargs: pytest.fail("done evaluator must not rerun during repair"),
    )
    bundle = json.loads((root / "comparison.bundle.json").read_text())

    assert not (root / "comparison.publish.json").exists()
    assert hashlib.sha256(outcome.comparison_path.read_bytes()).hexdigest() == bundle["comparison_sha256"]
    assert hashlib.sha256(outcome.result_path.read_bytes()).hexdigest() == bundle["result_markdown_sha256"]


def test_failed_after_preflight_preserves_package_and_preflight_report_status(tmp_path: Path) -> None:
    from scripts.campaign_evaluate import evaluate_campaign

    root, instances = _campaign(tmp_path / "campaign")
    state_path = root / "procontract/one/state.json"
    state = json.loads(state_path.read_text())
    state["phase"] = "failed"
    state["phase_timestamps"].update({"packaged": 2, "preflighted": 2.5, "failed": 3})
    _write_json(state_path, state)

    outcome = evaluate_campaign(root, instances=instances, command_runner=_runner([]))
    procontract = json.loads(outcome.comparison_path.read_text())["pairs"][0]["procontract"]

    assert procontract["package"]
    assert procontract["preflight"]
    assert procontract["cache"]["ratio"] == {
        "decimal": "0.6",
        "denominator": 5,
        "numerator": 3,
    }


def test_terminal_resume_evaluates_without_credentials_or_adapter_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import campaign_evaluate, campaign_runner
    from scripts.campaign_usage import CircuitBreaker

    root, _ = _campaign(tmp_path / "campaign")
    _write_json(root / "breaker.json", CircuitBreaker().snapshot())
    config = object()
    plan = SimpleNamespace(
        output=root,
        config=config,
        jobs=(
            campaign_runner.CampaignJob("one", "procontract", "one-image"),
            campaign_runner.CampaignJob("one", "vanilla", "one-image"),
        ),
        dry_run=False,
        cleanup_only=False,
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_SERVER_PASSWORD", raising=False)
    monkeypatch.setattr(campaign_runner, "load_config", lambda _path: config)
    monkeypatch.setattr(campaign_runner, "load_all_instances", lambda **_kwargs: [])
    monkeypatch.setattr(campaign_runner, "plan_campaign", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(
        campaign_runner,
        "run_campaign",
        lambda *_args, **_kwargs: pytest.fail("terminal evaluation resume must not call adapters"),
    )
    monkeypatch.setattr(
        campaign_evaluate,
        "evaluate_campaign",
        lambda campaign_root, *, retry_evaluator: campaign_evaluate.CampaignEvaluationResult(
            "completed",
            1,
            0,
            campaign_root / "comparison.json",
            campaign_root / "RESULT.md",
        ),
    )

    assert (
        campaign_runner.main(
            ["--config", str(tmp_path / "campaign.yaml"), "--output", str(root), "--resume", "--evaluate"]
        )
        == 0
    )


def test_redo_with_evaluate_runs_inference_before_evaluator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import campaign_evaluate, campaign_runner
    from scripts.campaign_usage import CircuitBreaker

    root, _ = _campaign(tmp_path / "campaign")
    _write_json(root / "breaker.json", CircuitBreaker().snapshot())
    plan = SimpleNamespace(
        output=root,
        config=object(),
        jobs=(campaign_runner.CampaignJob("one", "procontract", "one-image"),),
        dry_run=False,
        cleanup_only=False,
    )
    events = []
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "test-password")
    monkeypatch.setattr(campaign_runner, "load_config", lambda _path: plan.config)
    monkeypatch.setattr(campaign_runner, "load_all_instances", lambda **_kwargs: [])
    monkeypatch.setattr(campaign_runner, "plan_campaign", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(
        campaign_runner,
        "run_campaign",
        lambda *_args, **_kwargs: (
            events.append("inference"),
            campaign_runner.CampaignResult(1, 0, (), 0, None),
        )[1],
    )
    monkeypatch.setattr(
        campaign_evaluate,
        "evaluate_campaign",
        lambda *_args, **_kwargs: (
            events.append("evaluate"),
            campaign_evaluate.CampaignEvaluationResult(
                "completed", 1, 0, root / "comparison.json", root / "RESULT.md"
            ),
        )[1],
    )

    status = campaign_runner.main(
        [
            "--config",
            str(tmp_path / "campaign.yaml"),
            "--output",
            str(root),
            "--resume",
            "--redo",
            "--evaluate",
        ]
    )

    assert status == 0
    assert events == ["inference", "evaluate"]


@pytest.mark.slow
def test_real_calculator_evaluator_scores_two_tiny_submissions_when_docker_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from programbench.utils.load_data import load_all_instances
    from scripts.campaign_evaluate import evaluate_campaign, validate_evaluation

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is unavailable")
    daemon = subprocess.run((docker, "info"), capture_output=True, timeout=10)
    if daemon.returncode:
        pytest.skip("Docker daemon is unavailable")
    instance_id = "testorg__calculator.abc1234"
    tasks = Path(__file__).parents[1] / "src/programbench/data/tasks" / instance_id
    fixture_hash = hashlib.sha256(
        (tasks / "Dockerfile").read_bytes() + (tasks / "tests/33128f6b8600/eval/run.sh").read_bytes()
    ).hexdigest()[:12]
    docker_org = f"programbench-fixture-{os.getpid()}-{fixture_hash}"
    image = f"{docker_org}/{instance_id.replace('__', '_1776_')}:task_cleanroom_v6"
    monkeypatch.setenv("PROGRAMBENCH_DOCKER_ORG", docker_org)
    try:
        subprocess.run((docker, "build", "-t", image, str(tasks)), check=True, timeout=600)

        fixtures = Path(__file__).parents[1] / "src/programbench/data/test_runs"
        submissions = {}
        executable_hashes = {}
        for arm, source in (("procontract", "correct"), ("vanilla", "incorrect")):
            archive = io.BytesIO()
            with zipfile.ZipFile(fixtures / source / instance_id / "submission.zip") as zipped:
                with tarfile.open(fileobj=archive, mode="w:gz") as tar:
                    for member in zipped.infolist():
                        payload = zipped.read(member)
                        info = tarfile.TarInfo(member.filename)
                        info.size = len(payload)
                        info.mode = member.external_attr >> 16
                        tar.addfile(info, io.BytesIO(payload))
                        if member.filename == "calculator.sh" and arm == "procontract":
                            executable_hashes[(instance_id, arm)] = hashlib.sha256(payload).hexdigest()
            submissions[(instance_id, arm)] = archive.getvalue()

        root, _ = _campaign(
            tmp_path / "campaign",
            instance_ids=(instance_id,),
            submissions=submissions,
            executable_hashes=executable_hashes,
        )
        blob_root = tmp_path / "blobs"
        branch = "33128f6b8600"
        test_tar = blob_root / instance_id / "tests" / f"{branch}.tar.gz"
        test_tar.parent.mkdir(parents=True)
        with tarfile.open(test_tar, "w:gz") as archive:
            archive.add(tasks / "tests" / branch / "eval", arcname="eval")
        monkeypatch.setenv("PROGRAMBENCH_BLOB_DIR", str(blob_root))
        instance = next(value for value in load_all_instances() if value["instance_id"] == instance_id)

        outcome = evaluate_campaign(root)
        pair = json.loads(outcome.comparison_path.read_text())["pairs"][0]

        assert outcome.status == "completed"
        assert pair["procontract"]["utility"] == {"decimal": "1", "denominator": 1, "numerator": 1}
        assert pair["vanilla"]["utility"] == {"decimal": "0", "denominator": 1, "numerator": 0}
        for arm in ("procontract", "vanilla"):
            eval_path = root / "evaluation" / arm / instance_id / f"{instance_id}.eval.json"
            validated = validate_evaluation(eval_path, instance)
            assert eval_path.is_file()
            assert validated.valid
            state = json.loads((root / f"evaluation-state/{arm}.json").read_text())
            assert state["status"] == "done"
            assert state["evaluator_images"][instance_id]["digest"].startswith("sha256:")
            assert pair[arm]["errors"] == []
    finally:
        subprocess.run((docker, "rmi", "-f", image), check=True, capture_output=True, timeout=60)
