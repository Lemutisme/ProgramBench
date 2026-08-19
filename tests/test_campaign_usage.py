from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from email.utils import format_datetime
from datetime import datetime, timezone

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
        "type": "assistant",
        "tokens": {
            "input": input,
            "output": output,
            "reasoning": reasoning,
            "cache": {"read": read, "write": write},
        },
        "cost": Decimal(str(cost)),
        "content": "ok",
    }


def projected(*messages: dict) -> dict:
    return {"data": list(messages)}


def test_summarize_usage_counts_projected_assistant_turns_exactly() -> None:
    messages = projected(
        {"type": "user", "content": "hello"},
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


def test_json_cost_preserves_long_decimal_and_rejects_string_cost() -> None:
    payload = b'{"data":[{"type":"assistant","cost":0.123456789012345678901234567890,"tokens":{"input":1,"output":2,"reasoning":0,"cache":{"read":0,"write":0}},"content":"ok"}]}'
    assert summarize_usage(payload).cost == Decimal("0.123456789012345678901234567890")
    with pytest.raises(UsageParseError):
        summarize_usage({"data": [{"type": "assistant", "cost": "0.1", "tokens": {}, "content": "ok"}]})


def test_known_flat_and_upstream_message_schemas_reject_mixed_shapes() -> None:
    flat = {
        "type": "assistant",
        "cost": 0.1,
        "tokens": {"input": 1, "output": 2, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        "content": "ok",
    }
    upstream = {
        "info": {
            "role": "assistant",
            "cost": 0.2,
            "tokens": {"input": 1, "output": 2, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        },
        "parts": [],
    }
    assert summarize_usage({"data": [flat, upstream]}).provider_turns == 2
    for invalid in (
        {**flat, "role": "assistant"},
        {"info": upstream["info"], "parts": [], "type": "assistant"},
        {"type": "assistant", "cost": 0.1, "tokens": {}, "content": None},
    ):
        with pytest.raises(UsageParseError):
            summarize_usage({"data": [invalid]})


def test_hostctl_like_104_turn_usage_has_103_cache_hits() -> None:
    messages = []
    for index in range(104):
        messages.append(
            {
                "type": "assistant",
                "cost": 0.000001,
                "tokens": {
                    "input": 100 if index == 0 else 10,
                    "output": 5,
                    "reasoning": 1,
                    "cache": {"read": 0 if index == 0 else 90, "write": 100 if index == 0 else 0},
                },
                "content": "ok",
            }
        )
    result = summarize_usage({"data": messages})
    assert result.provider_turns == 104
    assert result.turns_with_cache == 103
    assert result.turns_without_cache == 1
    assert result.cache_read == 9270
    assert result.cache_write == 100
    assert result.cost == Decimal("0.000104")
    assert result.cache_read_ratio == Decimal(9270) / Decimal(10500)


def test_newer_generation_replaces_snapshot_and_terminal_trajectory_once() -> None:
    breaker = CircuitBreaker()
    unhealthy = usage(turns=20, read=0, cost="0.1")
    corrected = usage(turns=20, read=4000, cost="0.2")
    breaker.observe("job-1", 1, unhealthy)
    breaker.observe("job-1", 2, unhealthy)
    assert breaker.consecutive_cache_failures == 1
    assert not breaker.paused
    breaker.observe("job-2", 1, unhealthy)
    assert breaker.paused
    breaker.observe("job-1", 3, corrected)
    assert breaker.campaign_usage.cost == Decimal("6.0")
    assert breaker.consecutive_cache_failures == 1
    with pytest.raises(StaleObservationError):
        breaker.observe("job-1", 2, corrected)


def test_rate_limit_deadline_only_extends_and_parses_bounds_and_dates() -> None:
    now = [100.0]
    breaker = CircuitBreaker(clock=lambda: now[0], retry_after_default=7, retry_after_max=20)
    breaker.observe_error(429, retry_after="15")
    breaker.observe_error(429, retry_after="3")
    assert breaker.backoff_until == 115
    breaker.observe_error(429, retry_after="999")
    assert breaker.backoff_until == 120
    now[0] = 110
    breaker.observe_error(429, retry_after="bad")
    assert breaker.backoff_until == 120
    now[0] = 100
    date = format_datetime(datetime.fromtimestamp(112, timezone.utc), usegmt=True)
    breaker.observe_error(429, retry_after=date)
    assert breaker.backoff_until == 120


def test_auth_acknowledgement_recovers_but_hard_cap_acknowledgement_does_not() -> None:
    auth = CircuitBreaker()
    auth.observe_error(401)
    auth.acknowledge_auth()
    assert auth.admission_allowed
    auth.observe_error(403)
    auth.acknowledge_auth()
    assert auth.admission_allowed
    hard = CircuitBreaker(hard_cost_cap="1")
    hard.observe("job", 1, usage(turns=1, cost="1"))
    hard.acknowledge_hard_cap()
    hard.acknowledge()
    assert hard.hard_stopped
    assert hard.paused


def test_snapshot_rejects_underreported_derived_totals_and_job_schema() -> None:
    breaker = CircuitBreaker()
    breaker.observe("job", 1, usage(turns=1, cost="0.1"))
    snapshot = breaker.snapshot()
    snapshot["campaign_usage"]["cost"] = "0"
    with pytest.raises(UsageParseError):
        CircuitBreaker.from_snapshot(snapshot)


def test_fractional_backoff_round_trips_and_fresh_http_date_is_exact() -> None:
    now = [100.25]
    breaker = CircuitBreaker(clock=lambda: now[0], retry_after_max=100)
    breaker.observe_error(429, retry_after="1.5")
    restored = CircuitBreaker.from_snapshot(json.dumps(breaker.snapshot()), clock=lambda: now[0])
    assert restored.backoff_until == pytest.approx(101.75)
    fresh = CircuitBreaker(clock=lambda: 100.25, retry_after_max=100)
    fresh.observe_error(429, retry_after=format_datetime(datetime.fromtimestamp(103.25, timezone.utc), usegmt=True))
    assert fresh.backoff_until == pytest.approx(103.0)


def test_persistence_rejects_corrupt_cache_failures_and_job_turn_aggregates() -> None:
    breaker = CircuitBreaker()
    breaker.observe("job", 1, usage(turns=1, cost="0.1"))
    corrupt = breaker.snapshot()
    corrupt["cache_failures"] = 999
    with pytest.raises(UsageParseError):
        CircuitBreaker.from_snapshot(corrupt)
    corrupt = breaker.snapshot()
    corrupt["jobs"]["job"]["usage"]["input"] = 999
    with pytest.raises(UsageParseError):
        CircuitBreaker.from_snapshot(corrupt)


def test_exact_decimal_sum_spans_exponents_and_cost_cap() -> None:
    result = summarize_usage(
        b'{"data":[{"type":"assistant","cost":1e100,"tokens":{"input":1,"output":0,"reasoning":0,"cache":{"read":0,"write":0}},"content":"ok"},{"type":"assistant","cost":0.1,"tokens":{"input":1,"output":0,"reasoning":0,"cache":{"read":0,"write":0}},"content":"ok"}]}'
    )
    assert result.cost == Decimal(
        "10000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000.1"
    )
    breaker = CircuitBreaker(hard_cost_cap=Decimal("1e100"))
    breaker.observe("job", 1, result)
    assert breaker.hard_stopped


def test_projected_cost_missing_or_null_is_zero_but_string_is_invalid() -> None:
    base = {
        "type": "assistant",
        "tokens": {"input": 1, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        "content": "ok",
    }
    assert summarize_usage({"data": [{**base}, {**base, "cost": None}]}).cost == 0
    info = {"role": "assistant", "tokens": base["tokens"]}
    assert (
        summarize_usage({"data": [{"info": info, "parts": []}, {"info": {**info, "cost": None}, "parts": []}]}).cost
        == 0
    )
    with pytest.raises(UsageParseError):
        summarize_usage({"data": [{**base, "cost": "0"}]})


def test_snapshot_requires_strict_version_and_nonempty_job_ids() -> None:
    breaker = CircuitBreaker()
    breaker.observe("job", 1, usage(turns=1))
    snapshot = breaker.snapshot()
    snapshot["version"] = True
    with pytest.raises(UsageParseError):
        CircuitBreaker.from_snapshot(snapshot)
    snapshot = breaker.snapshot()
    snapshot["jobs"][""] = snapshot["jobs"].pop("job")
    with pytest.raises(UsageParseError):
        CircuitBreaker.from_snapshot(snapshot)


def test_newer_generation_lower_usage_replaces_and_decreases_totals() -> None:
    breaker = CircuitBreaker()
    higher = usage(turns=2, cost="0.2")
    lower = usage(turns=1, cost="0.1")
    breaker.observe("job", 1, higher)
    breaker.observe("job", 2, lower)
    assert breaker.campaign_usage.provider_turns == 1
    assert breaker.campaign_usage.cost == Decimal("0.1")


def test_zero_turn_job_snapshot_round_trips_but_nonzero_empty_aggregate_rejects() -> None:
    breaker = CircuitBreaker()
    breaker.observe("empty", 1, UsageSummary())
    restored = CircuitBreaker.from_snapshot(breaker.snapshot())
    assert restored.campaign_usage == UsageSummary()
    corrupt = breaker.snapshot()
    corrupt["jobs"]["empty"]["usage"]["input"] = 1
    with pytest.raises(UsageParseError):
        CircuitBreaker.from_snapshot(corrupt)


def test_snapshot_rejects_pause_causes_without_caps_and_impossible_derived_causes() -> None:
    breaker = CircuitBreaker()
    breaker.observe("job", 1, usage(turns=1, cost="0"))
    for cause in ("soft", "hard"):
        corrupt = breaker.snapshot()
        corrupt["pause_causes"] = [cause]
        with pytest.raises(UsageParseError):
            CircuitBreaker.from_snapshot(corrupt)
    capped = CircuitBreaker(soft_cost_cap="1")
    capped.observe("job", 1, usage(turns=1, cost="0"))
    corrupt = capped.snapshot()
    corrupt["pause_causes"] = ["soft"]
    with pytest.raises(UsageParseError):
        CircuitBreaker.from_snapshot(corrupt)


def test_signed_zero_projected_cost_is_canonicalized() -> None:
    usage_result = summarize_usage(
        b'{"data":[{"type":"assistant","cost":-0.0,"tokens":{"input":0,"output":0,"reasoning":0,"cache":{"read":0,"write":0}},"content":"ok"}]}'
    )
    assert usage_result.cost == Decimal(0)
    assert usage_result.turns[0].cost == Decimal(0)
    assert usage_result.to_dict()["cost"] == "0"
    assert usage_result.to_dict()["turns"][0]["cost"] == "0"


def test_upstream_info_rejects_flat_type_discriminator() -> None:
    info = {
        "role": "assistant",
        "type": "assistant",
        "cost": 0,
        "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
    }
    with pytest.raises(UsageParseError):
        summarize_usage({"data": [{"info": info, "parts": []}]})


def test_acknowledged_recoverable_pause_round_trips_without_derived_drift() -> None:
    breaker = CircuitBreaker(soft_cost_cap="1")
    breaker.observe("job", 1, usage(turns=1, cost="1"))
    breaker.acknowledge_soft_cap()
    restored = CircuitBreaker.from_snapshot(breaker.snapshot())
    assert restored.admission_allowed
    assert restored.campaign_usage == breaker.campaign_usage
    snapshot = breaker.snapshot()
    snapshot["jobs"]["job"]["generation"] = True
    with pytest.raises(UsageParseError):
        CircuitBreaker.from_snapshot(snapshot)
