"""Numerical component of the prospective final confirmation; grants no adoption.

Accept only an externally assembled scalar table. This module does not authenticate
official scores, register attempts, verify revisions, or validate independence.
Do not feed it a live/frozen study's partial results. Tests use synthetic data only.
"""

import argparse
from collections import Counter
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path

import mpmath


class InputError(ValueError):
    pass


def analyze(data):
    require_keys(data, {"schema_version", "task_ids", "attempt_index", "rows"})
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise InputError("unsupported schema version")
    tasks = data["task_ids"]
    if (not isinstance(tasks, list) or len(tasks) != 200
            or any(not isinstance(task, str) or not task for task in tasks)
            or len(set(tasks)) != 200):
        raise InputError("expected exactly 200 distinct prospectively registered task IDs")
    attempt = data["attempt_index"]
    if type(attempt) is not int or attempt < 1:
        raise InputError("attempt index must be a positive integer from the external ledger")
    if not isinstance(data["rows"], list):
        raise InputError("rows must be an array")
    expected = {(task, arm, repeat) for task in tasks
                for arm in ("baseline", "candidate") for repeat in (1, 2)}
    rows = {}
    for row in data["rows"]:
        require_keys(row, {"task_id", "arm", "repeat", "closed", "status", "score"})
        if (not isinstance(row["task_id"], str) or not isinstance(row["arm"], str)
                or type(row["repeat"]) is not int or type(row["closed"]) is not bool):
            raise InputError("malformed assignment identity or closure flag")
        key = (row["task_id"], row["arm"], row["repeat"])
        if key not in expected or key in rows:
            raise InputError("duplicate or unassigned row")
        if row["status"] not in ("valid", "invalid", "cancelled", "not_started", "pending"):
            raise InputError("unknown scalar status")
        if row["status"] == "pending" and row["closed"]:
            raise InputError("pending assignments cannot be closed")
        if row["status"] == "valid" and not row["closed"]:
            raise InputError("valid scores must be closed")
        value = score(row["score"]) if row["status"] == "valid" else None
        if row["status"] != "valid" and row["score"] is not None:
            raise InputError("a non-valid assignment cannot supply a score")
        rows[key] = {"closed": row["closed"], "status": row["status"], "score": value}
    alpha = Fraction(1, 20 * attempt * (attempt + 1))
    result = {
        "schema_version": 1,
        "scope": "numerical component only; input provenance and inference assumptions are external",
        "formal_acceptance_authority": False,
        "performance_adopted": False,
        "attempt_index": attempt,
        "alpha_exact": str(alpha),
        "assigned": len(expected),
        "provided": len(rows),
        "closed": sum(row["closed"] for row in rows.values()),
        "missing_rows": len(expected - rows.keys()),
        "status_counts": dict(sorted(Counter(row["status"] for row in rows.values()).items())),
        "all_assigned_scores_valid": False,
        "numeric_criteria_met": False,
        "primary": None,
    }
    # Do not expose means from early results, even if all currently present rows pass.
    if len(rows) != len(expected) or not all(row["closed"] for row in rows.values()):
        return result | {"analysis_status": "awaiting_all_original_assignments_to_close"}
    if any(row["status"] != "valid" for row in rows.values()):
        # Bounds describe missing information, never substitute an official score.
        low = Fraction(0)
        high = Fraction(0)
        for key, row in rows.items():
            value = row["score"]
            if key[1] == "candidate":
                low += value if value is not None else 0
                high += value if value is not None else 1
            if key[1] == "baseline":
                low -= value if value is not None else 1
                high -= value if value is not None else 0
        return result | {"analysis_status": "incomplete_official_scores",
                         "missing_score_gain_bounds": [float(low / 400), float(high / 400)]}
    differences = {
        task: [(rows[(task, "candidate", repeat)]["score"]
                - rows[(task, "baseline", repeat)]["score"]) for repeat in (1, 2)]
        for task in tasks
    }
    task_means = {task: sum(values) / 2 for task, values in differences.items()}
    gain = sum(task_means.values()) / 200
    variance = sum((value - gain) ** 2 for value in task_means.values()) / (199 * 200)
    means = {arm: sum(row["score"] for key, row in rows.items() if key[1] == arm) / 400
             for arm in ("baseline", "candidate")}
    result |= {
        "all_assigned_scores_valid": True,
        "means": {arm: float(value) for arm, value in means.items()},
        "gain": float(gain),
        "gain_exact": str(gain),
        "observed_gain_strictly_above_2pp": gain > Fraction(1, 50),
        "tasks": [{"task_id": task, "repeat_differences": [float(x) for x in differences[task]],
                   "mean_difference": float(task_means[task])} for task in tasks],
        "repeat_gains": [float(sum(values[i] for values in differences.values()) / 200) for i in (0, 1)],
        "task_direction_counts": {"positive": sum(x > 0 for x in task_means.values()),
                                  "zero": sum(x == 0 for x in task_means.values()),
                                  "negative": sum(x < 0 for x in task_means.values())},
        "leave_one_task_out_gain_range": [float(min((gain * 200 - x) / 199 for x in task_means.values())),
                                           float(max((gain * 200 - x) / 199 for x in task_means.values()))],
    }
    if variance == 0:
        return result | {"analysis_status": "inconclusive_zero_standard_error"}
    context = mpmath.mp.clone()
    context.dps = 80
    estimate = context.mpf(gain.numerator) / gain.denominator
    se = context.sqrt(context.mpf(variance.numerator) / variance.denominator)
    tail = context.mpf(alpha.numerator) / alpha.denominator
    critical = student_t_critical(context, tail, 199)
    statistic = estimate / se
    lower = estimate - critical * se
    p_value = student_t_sf(context, statistic, 199)
    if not all(context.isfinite(x) for x in (statistic, lower, p_value, critical)):
        return result | {"analysis_status": "inconclusive_nonfinite_inference"}
    # Treat values below our qualified numerical resolution as inconclusive.
    # This guard is deliberately wider than the quantile bracket; it is not a
    # certified interval bound on mpmath or an additional scientific effect gate.
    resolution = context.mpf("1e-50") * max(context.mpf(1), abs(estimate), abs(critical * se))
    boundary = abs(lower) <= resolution
    return result | {
        "analysis_status": "inconclusive_numerical_boundary" if boundary else "computed_approximate_inference",
        "numeric_criteria_met": gain > Fraction(1, 50) and lower > resolution,
        "primary": {
            "method": "approximate task-cluster paired Student t; one-sided",
            "n_task_clusters": 200,
            "degrees_of_freedom": 199,
            "standard_error": display_number(se),
            "standard_error_decimal": context.nstr(se, 30),
            "t": display_number(statistic),
            "t_decimal": context.nstr(statistic, 30),
            "p_value_decimal": context.nstr(p_value, 30),
            "alpha_decimal": context.nstr(tail, 30),
            "critical_t_decimal": context.nstr(critical, 30),
            "lower_bound": display_number(lower),
            "lower_bound_decimal": context.nstr(lower, 30),
            "numerical_resolution_decimal": context.nstr(resolution, 30),
            "numerical_resolution_is_certified_error_bound": False,
            "positive_mean_supported": lower > resolution,
            "mean_gain_above_2pp_supported": lower - context.mpf(1) / 50 > resolution,
        },
        "secondary_hoeffding_lower_bound": float(estimate - context.sqrt(2 * context.log(1 / tail) / 200)),
        "claim_limit": "A numeric pass is conditional on external provenance/execution/assumption checks. "
                       "Observed gain >2pp and evidence of a positive mean need not prove true gain >2pp. "
                       "This approximate test does not establish strict infinite-horizon error control, "
                       "generalization beyond the fixed benchmark, or stronger Improver ability.",
    }


