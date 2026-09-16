import copy
from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys

import mpmath
import pytest

from scripts.growth_final_analysis import InputError, analyze, student_t_critical, student_t_sf


def table():
    tasks = [f"synthetic-task-{i:03d}" for i in range(200)]
    return {"schema_version": 1, "attempt_index": 1, "task_ids": tasks,
            "rows": [{"task_id": task, "arm": arm, "repeat": repeat, "closed": True,
                      "status": "valid", "score": Decimal("0.5") if arm == "baseline"
                      else Decimal("0.53") if i % 2 else Decimal("0.55")}
                     for i, task in enumerate(tasks) for arm in ("baseline", "candidate")
                     for repeat in (1, 2)]}


def test_full_design_uses_task_clusters_and_exact_task_mean():
    result = analyze(table())
    assert result["assigned"] == result["closed"] == 800
    assert result["means"] == {"baseline": 0.5, "candidate": 0.54}
    assert result["gain_exact"] == "1/25"
    assert result["primary"]["n_task_clusters"] == 200
    assert result["primary"]["degrees_of_freedom"] == 199
    assert result["primary"]["standard_error"] == pytest.approx(0.01 / math.sqrt(199), rel=1e-14)
    assert result["numeric_criteria_met"] is True
    assert result["formal_acceptance_authority"] is False
    assert result["performance_adopted"] is False


def test_exact_two_percentage_points_does_not_pass_float_roundoff():
    data = table()
    for row in data["rows"]:
        if row["arm"] == "candidate":
            row["score"] = 0.51 if int(row["task_id"][-3:]) % 2 else 0.53
    result = analyze(data)
    assert result["gain_exact"] == "1/50"
    assert result["primary"]["positive_mean_supported"] is True
    assert result["observed_gain_strictly_above_2pp"] is False
    assert result["numeric_criteria_met"] is False


def test_repeats_are_aggregated_and_negative_repeat_is_not_an_extra_gate():
    data = table()
    for row in data["rows"]:
        if row["arm"] == "candidate":
            row["score"] += Decimal("0.08") if row["repeat"] == 1 else Decimal("-0.08")
    result = analyze(data)
    assert result["repeat_gains"] == [0.12, -0.04]
    assert result["gain_exact"] == "1/25"
    assert result["primary"]["standard_error"] == pytest.approx(0.01 / math.sqrt(199), rel=1e-14)
    assert result["numeric_criteria_met"] is True


def test_regression_diagnostics_do_not_create_an_adoption_condition():
    data = table()
    for row in data["rows"]:
        if row["arm"] == "candidate":
            row["score"] = Decimal("0.4") if int(row["task_id"][-3:]) < 20 else Decimal("0.6")
    result = analyze(data)
    assert result["task_direction_counts"] == {"negative": 20, "positive": 180, "zero": 0}
    assert result["gain"] == 0.08
    assert result["numeric_criteria_met"] is True


def test_variance_zero_is_inconclusive_even_with_large_gain():
    data = table()
    for row in data["rows"]:
        row["score"] = 0 if row["arm"] == "baseline" else 1
    result = analyze(data)
    assert result["all_assigned_scores_valid"] is True
    assert result["analysis_status"] == "inconclusive_zero_standard_error"
    assert result["gain"] == 1
    assert result["numeric_criteria_met"] is False
    assert result["primary"] is None


def test_same_scores_in_both_arms_and_reverse_direction():
    data = table()
    for row in data["rows"]:
        row["arm"] = "candidate" if row["arm"] == "baseline" else "baseline"
    reverse = analyze(data)
    assert reverse["gain"] == -0.04
    assert reverse["numeric_criteria_met"] is False
    assert float(reverse["primary"]["p_value_decimal"]) > 0.99
    for row in data["rows"]:
        row["score"] = 0.5
    assert analyze(data)["analysis_status"] == "inconclusive_zero_standard_error"


@pytest.mark.parametrize("status", ["invalid", "cancelled", "not_started"])
def test_closed_unknown_rows_are_kept_and_not_filled_with_zero(status):
    data = table()
    data["rows"][0] |= {"status": status, "score": None}
    result = analyze(data)
    assert result["analysis_status"] == "incomplete_official_scores"
    assert result["status_counts"][status] == 1
    assert result["status_counts"]["valid"] == 799
    assert result["missing_score_gain_bounds"] == [0.03875, 0.04125]
    assert result["numeric_criteria_met"] is False
    assert result["primary"] is None
    assert "means" not in result


