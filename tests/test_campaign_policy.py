import pytest

from scripts.campaign_policy import CAPABILITY_CONSERVATION, POLICIES, TaskTopology, policy, select_policy


@pytest.mark.parametrize(
    ("topology", "expected"),
    [
        (TaskTopology("massive", "reactive", "syntax", "exact"), "breadth"),
        (TaskTopology("narrow", "reactive", "text", "semantic"), "reactive"),
        (TaskTopology("broad", "persistent", "text", "semantic"), "interface"),
        (TaskTopology("broad", "stateless", "text", "exact"), "interface"),
        (TaskTopology("narrow", "stateless", "syntax", "semantic"), "representation"),
        (TaskTopology("narrow", "stateless", "text", "semantic"), "behavioral"),
    ],
)
def test_selects_regime_from_topology_without_task_identity(topology: TaskTopology, expected: str) -> None:
    assert select_policy(topology) == expected


def test_policies_are_concise_and_keep_settlement_terms_out() -> None:
    assert set(POLICIES) == {"none", "behavioral", "interface", "representation", "reactive", "breadth"}
    for name, text in POLICIES.items():
        assert len(text) <= 1_200
        assert "Contract" not in text
        assert (not text) == (name == "none")
        if name != "none":
            assert text.startswith(CAPABILITY_CONSERVATION)


def test_rejects_unknown_policy() -> None:
    with pytest.raises(ValueError, match="unknown execution policy"):
        policy("task-name-special-case")
