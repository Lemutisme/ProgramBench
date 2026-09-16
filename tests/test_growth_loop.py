import json
import os

import pytest

from scripts.growth_loop import GrowthError, GrowthInfrastructureError, choose, operation, record, seal


def results(values, seed=1):
    return [{"seed": seed, "rows": [{"instance_id": name, "score": score, "status": "valid"}
                                    for name, score in values.items()]}]


def test_selection_preserves_missing_denominator_and_rejects_regression():
    block = {"instances": ["a", "b"], "seeds": [1]}
    evaluated = {
        "incumbent": results({"a": 0.5, "b": 0.8}),
        "candidate-000": results({"a": 1.0}),
        "candidate-001": results({"a": 1.0, "b": 0.7}),
        "candidate-002": results({"a": 0.7, "b": 0.9}),
    }
    generated = {name: {"status": "completed"} for name in evaluated if name != "incumbent"}
    selected = choose(block, evaluated, generated, {"min_mean_gain": 0.01, "max_task_regression": 0.0})
    assert selected["winner"] == "candidate-002"
    assert selected["comparisons"]["candidate-000"]["assigned"] == 2
    assert selected["comparisons"]["candidate-000"]["mean_missing_as_zero"] == 0.5
    assert selected["comparisons"]["candidate-000"]["decision"] == "reject-incomplete"
    assert selected["comparisons"]["candidate-001"]["decision"] == "reject-performance"


def test_no_adoption_when_incumbent_missing_or_generation_failed():
    block = {"instances": ["a"], "seeds": [1]}
    criteria = {"min_mean_gain": 0, "max_task_regression": 1}
    for baseline, status in [([], "completed"), (results({"a": 0}), "failed")]:
        selected = choose(block, {"incumbent": baseline, "candidate": results({"a": 1})},
                          {"candidate": {"status": status}}, criteria)
        assert selected["winner"] is None


def test_equal_official_means_do_not_adopt_due_to_float_order():
    baseline = results({"a": 0.3, "b": 0.2, "c": 0.1})
    candidate = results({"a": 0.1, "b": 0.2, "c": 0.3})
    for group in (baseline, candidate):
        for row in group[0]["rows"]:
            row.update(resolved=round(row["score"] * 10), total=10)
    selected = choose({"instances": ["a", "b", "c"], "seeds": [1]},
                      {"incumbent": baseline, "candidate": candidate}, {"candidate": {"status": "completed"}},
                      {"min_mean_gain": 0, "max_task_regression": 1})
    assert selected["winner"] is None
    assert selected["comparisons"]["candidate"]["exact_mean_gain"] == {"numerator": 0, "denominator": 1}


@pytest.mark.parametrize("score", [float("nan"), float("inf"), 1.1, -0.1, True])
def test_invalid_score_cannot_adopt(score):
    with pytest.raises(GrowthError):
        choose({"instances": ["a"], "seeds": [1]},
               {"incumbent": results({"a": 0}), "candidate": results({"a": score})},
               {"candidate": {"status": "completed"}}, {"min_mean_gain": 0, "max_task_regression": 1})


def test_completed_operation_is_idempotent_and_inflight_is_not_reissued(tmp_path):
    calls = []

    def perform():
        calls.append(1)
        return {"status": "completed", "value": 2}

    assert operation(tmp_path / "done", "generation", {"binding": "rho"}, perform)["value"] == 2
    assert operation(tmp_path / "done", "generation", {"binding": "rho"}, perform)["value"] == 2
    assert calls == [1]
    with pytest.raises(GrowthError):
        operation(tmp_path / "done", "generation", {"binding": "substitution"}, perform)
    record(tmp_path / "inflight" / "started.json", {"kind": "generation"})
    with pytest.raises(GrowthError, match="in-flight"):
        operation(tmp_path / "inflight", "generation", {"binding": "rho"}, perform)
    assert calls == [1]


def test_failure_retains_usage_and_never_becomes_zero_cost(tmp_path):
    record(tmp_path / "usage.json", {"known_cost": "0.1", "unknown_cost_messages": 2})

    def fail():
        raise RuntimeError("provider secret should not be copied into result")

    result = operation(tmp_path, "generation", {}, fail)
    assert result["status"] == "failed"
    assert result["known_cost"] is None
    assert result["retained_usage"]["unknown_cost_messages"] == 2
    assert "provider secret" not in (tmp_path / "failure.json").read_text()