def test_missing_and_open_assignments_do_not_expose_partial_means():
    data = table()
    data["rows"].pop()
    result = analyze(data)
    assert result["missing_rows"] == 1
    assert result["analysis_status"] == "awaiting_all_original_assignments_to_close"
    assert "gain" not in result and "means" not in result
    data = table()
    data["rows"][0] |= {"status": "pending", "score": None, "closed": False}
    result = analyze(data)
    assert result["closed"] == 799
    assert "gain" not in result and "missing_score_gain_bounds" not in result


def test_valid_official_zero_is_not_missing():
    data = table()
    data["rows"][0]["score"] = 0
    result = analyze(data)
    assert result["all_assigned_scores_valid"] is True
    assert result["status_counts"] == {"valid": 800}
    assert result["means"]["baseline"] == 0.49875


@pytest.mark.parametrize("value", [True, None, "0.5", -0.001, 1.001, float("nan"), float("inf"),
                                  Decimal("NaN"), Decimal("Infinity"), {}, []])
def test_invalid_score_payloads_fail_closed(value):
    data = table()
    data["rows"][0]["score"] = value
    with pytest.raises(InputError):
        analyze(data)


@pytest.mark.parametrize("mutation", [
    lambda d: d["rows"].append(copy.deepcopy(d["rows"][0])),
    lambda d: d["task_ids"].pop(),
    lambda d: d["task_ids"].__setitem__(0, d["task_ids"][1]),
    lambda d: d["rows"][0].__setitem__("task_id", "unassigned"),
    lambda d: d["rows"][0].__setitem__("repeat", True),
    lambda d: d["rows"][0].__setitem__("arm", "incumbent"),
    lambda d: d["rows"][0].__setitem__("closed", 1),
    lambda d: d["rows"][0].__setitem__("closed", False),
    lambda d: d["rows"][0].__setitem__("status", "invalid"),
    lambda d: d["rows"][0].__setitem__("status", "pending"),
    lambda d: d["rows"][0].__setitem__("hidden_test_counts", 50),
    lambda d: d.__setitem__("attempt_index", 0),
    lambda d: d.__setitem__("attempt_index", True),
    lambda d: d.__setitem__("schema_version", True),
    lambda d: d.__setitem__("score_filter", "drop_invalid"),
])
def test_assignment_and_schema_changes_are_rejected(mutation):
    data = table()
    mutation(data)
    with pytest.raises(InputError):
        analyze(data)


def test_task_order_does_not_change_mean_or_uncertainty():
    data = table()
    first = analyze(data)
    data["task_ids"].reverse()
    data["rows"].reverse()
    second = analyze(data)
    assert first["primary"] == second["primary"]
    assert first["gain_exact"] == second["gain_exact"]


def test_alpha_spending_is_exact_and_does_not_reset_with_table():
    values = []
    for index in (1, 2, 3):
        data = table()
        data["attempt_index"] = index
        result = analyze(data)
        values.append(Fraction(result["alpha_exact"]))
    assert values == [Fraction(1, 40), Fraction(1, 120), Fraction(1, 240)]
    assert sum(Fraction(1, 20 * j * (j + 1)) for j in range(1, 1001)) == Fraction(50, 1001)


@pytest.mark.parametrize("degrees", [1, 2, 199])
@pytest.mark.parametrize("t", ["0", "0.3", "2", "-1.5", "5"])
def test_beta_tail_matches_independent_density_integration(degrees, t):
    # The reference uses a normalized Student density integral, not incomplete beta.
    context = mpmath.mp.clone()
    context.dps = 60
    value = context.mpf(t)
    normalizer = context.gamma(context.mpf(degrees + 1) / 2) / (
        context.sqrt(degrees * context.pi) * context.gamma(context.mpf(degrees) / 2))
    reference = context.quad(lambda x: normalizer * (1 + x*x/degrees) ** (-context.mpf(degrees+1)/2),
                             [value, context.inf])
    assert context.fabs(student_t_sf(context, value, degrees) - reference) < context.mpf("1e-45")


