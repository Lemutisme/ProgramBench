"""Exact OpenCode projected-message usage accounting and campaign admission gates."""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from datetime import timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Callable, Mapping


class UsageParseError(ValueError):
    """Raised when projected usage or persisted breaker state is invalid."""


class StaleObservationError(UsageParseError):
    """Raised when a job reports an older or regressive usage generation."""


@dataclass(frozen=True)
class TurnUsage:
    input: int = 0
    output: int = 0
    reasoning: int = 0
    cache_read: int = 0
    cache_write: int = 0
    prompt_total: int = 0
    cost: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        for name in ("input", "output", "reasoning", "cache_read", "cache_write", "prompt_total"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise UsageParseError(f"{name} must be a nonnegative integer")
        if self.prompt_total != self.input + self.cache_read + self.cache_write:
            raise UsageParseError("turn prompt total is inconsistent with token totals")
        if not isinstance(self.cost, Decimal) or not self.cost.is_finite() or self.cost < 0:
            raise UsageParseError("turn cost must be a nonnegative finite decimal")
        if self.cost.is_zero():
            object.__setattr__(self, "cost", Decimal(0))

    @property
    def cache_hit(self) -> bool:
        return self.cache_read > 0


@dataclass(frozen=True)
class UsageSummary:
    provider_turns: int = 0
    input: int = 0
    output: int = 0
    reasoning: int = 0
    cache_read: int = 0
    cache_write: int = 0
    prompt_total: int = 0
    turns_with_cache: int = 0
    turns_without_cache: int = 0
    max_prompt_total: int = 0
    cost: Decimal = Decimal(0)
    telemetry_gap: bool = False
    turns: tuple[TurnUsage, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "provider_turns",
            "input",
            "output",
            "reasoning",
            "cache_read",
            "cache_write",
            "prompt_total",
            "turns_with_cache",
            "turns_without_cache",
            "max_prompt_total",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise UsageParseError(f"{name} must be a nonnegative integer")
        if not isinstance(self.cost, Decimal) or not self.cost.is_finite() or self.cost < 0:
            raise UsageParseError("cost must be a nonnegative finite decimal")
        if self.cost.is_zero():
            object.__setattr__(self, "cost", Decimal(0))
        if type(self.telemetry_gap) is not bool or not isinstance(self.turns, tuple):
            raise UsageParseError("invalid usage summary shape")
        if any(not isinstance(turn, TurnUsage) for turn in self.turns):
            raise UsageParseError("turn records must be immutable TurnUsage values")
        expected = _summary_fields(self.turns)
        actual = (
            self.provider_turns,
            self.input,
            self.output,
            self.reasoning,
            self.cache_read,
            self.cache_write,
            self.prompt_total,
            self.turns_with_cache,
            self.turns_without_cache,
            self.max_prompt_total,
            self.cost,
            self.telemetry_gap,
        )
        if actual != expected:
            raise UsageParseError("usage summary does not match per-turn records")

    @property
    def prompt_tokens(self) -> int:
        """Compatibility name used by earlier campaign runner drafts."""
        return self.prompt_total

    @property
    def max_prompt(self) -> int:
        return self.max_prompt_total

    @property
    def max_prompt_tokens(self) -> int:
        return self.max_prompt_total

    @property
    def cache_read_ratio(self) -> Decimal:
        if self.prompt_total == 0:
            return Decimal(0)
        return Decimal(self.cache_read) / Decimal(self.prompt_total)

    def to_dict(self) -> dict[str, object]:
        return {
            "provider_turns": self.provider_turns,
            "input": self.input,
            "output": self.output,
            "reasoning": self.reasoning,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "prompt_total": self.prompt_total,
            "turns_with_cache": self.turns_with_cache,
            "turns_without_cache": self.turns_without_cache,
            "max_prompt_total": self.max_prompt_total,
            "cost": _decimal_string(self.cost),
            "telemetry_gap": self.telemetry_gap,
            "turns": [
                {
                    "input": turn.input,
                    "output": turn.output,
                    "reasoning": turn.reasoning,
                    "cache_read": turn.cache_read,
                    "cache_write": turn.cache_write,
                    "prompt_total": turn.prompt_total,
                    "cost": _decimal_string(turn.cost),
                }
                for turn in self.turns
            ],
        }


UsageTurn = TurnUsage


def summarize_usage(messages: Mapping[str, object] | bytes | str) -> UsageSummary:
    """Aggregate assistant usage from an OpenCode projected ``{"data": [...]}`` response."""
    payload = _load_json(messages)
    if not isinstance(payload, Mapping) or set(payload) != {"data"} or not isinstance(payload["data"], list):
        raise UsageParseError("projected messages must contain only a data list")

    turns: list[TurnUsage] = []
    for message in payload["data"]:
        turn = _projected_turn(message)
        if turn is not None:
            turns.append(turn)

    return UsageSummary(
        provider_turns=len(turns),
        input=sum(turn.input for turn in turns),
        output=sum(turn.output for turn in turns),
        reasoning=sum(turn.reasoning for turn in turns),
        cache_read=sum(turn.cache_read for turn in turns),
        cache_write=sum(turn.cache_write for turn in turns),
        prompt_total=sum(turn.prompt_total for turn in turns),
        turns_with_cache=sum(turn.cache_hit for turn in turns),
        turns_without_cache=sum(not turn.cache_hit for turn in turns),
        max_prompt_total=max((turn.prompt_total for turn in turns), default=0),
        cost=_sum_decimals(turn.cost for turn in turns),
        telemetry_gap=any(turn.cache_read > 0 and turn.cache_write == 0 for turn in turns),
        turns=tuple(turns),
    )


def _projected_turn(message: object) -> TurnUsage | None:
    if not isinstance(message, Mapping):
        raise UsageParseError("projected message must be an object")
    if "info" in message:
        if (
            set(message) - {"info", "parts"}
            or not isinstance(message.get("info"), Mapping)
            or not isinstance(message.get("parts"), list)
        ):
            raise UsageParseError("mixed or malformed upstream message schema")
        source = message["info"]
        if {"type", "content"} & set(source):
            raise UsageParseError("upstream info contains flat-only fields")
        role = source.get("role")
        if role == "user":
            return None
        if role != "assistant":
            raise UsageParseError("upstream message role must be assistant or user")
        return _turn_from_source(source)
    if "role" in message or "parts" in message:
        raise UsageParseError("mixed projected message schema")
    message_type = message.get("type")
    if message_type == "user":
        if not isinstance(message.get("content"), str):
            raise UsageParseError("flat user message requires content")
        return None
    if message_type != "assistant" or not {"type", "tokens", "content"} <= set(message):
        raise UsageParseError("flat assistant message requires type, cost, tokens, and content")
    if not isinstance(message["content"], str):
        raise UsageParseError("flat assistant content must be a string")
    return _turn_from_source(message)


def _turn_from_source(source: Mapping[str, object]) -> TurnUsage:
    tokens = source.get("tokens")
    if not isinstance(tokens, Mapping):
        raise UsageParseError("assistant tokens must be an object")
    cache = tokens.get("cache")
    if not isinstance(cache, Mapping):
        raise UsageParseError("assistant cache tokens must be an object")
    input_tokens = _token(tokens.get("input"))
    output_tokens = _token(tokens.get("output"))
    reasoning_tokens = _token(tokens.get("reasoning"))
    cache_read = _token(cache.get("read"))
    cache_write = _token(cache.get("write"))
    cost = _usage_cost(source.get("cost"))
    prompt_total = input_tokens + cache_read + cache_write
    return TurnUsage(
        input=input_tokens,
        output=output_tokens,
        reasoning=reasoning_tokens,
        cache_read=cache_read,
        cache_write=cache_write,
        prompt_total=prompt_total,
        cost=cost,
    )


class CircuitBreaker:
    """Thread-safe campaign-wide cache, provider-error, and cost admission state."""

    def __init__(
        self,
        cache_warmup_turns: int = 10,
        minimum_cache_ratio: float | Decimal = 0.80,
        consecutive_failures: int = 2,
        soft_cost_cap: int | float | str | Decimal | None = None,
        hard_cost_cap: int | float | str | Decimal | None = None,
        *,
        clock: Callable[[], float] = time.time,
        minimum_read_ratio: float | Decimal | None = None,
        stop_after_consecutive_failures: int | None = None,
        total_cost_soft: int | float | str | Decimal | None = None,
        total_cost_hard: int | float | str | Decimal | None = None,
        retry_after_default: int = 30,
        retry_after_max: int = 3600,
    ) -> None:
        if minimum_read_ratio is not None:
            minimum_cache_ratio = minimum_read_ratio
        if stop_after_consecutive_failures is not None:
            consecutive_failures = stop_after_consecutive_failures
        if total_cost_soft is not None:
            soft_cost_cap = total_cost_soft
        if total_cost_hard is not None:
            hard_cost_cap = total_cost_hard
        if type(cache_warmup_turns) is not int or cache_warmup_turns < 0:
            raise UsageParseError("cache warmup turns must be a nonnegative integer")
        if isinstance(minimum_cache_ratio, bool) or not isinstance(minimum_cache_ratio, (int, float, Decimal)):
            raise UsageParseError("minimum cache ratio must be numeric")
        ratio = float(minimum_cache_ratio)
        if not math.isfinite(ratio) or not 0 <= ratio <= 1:
            raise UsageParseError("minimum cache ratio must be finite and between zero and one")
        if type(consecutive_failures) is not int or consecutive_failures <= 0:
            raise UsageParseError("consecutive failures must be a positive integer")
        if type(retry_after_default) is not int or retry_after_default < 0:
            raise UsageParseError("retry-after default must be nonnegative")
        if type(retry_after_max) is not int or retry_after_max < retry_after_default:
            raise UsageParseError("retry-after maximum is invalid")
        soft = None if soft_cost_cap is None else _decimal(soft_cost_cap)
        hard = None if hard_cost_cap is None else _decimal(hard_cost_cap)
        if soft is not None and soft < 0 or hard is not None and hard < 0:
            raise UsageParseError("cost caps must be nonnegative")
        if soft is not None and hard is not None and soft > hard:
            raise UsageParseError("soft cost cap must not exceed hard cost cap")
        self.cache_warmup_turns = cache_warmup_turns
        self.minimum_cache_ratio = Decimal(str(ratio))
        self.consecutive_failures = consecutive_failures
        self.soft_cost_cap = soft
        self.hard_cost_cap = hard
        self._clock = clock
        self._retry_after_default = retry_after_default
        self._retry_after_max = retry_after_max
        self._lock = threading.RLock()
        self._campaign_usage = UsageSummary()
        self._jobs: dict[str, tuple[int, UsageSummary, bool]] = {}
        self._terminal_sequence: list[str] = []
        self._cache_failures = 0
        self._pause_causes: set[str] = set()
        self._cache_acknowledged = False
        self._soft_acknowledged = False
        self._backoff_until: float | None = None

    @classmethod
    def from_campaign_config(cls, config: object, **kwargs: object) -> CircuitBreaker:
        limits = config.limits
        cache = config.cache
        return cls(
            cache_warmup_turns=cache.warmup_turns,
            minimum_cache_ratio=cache.minimum_read_ratio,
            consecutive_failures=cache.stop_after_consecutive_failures,
            soft_cost_cap=limits.total_cost_soft,
            hard_cost_cap=limits.total_cost_hard,
            **kwargs,
        )

    @property
    def campaign_usage(self) -> UsageSummary:
        with self._lock:
            return self._campaign_usage

    @property
    def total_usage(self) -> UsageSummary:
        return self.campaign_usage

    @property
    def total_cost(self) -> Decimal:
        return self.campaign_usage.cost

    @property
    def consecutive_cache_failures(self) -> int:
        with self._lock:
            return self._cache_failures

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._is_paused()

    @property
    def hard_stopped(self) -> bool:
        with self._lock:
            return "hard" in self._pause_causes or "auth" in self._pause_causes

    @property
    def stop_active(self) -> bool:
        return self.hard_stopped

    @property
    def admission_allowed(self) -> bool:
        with self._lock:
            return not self._is_paused()

    @property
    def backoff_until(self) -> float | None:
        with self._lock:
            self._release_backoff()
            return self._backoff_until

    @property
    def pause_reason(self) -> str | None:
        with self._lock:
            if "auth" in self._pause_causes:
                return "provider authentication failure"
            if "hard" in self._pause_causes:
                return "campaign hard cost cap reached"
            if "soft" in self._pause_causes:
                return "campaign soft cost cap reached"
            if "cache" in self._pause_causes:
                return "cache health below campaign threshold"
            if self._backoff_until is not None and self._clock() < self._backoff_until:
                return "provider rate limit backoff"
            return None

    def observe(
        self,
        observation: str | UsageSummary | Mapping[str, object],
        generation: int | UsageSummary | Mapping[str, object] | None = None,
        usage: UsageSummary | Mapping[str, object] | None = None,
        *,
        job_id: str | None = None,
        terminal: bool = True,
    ) -> None:
        if isinstance(observation, str):
            if usage is None or not isinstance(generation, int):
                raise UsageParseError("job observations require generation and usage")
            resolved_job = observation
            resolved_generation = generation
            resolved_usage = _coerce_summary(usage)
        else:
            resolved_usage = _coerce_summary(observation)
            resolved_job = job_id or f"__trajectory_{id(resolved_usage)}"
            if generation is None:
                resolved_generation = 0
            elif isinstance(generation, int):
                resolved_generation = generation
            else:
                raise UsageParseError("observation generation must be an integer")
        if (
            not resolved_job
            or not isinstance(resolved_job, str)
            or type(resolved_generation) is not int
            or resolved_generation < 0
        ):
            raise UsageParseError("job ID and generation are invalid")
        if type(terminal) is not bool:
            raise UsageParseError("terminal must be boolean")
        with self._lock:
            previous = self._jobs.get(resolved_job)
            if previous is not None and resolved_generation < previous[0]:
                raise StaleObservationError("stale usage generation")
            if previous is not None and resolved_generation == previous[0]:
                if resolved_usage != previous[1] or terminal != previous[2]:
                    raise StaleObservationError("usage changed without a newer generation")
                return
            was_terminal = previous[2] if previous is not None else False
            is_terminal = was_terminal or terminal
            self._jobs[resolved_job] = (resolved_generation, resolved_usage, is_terminal)
            if is_terminal and not was_terminal:
                self._terminal_sequence.append(resolved_job)
                self._cache_acknowledged = False
            self._recompute_usage()
            self._apply_cost_caps()
            self._recompute_cache()

    def _recompute_usage(self) -> None:
        total = UsageSummary()
        for job in sorted(self._jobs):
            summary = self._jobs[job][1]
            total = _add(total, summary)
        self._campaign_usage = total

    def _recompute_cache(self) -> None:
        failures = 0
        for job in self._terminal_sequence:
            summary = self._jobs[job][1]
            if summary.provider_turns < self.cache_warmup_turns:
                continue
            if summary.cache_read_ratio < self.minimum_cache_ratio:
                failures += 1
            else:
                failures = 0
        self._cache_failures = failures
        if failures >= self.consecutive_failures and not self._cache_acknowledged:
            self._pause_causes.add("cache")

    def observe_error(
        self,
        status_code: int,
        *,
        retry_after: object = None,
        now: float | None = None,
        detail: object = None,
    ) -> None:
        if type(status_code) is not int:
            raise UsageParseError("provider status must be an integer")
        current = self._clock() if now is None else _finite_time(now)
        with self._lock:
            if status_code in (401, 403):
                self._pause_causes.add("auth")
                return
            if status_code != 429:
                return
            delay = _retry_after_seconds(retry_after, current, self._retry_after_default, self._retry_after_max)
            self._backoff_until = max(self._backoff_until or current, current + delay)

    def observe_usage(
        self,
        job_id: str,
        generation: int,
        usage: UsageSummary | Mapping[str, object],
        *,
        terminal: bool = True,
    ) -> None:
        self.observe(job_id, generation, usage, terminal=terminal)

    def observe_provider_error(self, status_code: int, **kwargs: object) -> None:
        self.observe_error(status_code, **kwargs)

    def acknowledge(self, reason: str | None = None) -> None:
        with self._lock:
            self._apply_cost_caps()
            if reason == "soft":
                if "soft" not in self._pause_causes:
                    return
                self._pause_causes.discard("soft")
                self._soft_acknowledged = True
                return
            if reason == "cache":
                if "cache" not in self._pause_causes:
                    return
                self._pause_causes.discard("cache")
                self._cache_acknowledged = True
                return
            if reason == "auth":
                if "auth" not in self._pause_causes:
                    return
                self._pause_causes.discard("auth")
                return
            if reason == "hard":
                return
            if reason is not None:
                raise UsageParseError("unknown acknowledgement reason")
            had_cache_pause = "cache" in self._pause_causes
            had_soft_pause = "soft" in self._pause_causes
            self._pause_causes.difference_update({"auth", "cache", "soft"})
            if had_cache_pause:
                self._cache_acknowledged = True
            if had_soft_pause:
                self._soft_acknowledged = True

    def acknowledge_cache(self) -> None:
        self.acknowledge("cache")

    def acknowledge_auth(self) -> None:
        self.acknowledge("auth")

    def acknowledge_soft_cap(self) -> None:
        self.acknowledge("soft")

    def acknowledge_hard_cap(self) -> None:
        return

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            self._release_backoff()
            self._apply_cost_caps()
            return {
                "version": 1,
                "cache_warmup_turns": self.cache_warmup_turns,
                "minimum_cache_ratio": _decimal_string(self.minimum_cache_ratio),
                "consecutive_failures": self.consecutive_failures,
                "retry_after_default": self._retry_after_default,
                "retry_after_max": self._retry_after_max,
                "cache_failures": self._cache_failures,
                "cache_acknowledged": self._cache_acknowledged,
                "soft_cap_acknowledged": self._soft_acknowledged,
                "pause_causes": sorted(self._pause_causes),
                "terminal_sequence": list(self._terminal_sequence),
                "backoff_until": self._backoff_until,
                "soft_cost_cap": None if self.soft_cost_cap is None else _decimal_string(self.soft_cost_cap),
                "hard_cost_cap": None if self.hard_cost_cap is None else _decimal_string(self.hard_cost_cap),
                "campaign_usage": self._campaign_usage.to_dict(),
                "jobs": {
                    job: {"generation": generation, "usage": summary.to_dict(), "terminal": terminal}
                    for job, (generation, summary, terminal) in sorted(self._jobs.items())
                },
            }

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Mapping[str, object] | bytes | str,
        *,
        clock: Callable[[], float] = time.time,
        **kwargs: object,
    ) -> CircuitBreaker:
        raw = _load_json(snapshot)
        if not isinstance(raw, Mapping):
            raise UsageParseError("breaker snapshot must be an object")
        _require_keys(
            raw,
            {
                "version",
                "cache_warmup_turns",
                "minimum_cache_ratio",
                "consecutive_failures",
                "retry_after_default",
                "retry_after_max",
                "cache_failures",
                "cache_acknowledged",
                "soft_cap_acknowledged",
                "pause_causes",
                "terminal_sequence",
                "backoff_until",
                "soft_cost_cap",
                "hard_cost_cap",
                "campaign_usage",
                "jobs",
            },
        )
        if (
            type(raw["version"]) is not int
            or raw["version"] != 1
            or type(raw["cache_failures"]) is not int
            or raw["cache_failures"] < 0
        ):
            raise UsageParseError("invalid breaker snapshot version or cache failures")
        causes = raw["pause_causes"]
        if (
            not isinstance(causes, list)
            or any(cause not in {"cache", "auth", "soft", "hard"} for cause in causes)
            or len(causes) != len(set(causes))
        ):
            raise UsageParseError("invalid breaker pause causes")
        backoff = raw["backoff_until"]
        if backoff is not None:
            backoff = _finite_time(backoff)
        soft = raw["soft_cost_cap"]
        hard = raw["hard_cost_cap"]
        breaker = cls(
            cache_warmup_turns=raw["cache_warmup_turns"],
            minimum_cache_ratio=_decimal(raw["minimum_cache_ratio"]),
            consecutive_failures=raw["consecutive_failures"],
            soft_cost_cap=soft,
            hard_cost_cap=hard,
            clock=clock,
            retry_after_default=raw["retry_after_default"],
            retry_after_max=raw["retry_after_max"],
            **kwargs,
        )
        if "soft" in causes and breaker.soft_cost_cap is None:
            raise UsageParseError("soft pause requires a soft cost cap")
        if "hard" in causes and breaker.hard_cost_cap is None:
            raise UsageParseError("hard pause requires a hard cost cap")
        campaign = _summary_from_dict(raw["campaign_usage"])
        jobs = raw["jobs"]
        if not isinstance(jobs, Mapping):
            raise UsageParseError("breaker jobs must be an object")
        restored: dict[str, tuple[int, UsageSummary, bool]] = {}
        for job, item in jobs.items():
            if (
                not isinstance(job, str)
                or not isinstance(item, Mapping)
                or set(item) != {"generation", "usage", "terminal"}
            ):
                raise UsageParseError("invalid breaker job snapshot")
            generation = item["generation"]
            if type(generation) is not int or generation < 0:
                raise UsageParseError("invalid breaker job generation")
            if type(item["terminal"]) is not bool:
                raise UsageParseError("invalid breaker job terminal flag")
            if not job:
                raise UsageParseError("breaker job ID must be nonempty")
            restored[job] = (generation, _summary_from_dict(item["usage"], require_turns=True), item["terminal"])
        sequence = raw["terminal_sequence"]
        if (
            not isinstance(sequence, list)
            or any(not isinstance(job, str) for job in sequence)
            or len(sequence) != len(set(sequence))
        ):
            raise UsageParseError("invalid terminal sequence")
        if set(sequence) != {job for job, (_, _, terminal) in restored.items() if terminal}:
            raise UsageParseError("terminal sequence does not match jobs")
        if type(raw["cache_acknowledged"]) is not bool:
            raise UsageParseError("invalid cache acknowledgement")
        if type(raw["soft_cap_acknowledged"]) is not bool:
            raise UsageParseError("invalid soft-cap acknowledgement")
        with breaker._lock:
            persisted_cache_failures = raw["cache_failures"]
            breaker._cache_failures = persisted_cache_failures
            breaker._cache_acknowledged = raw["cache_acknowledged"]
            breaker._soft_acknowledged = raw["soft_cap_acknowledged"]
            breaker._pause_causes = set(causes)
            breaker._backoff_until = backoff
            breaker._campaign_usage = campaign
            breaker._jobs = restored
            breaker._terminal_sequence = list(sequence)
            serialized_causes = set(causes)
            expected_usage, expected_cache_failures = CircuitBreaker._derived_from_jobs(breaker)
            if expected_usage != campaign or expected_cache_failures != persisted_cache_failures:
                raise UsageParseError("breaker snapshot derived state is inconsistent")
            breaker._cache_failures = persisted_cache_failures
            breaker._apply_cost_caps()
            if {cause for cause in serialized_causes if cause in {"soft", "hard"}} != {
                cause for cause in breaker._pause_causes if cause in {"soft", "hard"}
            }:
                raise UsageParseError("cost pause causes are inconsistent with totals")
            if breaker._soft_acknowledged != raw["soft_cap_acknowledged"]:
                raise UsageParseError("soft-cap acknowledgement is inconsistent with totals")
            if breaker._cache_acknowledged and "cache" in serialized_causes:
                raise UsageParseError("acknowledged cache pause is inconsistent")
            if (
                breaker._cache_failures >= breaker.consecutive_failures
                and not breaker._cache_acknowledged
                and "cache" not in serialized_causes
            ):
                raise UsageParseError("cache pause is missing")
            if breaker._soft_acknowledged and "soft" in serialized_causes:
                raise UsageParseError("acknowledged soft pause is inconsistent")
            if (
                "soft" in serialized_causes
                and breaker.soft_cost_cap is not None
                and campaign.cost < breaker.soft_cost_cap
            ):
                raise UsageParseError("soft cost pause is below its configured cap")
            if (
                breaker.soft_cost_cap is not None
                and campaign.cost >= breaker.soft_cost_cap
                and breaker.hard_cost_cap is not None
                and campaign.cost < breaker.hard_cost_cap
                and not breaker._soft_acknowledged
                and "soft" not in serialized_causes
            ):
                raise UsageParseError("soft cost pause is missing")
            if (
                breaker.hard_cost_cap is not None
                and campaign.cost >= breaker.hard_cost_cap
                and "hard" not in breaker._pause_causes
            ):
                raise UsageParseError("hard cost pause is missing")
        return breaker

    @staticmethod
    def _derived_from_jobs(breaker: CircuitBreaker) -> tuple[UsageSummary, int]:
        breaker._recompute_usage()
        breaker._recompute_cache()
        return breaker._campaign_usage, breaker._cache_failures

    def _release_backoff(self) -> None:
        if self._backoff_until is not None and self._clock() >= self._backoff_until:
            self._backoff_until = None

    def _is_paused(self) -> bool:
        self._release_backoff()
        return bool(self._pause_causes) or self._backoff_until is not None

    def _apply_cost_caps(self) -> None:
        cost = self._campaign_usage.cost
        if "hard" in self._pause_causes or self.hard_cost_cap is not None and cost >= self.hard_cost_cap:
            self._pause_causes.add("hard")
            self._pause_causes.discard("soft")
            self._soft_acknowledged = False
            return
        if self.soft_cost_cap is None or cost < self.soft_cost_cap:
            self._pause_causes.discard("soft")
            self._soft_acknowledged = False
            return
        # An explicit soft-cap acknowledgement remains valid while cost stays below hard cap.
        if self._soft_acknowledged:
            self._pause_causes.discard("soft")
            return
        self._pause_causes.add("soft")


def _load_json(payload: Mapping[str, object] | bytes | str) -> object:
    if isinstance(payload, Mapping):
        return payload
    if not isinstance(payload, (bytes, str)):
        raise UsageParseError("payload must be an object or JSON bytes")

    def duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise UsageParseError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise UsageParseError(f"invalid JSON number: {value}")

    try:
        return json.loads(
            payload,
            object_pairs_hook=duplicate_keys,
            parse_float=Decimal,
            parse_constant=reject_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, UsageParseError):
            raise
        raise UsageParseError("invalid JSON payload") from error


def _token(value: object) -> int:
    if value is None:
        return 0
    if type(value) is not int or value < 0:
        raise UsageParseError("token counts must be nonnegative integers")
    return value


def _decimal(value: object) -> Decimal:
    if value is None:
        return Decimal(0)
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise UsageParseError("cost must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise UsageParseError("cost must be numeric") from error
    if not result.is_finite() or result < 0:
        raise UsageParseError("cost must be a nonnegative finite decimal")
    if result.is_zero():
        return Decimal(0)
    return result


def _usage_cost(value: object) -> Decimal:
    if value is None:
        return Decimal(0)
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise UsageParseError("projected message cost must be a JSON number")
    return _decimal(value)


def _decimal_string(value: Decimal) -> str:
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result or "0"


def _sum_decimals(values: object) -> Decimal:
    decimals = tuple(values)
    if not decimals:
        return Decimal(0)
    minimum_exponent = min(value.as_tuple().exponent for value in decimals)
    total = sum(
        int("".join(str(digit) for digit in value.as_tuple().digits) or "0")
        * (10 ** (value.as_tuple().exponent - minimum_exponent))
        for value in decimals
    )
    if total == 0:
        return Decimal(0)
    return Decimal((0, tuple(int(digit) for digit in str(total)), minimum_exponent))


def _summary_fields(turns: tuple[TurnUsage, ...]) -> tuple[object, ...]:
    return (
        len(turns),
        sum(turn.input for turn in turns),
        sum(turn.output for turn in turns),
        sum(turn.reasoning for turn in turns),
        sum(turn.cache_read for turn in turns),
        sum(turn.cache_write for turn in turns),
        sum(turn.prompt_total for turn in turns),
        sum(turn.cache_hit for turn in turns),
        sum(not turn.cache_hit for turn in turns),
        max((turn.prompt_total for turn in turns), default=0),
        _sum_decimals(turn.cost for turn in turns),
        any(turn.cache_read > 0 and turn.cache_write == 0 for turn in turns),
    )


def _coerce_summary(value: UsageSummary | Mapping[str, object]) -> UsageSummary:
    if isinstance(value, UsageSummary):
        return UsageSummary(
            provider_turns=value.provider_turns,
            input=value.input,
            output=value.output,
            reasoning=value.reasoning,
            cache_read=value.cache_read,
            cache_write=value.cache_write,
            prompt_total=value.prompt_total,
            turns_with_cache=value.turns_with_cache,
            turns_without_cache=value.turns_without_cache,
            max_prompt_total=value.max_prompt_total,
            cost=value.cost,
            telemetry_gap=value.telemetry_gap,
            turns=value.turns,
        )
    if isinstance(value, Mapping) and "data" in value:
        return summarize_usage(value)
    return _summary_from_dict(value)


def _summary_from_dict(value: object, *, require_turns: bool = False) -> UsageSummary:
    if not isinstance(value, Mapping):
        raise UsageParseError("usage summary must be an object")
    required = {
        "provider_turns",
        "input",
        "output",
        "reasoning",
        "cache_read",
        "cache_write",
        "prompt_total",
        "turns_with_cache",
        "turns_without_cache",
        "max_prompt_total",
        "cost",
        "telemetry_gap",
        "turns",
    }
    _require_keys(value, required)
    turns_value = value["turns"]
    if not isinstance(turns_value, list):
        raise UsageParseError("usage turns must be a list")
    turns: list[TurnUsage] = []
    for item in turns_value:
        if not isinstance(item, Mapping) or set(item) != {
            "input",
            "output",
            "reasoning",
            "cache_read",
            "cache_write",
            "prompt_total",
            "cost",
        }:
            raise UsageParseError("invalid usage turn")
        turn = TurnUsage(
            input=_token(item["input"]),
            output=_token(item["output"]),
            reasoning=_token(item["reasoning"]),
            cache_read=_token(item["cache_read"]),
            cache_write=_token(item["cache_write"]),
            prompt_total=_token(item["prompt_total"]),
            cost=_decimal(item["cost"]),
        )
        if turn.prompt_total != turn.input + turn.cache_read + turn.cache_write:
            raise UsageParseError("usage turn prompt total is inconsistent")
        turns.append(turn)
    summary = UsageSummary(
        provider_turns=_token(value["provider_turns"]),
        input=_token(value["input"]),
        output=_token(value["output"]),
        reasoning=_token(value["reasoning"]),
        cache_read=_token(value["cache_read"]),
        cache_write=_token(value["cache_write"]),
        prompt_total=_token(value["prompt_total"]),
        turns_with_cache=_token(value["turns_with_cache"]),
        turns_without_cache=_token(value["turns_without_cache"]),
        max_prompt_total=_token(value["max_prompt_total"]),
        cost=_decimal(value["cost"]),
        telemetry_gap=value["telemetry_gap"],
        turns=tuple(turns),
    )
    if turns and summary.provider_turns != len(turns):
        raise UsageParseError("usage summary turn counts are inconsistent")
    if summary.turns_with_cache + summary.turns_without_cache != summary.provider_turns:
        raise UsageParseError("usage summary turn counts are inconsistent")
    if require_turns and not turns and summary != UsageSummary():
        raise UsageParseError("zero-turn job usage has nonzero aggregate fields")
    if turns:
        recomputed = UsageSummary(
            provider_turns=len(turns),
            input=sum(turn.input for turn in turns),
            output=sum(turn.output for turn in turns),
            reasoning=sum(turn.reasoning for turn in turns),
            cache_read=sum(turn.cache_read for turn in turns),
            cache_write=sum(turn.cache_write for turn in turns),
            prompt_total=sum(turn.prompt_total for turn in turns),
            turns_with_cache=sum(turn.cache_hit for turn in turns),
            turns_without_cache=sum(not turn.cache_hit for turn in turns),
            max_prompt_total=max(turn.prompt_total for turn in turns),
            cost=_sum_decimals(turn.cost for turn in turns),
            telemetry_gap=any(turn.cache_read > 0 and turn.cache_write == 0 for turn in turns),
            turns=tuple(turns),
        )
        if summary != recomputed:
            raise UsageParseError("usage summary does not match per-turn records")
    return summary


def _require_keys(value: Mapping[str, object], allowed: set[str]) -> None:
    if set(value) != allowed or any(not isinstance(key, str) for key in value):
        raise UsageParseError("snapshot contains unknown or missing fields")


def _add(total: UsageSummary, delta: UsageSummary) -> UsageSummary:
    turns = total.turns + delta.turns
    fields = _summary_fields(turns)
    return UsageSummary(
        provider_turns=fields[0],
        input=fields[1],
        output=fields[2],
        reasoning=fields[3],
        cache_read=fields[4],
        cache_write=fields[5],
        prompt_total=fields[6],
        turns_with_cache=fields[7],
        turns_without_cache=fields[8],
        max_prompt_total=fields[9],
        cost=fields[10],
        telemetry_gap=fields[11],
        turns=turns,
    )


def _finite_time(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)) or not math.isfinite(float(value)):
        raise UsageParseError("time must be finite")
    return float(value)


def _retry_after_seconds(value: object, now: float, default: int, maximum: int) -> float:
    if value is None:
        return float(default)
    try:
        seconds = float(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        try:
            parsed = parsedate_to_datetime(str(value))
        except (TypeError, ValueError, OverflowError):
            return float(default)
        if parsed is None:
            return float(default)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        seconds = parsed.timestamp() - now
    if not math.isfinite(seconds):
        return float(default)
    return max(0.0, min(float(maximum), seconds))