def require_keys(value, expected):
    if not isinstance(value, dict) or set(value) != expected:
        raise InputError("unexpected scalar schema keys")


def score(value):
    # Trusted score extraction can preserve the canonical integer ratio. A JSON
    # float cannot represent every official count ratio at a strict gain boundary.
    if isinstance(value, dict):
        require_keys(value, {"resolved", "total"})
        if (type(value["resolved"]) is not int or type(value["total"]) is not int
                or value["total"] <= 0 or not 0 <= value["resolved"] <= value["total"]):
            raise InputError("official score counts must be integers with 0 <= resolved <= total and total > 0")
        return Fraction(value["resolved"], value["total"])
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise InputError("valid scores must be finite official numbers or exact resolved/total counts")
    if isinstance(value, Decimal) and not value.is_finite():
        raise InputError("nonfinite score")
    if isinstance(value, float) and not math.isfinite(value):
        raise InputError("nonfinite score")
    number = Fraction(str(value))
    if not 0 <= number <= 1:
        raise InputError("score outside [0,1]")
    return number


def student_t_sf(context, value, degrees):
    """Stable positive-tail Student t survival probability via incomplete beta."""
    half_tail = context.betainc(context.mpf(degrees) / 2, context.mpf(1) / 2,
                               0, degrees / (degrees + value * value), regularized=True) / 2
    return half_tail if value >= 0 else 1 - half_tail


def student_t_critical(context, alpha, degrees):
    if not 0 < alpha < context.mpf(1) / 2 or degrees < 1:
        raise InputError("invalid Student t quantile coordinate")
    low = context.mpf(0)
    high = context.mpf(1)
    while student_t_sf(context, high, degrees) > alpha:
        high *= 2
    for _ in range(220):
        middle = (low + high) / 2
        if student_t_sf(context, middle, degrees) > alpha:
            low = middle
        else:
            high = middle
    return high


def display_number(value):
    number = float(value)
    return number if math.isfinite(number) and (number != 0 or value == 0) else None


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("duplicate JSON object key")
        result[key] = value
    return result


def reject_constant(_):
    raise InputError("nonfinite JSON constant")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    payload = args.input.read_bytes()
    data = json.loads(payload, parse_float=Decimal, parse_constant=reject_constant, object_pairs_hook=unique_object)
    result = analyze(data) | {"input_sha256": hashlib.sha256(payload).hexdigest(),
                              "analyzer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                              "mpmath_version": mpmath.__version__}
    serialized = json.dumps(result, allow_nan=False, indent=2, sort_keys=True) + "\n"
    with args.output.open("x") as stream:
        stream.write(serialized)


if __name__ == "__main__":
    main()