@pytest.mark.parametrize("alpha", ["0.025", "0.008333333333333333333333333333", "0.00000001"])
def test_critical_value_cauchy_has_closed_form_reference(alpha):
    context = mpmath.mp.clone()
    context.dps = 80
    tail = context.mpf(alpha)
    actual = student_t_critical(context, tail, 1)
    expected = 1 / context.tan(context.pi * tail)
    assert context.fabs(actual / expected - 1) < context.mpf("1e-60")


def test_df199_critical_value_and_hoeffding_margin():
    result = analyze(table())
    assert float(result["primary"]["critical_t_decimal"]) == pytest.approx(1.97195654425175, abs=5e-14)
    assert result["gain"] - result["secondary_hoeffding_lower_bound"] == pytest.approx(0.19206455826398414)


def test_independent_180_digit_near_critical_counterexample_stays_inconclusive():
    data = json.loads((Path(__file__).parent / 'fixtures/final-analysis/near-critical.json').read_bytes(), parse_float=Decimal)
    result = analyze(data)
    assert result['observed_gain_strictly_above_2pp'] is True
    assert result['analysis_status'] == 'inconclusive_numerical_boundary'
    assert result['numeric_criteria_met'] is False
    assert result['primary']['positive_mean_supported'] is False
    # Shift only the effect, keeping the task variance fixed, to the stronger L>2pp boundary.
    with localcontext() as context:
        context.prec = 600
        for row in data['rows']:
            if row['arm'] == 'candidate':
                row['score'] += Decimal('0.02')
    result = analyze(data)
    assert result['numeric_criteria_met'] is True
    assert result['primary']['positive_mean_supported'] is True
    assert result['primary']['mean_gain_above_2pp_supported'] is False


def test_tiny_nonzero_variance_is_serializable_without_false_display_zero(tmp_path):
    source = Path(__file__).parent / 'fixtures/final-analysis/tiny-variance.json'
    destination = tmp_path / 'tiny-output.json'
    executed = subprocess.run([sys.executable, str(Path(__file__).parents[1] / 'scripts/growth_final_analysis.py'),
                               '--input', str(source), '--output', str(destination)],
                              env={'PATH': os.defpath, 'HOME': str(tmp_path), 'PYTHONDONTWRITEBYTECODE': '1'},
                              capture_output=True, timeout=60)
    assert executed.returncode == 0, executed.stderr.decode()
    result = json.loads(destination.read_bytes())
    assert result['primary']['t'] is None
    assert result['primary']['standard_error'] is None
    assert Decimal(result['primary']['t_decimal']).is_finite()
    assert Decimal(result['primary']['standard_error_decimal']) > 0
    assert result['numeric_criteria_met'] is True


