from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from scripts.campaign_usage import (
    CircuitBreaker,
    StaleObservationError,
    UsageParseError,
    UsageSummary,
    summarize_usage,
)


def assistant(
    *,
    input: object = 0,
    output: object = 0,
    reasoning: object = 0,
    read: object = 0,
    write: object = 0,
    cost: object = 0,
) -> dict:
    return {
        "role": "assistant",
        "tokens": {
            "input": input,
            "output": output,
            "reasoning": reasoning,
            "cache": {"read": read, "write": write},
        },
        "cost": cost,
    }


def projected(*messages: dict) -> dict:
    return {"data": list(messages)}


def test_summarize_usage_counts_projected_assistant_turns_exactly() -> None:
    messages = projected(
        {"role": "user", "parts": []},
        assistant(input=1000, output=50, reasoning=20, cost="0.001"),
        assistant(input=100, output=40, reasoning=10, read=1900, cost="0.0005"),
    )

    usage = summarize_usage(messages)

    assert usage == UsageSummary(
        provider_turns=2,
        input=1100,
        output=90,
        reasoning=30,
        cache_read=1900,
        cache_write=0,
        prompt_total=3000,
        turns_with_cache=1,
        turns_without_cache=1,
        max_prompt_total=2000,
        cost=Decimal("0.0015"),
        telemetry_gap=True,
        turns=usage.turns,
    )
    assert usage.cache_read_ratio == Decimal(1900) / Decimal(3000)
    assert messages["data"][1]["tokens"]["input"] == 1000


@pytest.mark.parametrize(
    "payload",
    [
        projected(assistant(input=True)),
        projected(assistant(input=float("nan"))),
        projected(assistant(input=-1)),
        {"data": [{"role": "assistant", "tokens": []}]},
    ],
)
def test_summarize_usage_rejects_invalid_token_values(payload: dict) -> None:
    with pytest.raises(UsageParseError):
        summarize_usage(payload)


def test_loading_bytes_rejects_duplicate_json_keys_and_nonfinite_constants() -> None:
    with pytest.raises(UsageParseError, match="duplicate"):
        summarize_usage(b'{"data":[{"role":"assistant","role":"user"}]}')
    with pytest.raises(UsageParseError):
        summarize_usage(b'{"data":[{"role":"assistant","tokens":{"input":NaN}}]}')


def usage(*, turns: int = 20, read: int = 0, cost: str = "0") -> UsageSummary:
    return summarize_usage(projected(*(assistant(input=100, read=read, cost=cost) for _ in range(turns))))


def test_breaker_pauses_after_two_unhealthy_terminal_trajectories_and_healthy_resets() -> None:
    breaker = CircuitBreaker(soft_cost_cap="10", hard_cost_cap="20")
    breaker.observe("job-1", 1, usage(turns=20, read=0))
    assert not breaker.paused
    breaker.observe("job-2", 1, usage(turns=20, read=0))
    assert breaker.paused
    breaker.observe("job-3", 1, usage(turns=20, read=2000))
    assert breaker.paused
    breaker.acknowledge_cache()
    assert breaker.consecutive_cache_failures == 0


def test_breaker_auth_rate_limit_and_exact_cost_caps() -> None:
    now = [100.0]
    breaker = CircuitBreaker(soft_cost_cap="1.00", hard_cost_cap="2.00", clock=lambda: now[0])
    breaker.observe("soft", 1, usage(turns=1, cost="1.00"))
    assert breaker.paused
    breaker.acknowledge_soft_cap()
    breaker.observe_error(429, retry_after="5")
    assert not breaker.admission_allowed
    now[0] = 105
    assert breaker.admission_allowed
    breaker.observe_error(401, detail="secret-token-must-not-leak")
    assert breaker.paused
    assert "secret" not in breaker.pause_reason
    assert breaker.hard_stopped


def test_breaker_hard_cap_requests_active_work_stop_at_exact_crossing() -> None:
    breaker = CircuitBreaker(soft_cost_cap="1", hard_cost_cap="2")
    breaker.observe("job", 1, usage(turns=1, cost="2"))
    assert breaker.hard_stopped
    assert breaker.paused


def test_breaker_observation_generations_are_idempotent_and_delta_based() -> None:
    breaker = CircuitBreaker()
    first = usage(turns=1, cost="0.1")
    second = summarize_usage(projected(assistant(input=100, cost="0.1"), assistant(input=100, cost="0.2")))
    breaker.observe("job", 1, first)
    breaker.observe("job", 1, first)
    breaker.observe("job", 2, second)
    assert breaker.campaign_usage.cost == Decimal("0.3")
    with pytest.raises(StaleObservationError):
        breaker.observe("job", 1, second)


def test_breaker_concurrent_observations_do_not_lose_campaign_totals() -> None:
    breaker = CircuitBreaker()
    values = [usage(turns=1, cost="0.01") for _ in range(20)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda item: breaker.observe(item[0], 1, item[1]), ((str(i), value) for i, value in enumerate(values))
            )
        )
    assert breaker.campaign_usage.provider_turns == 20
    assert breaker.campaign_usage.cost == Decimal("0.20")


def test_breaker_snapshot_is_strictly_reloadable_and_rejects_malformed_state() -> None:
    breaker = CircuitBreaker(soft_cost_cap="10", hard_cost_cap="20")
    breaker.observe("job", 1, usage(turns=2, cost="0.25"))
    encoded = json.dumps(breaker.snapshot(), sort_keys=True)
    restored = CircuitBreaker.from_snapshot(encoded)
    assert restored.snapshot() == breaker.snapshot()
    with pytest.raises(UsageParseError):
        CircuitBreaker.from_snapshot('{"version":1,"version":1}')
    with pytest.raises(UsageParseError):
        CircuitBreaker.from_snapshot('{"version":true}')
