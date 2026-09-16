"""Real local result/SQLite accounting; no model, network provider or grader."""

import sqlite3

import pytest

from scripts.growth_compare import inventory, run
from scripts.growth_loop import read, record
from scripts.growth_native import native_accounting
from tests import test_growth_compare

initial = test_growth_compare.initial
candidate = test_growth_compare.candidate
study = test_growth_compare.study
comparison = test_growth_compare.comparison


def ledger(root, gap=None):
    path = root / "workspace-state/provider-control/requests.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE request (peer_pid INTEGER, finished REAL, usage TEXT, sent REAL, terminal_at REAL)")
        db.execute("INSERT INTO request VALUES (1,?,?,1,1)",
                   (None if gap == "open" else 1, None if gap == "missing" else '{"input_tokens":1}'))
    if gap == "unreadable":
        path.write_bytes(b"broken SQLite")


@pytest.mark.parametrize("content", ["{broken", "[]", "null"])
def test_malformed_native_result_preserves_gateway_and_unknown_cost(tmp_path, content):
    (tmp_path / "procontract-result.json").write_text(content)
    ledger(tmp_path, "missing")
    result = native_accounting(tmp_path)
    assert result["known_cost"] is None
    assert result["usage_incomplete"] is True
    assert result["native_usage_incomplete"] is None
    assert result["gateway"]["missing_usage"] == 1
    assert result["accounting_source"] == "unavailable-native-result"
    assert result["accounting_error_type"]


@pytest.mark.parametrize("gap", [None, "open", "missing", "unreadable"])
@pytest.mark.parametrize("cost", ["0", "0.25"])
def test_gateway_evidence_qualifies_native_usage_without_adding_cost(tmp_path, gap, cost):
    record(tmp_path / "procontract-result.json", {"cost": cost, "usage_incomplete": False})
    ledger(tmp_path, gap)
    result = native_accounting(tmp_path)
    assert result["known_cost"] == cost
    assert result["native_usage_incomplete"] is False
    assert result["usage_incomplete"] is (gap is not None)


def test_missing_native_cost_is_unknown_even_with_gateway_requests(tmp_path):
    ledger(tmp_path, "missing")
    result = native_accounting(tmp_path)
    assert result["known_cost"] is None
    assert result["usage_incomplete"] is True


@pytest.mark.parametrize("gap", [{"open": 1}, {"missing_usage": 1}, {"unreadable": True}])
def test_retained_accounting_cannot_override_confirmed_gateway_gap(tmp_path, gap):
    slot = {"stage": "development", "subject": "incumbent", "seed": 7, "path": "slot",
            "specification_sha256": "a" * 64, "instances": ["toy"]}
    output = tmp_path / "slot"
    record(output / "started.json", {})
    record(output / "result.json", {"rows": [{"instance_id": "toy", "status": "valid", "score": 0.5}]})
    record(output / "accounting.json", {"rows": [{"instance_id": "toy", "known_cost": "0.25",
        "usage_incomplete": False, "gateway": {"present": True, "admitted": 1, **gap}}]})
    result = inventory(tmp_path, [slot])
    assert result["known_cost"] == "0.25"
    assert result["usage_incomplete"] is True
    assert result["rows"][0]["accounting"]["usage_incomplete"] is False
    assert result["rows"][0]["score_status"] == "valid"


@pytest.mark.parametrize("cost,total", [("0", "0"), ("0.25", "1.00"), (None, None)])
def test_completed_comparison_retains_unknown_usage_and_separate_valid_scores(comparison, cost, total):
    specification, path, root = comparison

    class GatewayGap(test_growth_compare.ToyBackend):
        def evaluate(self, output, spec):
            result = super().evaluate(output, spec)
            record(output / "accounting.json", {"rows": [{"instance_id": task, "known_cost": cost,
                "usage_incomplete": False, "gateway": {"present": True, "admitted": 1,
                    "open": 0, "missing_usage": 1}} for task in spec["instances"]]})
            return result

    result = run(path, root, GatewayGap(specification, root))
    completed = read(root / "completed.json")
    assert completed == result
    assert result["winner"] == "candidate-000"
    assert result["accounting"]["known_cost"] == total
    assert result["accounting"]["usage_incomplete"] is True
    assert result["accounting"]["unknown_cost_slots"] == (4 if cost is None else 0)
    assert all(row["usage_incomplete"] for row in result["accounting"]["rows"])