def test_cli_preserves_lexical_score_precision_binds_input_and_will_not_overwrite(tmp_path):
    data = table()
    for row in data["rows"]:
        row["score"] = float(row["score"])
    source = tmp_path / "synthetic.json"
    destination = tmp_path / "analysis.json"
    source.write_text(json.dumps(data))
    command = [sys.executable, str(Path(__file__).parents[1] / "scripts/growth_final_analysis.py"), "--input", str(source),
               "--output", str(destination)]
    environment = {"PATH": os.defpath, "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    first = subprocess.run(command, env=environment, capture_output=True, timeout=60)
    assert first.returncode == 0, first.stderr.decode()
    original = destination.read_bytes()
    result = json.loads(original)
    assert result["input_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert result["gain_exact"] == "1/25"
    second = subprocess.run(command, env=environment, capture_output=True, timeout=60)
    assert second.returncode != 0
    assert destination.read_bytes() == original


@pytest.mark.parametrize("payload", ['{"schema_version":1,"schema_version":1}', '{"score":NaN}'])
def test_cli_rejects_ambiguous_json_without_output(tmp_path, payload):
    source = tmp_path / "bad.json"
    destination = tmp_path / "analysis.json"
    source.write_text(payload)
    result = subprocess.run([sys.executable, str(Path(__file__).parents[1] / "scripts/growth_final_analysis.py"), "--input", str(source),
                             "--output", str(destination)],
                            env={"PATH": os.defpath, "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"},
                            capture_output=True, timeout=30)
    assert result.returncode != 0
    assert not destination.exists()


def official_counts_table():
    data = table()
    for row in data["rows"]:
        row["score"] = {"resolved": 1, "total": 3} if row["arm"] == "baseline" else {
            "resolved": 103 if int(row["task_id"][-3:]) % 2 else 109, "total": 300,
        }
    return data


def test_official_count_ratios_match_existing_development_gate():
    from scripts.growth_loop import measure

    data = official_counts_table()
    evaluations = {
        name: [{"seed": repeat, "rows": [
            {"instance_id": row["task_id"], "status": row["status"], **row["score"],
             "score": float(Fraction(row["score"]["resolved"], row["score"]["total"]))}
            for row in data["rows"] if row["arm"] == arm and row["repeat"] == repeat
        ]} for repeat in (1, 2)]
        for name, arm in (("incumbent", "baseline"), ("candidate-000", "candidate"))
    }
    _, exact = measure({"instances": data["task_ids"], "seeds": [1, 2]}, evaluations, evaluations)
    gain = (sum(exact["candidate-000"]) - sum(exact["incumbent"])) / 400
    result = analyze(data)
    assert Fraction(result["gain_exact"]) == gain == Fraction(1, 50)
    assert result["primary"]["positive_mean_supported"] is True
    assert result["numeric_criteria_met"] is False


def test_nonterminating_count_ratio_prevents_decimal_rounding_false_positive():
    data = official_counts_table()
    exact = analyze(data)
    for row in data["rows"]:
        row["score"] = float(Fraction(row["score"]["resolved"], row["score"]["total"]))
    rounded = analyze(data)
    # Numbers alone faithfully retain their lexical values, which already lost
    # the count identity. Future official extraction must use the ratio form.
    assert Fraction(rounded["gain_exact"]) > Fraction(1, 50)
    assert rounded["numeric_criteria_met"] is True
    assert exact["gain_exact"] == "1/50"
    assert exact["observed_gain_strictly_above_2pp"] is False


@pytest.mark.parametrize("difference", [-1, 0, 1])
def test_exact_counts_keep_sub_float_effect_boundary(difference):
    data = official_counts_table()
    scale = 10 ** 60
    for row in data["rows"]:
        if row["arm"] == "candidate":
            row["score"] = {"resolved": row["score"]["resolved"] * scale + difference,
                            "total": row["score"]["total"] * scale}
    result = analyze(data)
    assert Fraction(result["gain_exact"]) == Fraction(1, 50) + Fraction(difference, 300 * scale)
    assert result["numeric_criteria_met"] is (difference > 0)


@pytest.mark.parametrize("counts", [
    {"resolved": True, "total": 3}, {"resolved": 1, "total": True},
    {"resolved": 1, "total": 0}, {"resolved": 0, "total": -1},
    {"resolved": -1, "total": 3}, {"resolved": 4, "total": 3},
    {"resolved": "1", "total": 3}, {"resolved": 1.0, "total": 3},
    {"resolved": 1, "total": 3.0}, {"resolved": 1},
    {"resolved": 1, "total": 3, "score": 0.9},
    {"numerator": 1, "denominator": 3},
])
def test_malformed_official_counts_fail_closed(counts):
    data = official_counts_table()
    data["rows"][0]["score"] = counts
    with pytest.raises(InputError):
        analyze(data)


def test_count_form_preserves_valid_zero_and_does_not_fill_invalid():
    data = official_counts_table()
    data["rows"][0]["score"] = {"resolved": 0, "total": 3}
    assert analyze(data)["status_counts"] == {"valid": 800}
    data["rows"][0]["status"] = "invalid"
    with pytest.raises(InputError):
        analyze(data)
    data["rows"][0]["score"] = None
    result = analyze(data)
    assert result["status_counts"] == {"valid": 799, "invalid": 1}
    assert result["analysis_status"] == "incomplete_official_scores"
    assert "gain" not in result


def test_cli_accepts_exact_counts_without_rounding(tmp_path):
    source = tmp_path / "exact-counts.json"
    destination = tmp_path / "exact-analysis.json"
    source.write_text(json.dumps(official_counts_table()))
    command = [sys.executable, str(Path(__file__).parents[1] / "scripts/growth_final_analysis.py"),
               "--input", str(source), "--output", str(destination)]
    result = subprocess.run(command, env={"PATH": os.defpath, "HOME": str(tmp_path),
                                         "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode()
    decoded = json.loads(destination.read_bytes())
    assert decoded["gain_exact"] == "1/50"
    assert decoded["numeric_criteria_met"] is False