def test_infrastructure_failure_closes_accounting_and_stops_fresh_and_cached_execution(tmp_path):
    record(tmp_path / "accounting.json", {"gateway": {"admitted": 0}, "known_cost": None})
    calls = []

    def fail():
        calls.append("import")
        raise GrowthInfrastructureError("source import failed")

    for _ in range(2):
        with pytest.raises(GrowthInfrastructureError):
            operation(tmp_path, "generation", {}, fail)
            calls.append("later generation or evaluation")
    assert calls == ["import"]
    result = json.loads((tmp_path / "result.json").read_bytes())
    assert result == json.loads((tmp_path / "failure.json").read_bytes())
    assert result["status"] == "failed" and result["failure_classification"] == "infrastructure"
    assert result["retained_accounting"]["gateway"]["admitted"] == 0
    assert result["known_cost"] is None and result["usage_incomplete"] is True
    assert "source import failed" in (tmp_path / "failure-traceback.txt").read_text()


def test_immutable_record_cannot_be_replaced(tmp_path):
    seal(tmp_path / "record", b"first")
    seal(tmp_path / "record", b"first")
    with pytest.raises(GrowthError):
        seal(tmp_path / "record", b"second")


def test_cancelled_native_generation_does_not_admit(tmp_path):
    from scripts.growth_native import native_generation

    with pytest.raises(GrowthError, match="before native admission"):
        native_generation(object(), tmp_path, {}, {}, None, cancelled=lambda: True)
    assert (tmp_path / "issue.json").is_file()
    assert not (tmp_path / "admission.json").exists()


def test_closed_feedback_retains_rejected_policy_and_separates_d_from_p(tmp_path):
    from scripts.growth_native import closed_feedback
    from scripts.growth_loop import policy

    artifact = policy(tmp_path, b"Rejected but informative policy.\n")
    record(tmp_path / "g0/decision.json", {
        "winner": None,
        "generation_results": {"candidate-000": {"status": "completed", "artifacts": {"solver_policy": artifact}}},
        "screen": {"comparisons": {"candidate-000": {"mean": 0.8, "rows": ["private row excluded"]}}},
        "comparisons": {"candidate-000": {"mean": 0.6, "rows": ["private row excluded"]}},
        "confirmation_consumed": True,
    })
    study = {"development_instances": ["d"], "rounds": [{"id": "g0", "confirmation_instances": ["p0"]},
                                                            {"id": "g1", "confirmation_instances": ["unopened-p1"]}]}
    feedback = closed_feedback(study, tmp_path, 1)
    previous = feedback["generations"][0]
    assert previous["development_screen"]["results"]["candidate-000"]["mean"] == 0.8
    assert previous["consumed_confirmation"]["results"]["candidate-000"]["mean"] == 0.6
    assert previous["consumed_confirmation"]["fresh_for_future_use"] is False
    assert previous["proposals_including_rejected"]["candidate-000"]["artifacts"]["solver_policy"]["text"].startswith("Rejected")
    assert "unopened-p1" not in json.dumps(feedback)
    assert "private row excluded" not in json.dumps(feedback)


def test_generator_and_real_gateway_share_one_exact_deadline(tmp_path):
    from scripts.campaign_provider_gateway import Gateway
    from scripts.growth_loop import policy, sha256
    from scripts.growth_native import generator_payload

    artifact = policy(tmp_path, b"A policy\n")
    specification = {"binding": {"solver_policy": artifact, "generator_policy": artifact}, "mutable_objects": ["solver_policy"]}
    started = 1234567.9999
    payload = generator_payload(tmp_path, specification, "Produce policy", "context", "openai/gpt-5.6-luna", "max", started_at=started)
    issue = tmp_path / "procontract-issue.json"
    record(issue, {"payload": payload, "startedAt": started})
    gateway = Gateway(tmp_path / "channel", tmp_path / "control", "gpt-5.6-luna", "max",
                      upstream="http://127.0.0.1:9/v1", api_key="scripted-only", issue=issue)
    try:
        assert gateway.deadline() == (int(started * 1000) + 21600000) / 1000
        identity, deadline = gateway.db.execute("SELECT identity,deadline FROM execution_budget").fetchone()
        assert identity == sha256(issue.read_bytes()) and deadline == payload["budget"]["deadline"] / 1000
        issue.chmod(0o600)
        changed = json.loads(issue.read_bytes())
        changed["startedAt"] += 1
        changed["payload"]["budget"]["deadline"] += 1000
        issue.write_text(json.dumps(changed))
        with pytest.raises(ValueError, match="contract_budget_conflict"):
            gateway.deadline()
        assert gateway.db.execute("SELECT COUNT(*) FROM request").fetchone()[0] == 0
    finally:
        gateway.server_close()
        gateway.db.close()
        os.close(gateway.directory_fd)
