import hashlib
import json
import re
import shlex
import threading
import time
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.campaign_usage import CircuitBreaker


def job(index: int):
    from scripts.campaign_runner import CampaignJob

    return CampaignJob(f"task-{index}", "vanilla" if index % 2 else "procontract", f"image-{index}")


def test_scheduler_never_runs_more_than_worker_active_jobs():
    from scripts.campaign_runner import run_campaign

    active = 0
    maximum = 0
    lock = threading.Lock()
    completed = []

    def execute(candidate, _stop):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.01)
        with lock:
            active -= 1
            completed.append(candidate.key)

    result = run_campaign(tuple(job(index) for index in range(16)), workers=8, execute=execute)

    assert maximum == 8
    assert result.completed == 16
    assert result.pending == ()
    assert len(completed) == 16


def test_daemon_worker_pool_joins_all_workers_after_normal_completion():
    from scripts.campaign_runner import _DaemonWorkerPool

    pool = _DaemonWorkerPool(2)
    futures = [pool.submit(lambda value=value: value) for value in range(4)]
    pool.shutdown(wait=True, cancel_futures=False)

    assert [future.result() for future in futures] == list(range(4))
    assert not any(thread.is_alive() for thread in pool.threads)


def test_daemon_worker_pool_cancels_queued_future_without_joining_active_job():
    from scripts.campaign_runner import _DaemonWorkerPool

    pool = _DaemonWorkerPool(1)
    started = threading.Event()
    release = threading.Event()
    active = pool.submit(lambda: (started.set(), release.wait(1)))
    assert started.wait(1)
    queued = pool.submit(lambda: pytest.fail("queued job must be cancelled"))

    pool.shutdown(wait=False, cancel_futures=True)
    release.set()
    for thread in pool.threads:
        thread.join(1)

    assert active.result() == (None, True)
    assert queued.cancelled()
    assert not any(thread.is_alive() for thread in pool.threads)


def test_stop_token_is_thread_safe_and_keeps_first_sanitized_reason():
    from scripts.campaign_stop import StopToken

    token = StopToken()
    first = threading.Thread(target=lambda: token.set(" keyboard interrupt\nunsafe detail "))
    second = threading.Thread(target=lambda: (first.join(), token.set("replacement")))
    first.start()
    second.start()
    first.join()
    second.join()

    assert token.is_set()
    assert token.wait(0)
    assert token.reason == "keyboard_interrupt_unsafe_detail"


def test_scheduler_passes_one_real_stop_token_to_every_job():
    from scripts.campaign_runner import run_campaign
    from scripts.campaign_stop import StopToken

    seen = []

    run_campaign(tuple(job(index) for index in range(2)), workers=1, execute=lambda _job, stop: seen.append(stop))

    assert len(seen) == 2
    assert seen[0] is seen[1]
    assert isinstance(seen[0], StopToken)
    assert not seen[0].is_set()


def test_scheduler_treats_preexisting_external_stop_as_interrupted_without_admission():
    from scripts.campaign_runner import run_campaign

    event = threading.Event()
    event.set()

    result = run_campaign(
        tuple(job(index) for index in range(2)),
        stop_event=event,
        execute=lambda *_: pytest.fail("pre-stopped campaign cannot admit work"),
    )

    assert result.interrupted
    assert result.pending == ()


def test_active_job_registry_runs_idempotent_force_callbacks_outside_lock():
    from scripts.campaign_stop import ActiveJobRegistry

    registry = ActiveJobRegistry()
    calls = []
    handle = registry.register(
        job(0),
        force_stop=lambda: calls.append(registry.active_jobs()),
        terminalize=lambda: calls.append("terminalized"),
    )

    registry.force_stop_all()
    registry.force_stop_all()
    registry.unregister(handle)

    assert calls == [((job(0),)), "terminalized"]
    assert registry.active_jobs() == ()


def test_active_job_registry_remembers_idempotent_callback_failure():
    from scripts.campaign_stop import ActiveJobRegistry

    registry = ActiveJobRegistry()
    calls = []
    candidate = job(0)

    def force_stop():
        calls.append("force")
        raise RuntimeError("secret")

    handle = registry.register(candidate, force_stop=force_stop, terminalize=lambda: False)

    assert registry.force_stop_all() == (candidate,)
    assert registry.force_stop_all() == (candidate,)
    registry.unregister(handle)
    assert calls == ["force"]


def test_active_job_registry_skips_entry_unregistered_after_force_snapshot():
    from scripts.campaign_stop import ActiveJobRegistry

    registry = ActiveJobRegistry()
    first_started = threading.Event()
    release_first = threading.Event()
    second_calls = []
    results = []
    first = registry.register(
        job(0),
        force_stop=lambda: (first_started.set(), release_first.wait(1)),
        terminalize=lambda: None,
    )
    second = registry.register(
        job(1),
        force_stop=lambda: second_calls.append("stale"),
        terminalize=lambda: False,
    )
    thread = threading.Thread(target=lambda: results.append(registry.force_stop_all()))
    thread.start()
    assert first_started.wait(1)
    registry.unregister(second)
    release_first.set()
    thread.join(1)
    registry.unregister(first)

    assert results == [()]
    assert second_calls == []


def test_scheduler_prints_plain_progress(capsys):
    from scripts.campaign_runner import run_campaign

    run_campaign((job(0),), workers=1)

    assert "completed=1 running=0 pending=0 failed=0" in capsys.readouterr().out


def test_scheduler_leaves_unadmitted_jobs_pending_when_breaker_pauses():
    from scripts.campaign_runner import run_campaign

    breaker = CircuitBreaker(cache_warmup_turns=0, consecutive_failures=2)
    started = []

    def execute(candidate, _stop):
        started.append(candidate.key)
        breaker.observe_error(401)

    result = run_campaign(tuple(job(index) for index in range(4)), workers=1, breaker=breaker, execute=execute)

    assert started == ["task-0/procontract"]
    assert result.pending == tuple(job(index) for index in range(1, 4))
    assert result.pause_reason == "provider authentication failure"


def test_scheduler_drains_running_jobs_before_returning_paused():
    from scripts.campaign_runner import run_campaign

    breaker = CircuitBreaker()
    ready = threading.Barrier(2)
    first_finished = threading.Event()
    second_finished = threading.Event()

    def execute(candidate, _stop):
        if candidate.instance_id == "task-0":
            ready.wait(1)
            breaker.observe_error(401)
            first_finished.set()
            return
        ready.wait(1)
        assert first_finished.wait(1)
        second_finished.set()

    result = run_campaign(tuple(job(index) for index in range(3)), workers=2, breaker=breaker, execute=execute)

    assert second_finished.is_set()
    assert result.paused
    assert result.running == 0
    assert result.pending == (job(2),)


def test_scheduler_waits_for_rate_limit_backoff_before_admitting_next_job():
    from scripts.campaign_runner import run_campaign

    now = [0.0]
    breaker = CircuitBreaker(clock=lambda: now[0], retry_after_default=1)
    started = []

    def execute(candidate, _stop):
        started.append(candidate.key)
        if len(started) == 1:
            breaker.observe_error(429, retry_after="2", now=now[0])

    result = run_campaign(
        (job(0), job(1)),
        workers=1,
        breaker=breaker,
        execute=execute,
        clock=lambda: now[0],
        sleeper=lambda delay: now.__setitem__(0, now[0] + delay),
    )

    assert result.completed == 2
    assert started == ["task-0/procontract", "task-1/vanilla"]


def test_scheduler_interrupts_3600_second_admission_backoff_within_bound():
    from scripts.campaign_runner import run_campaign

    breaker = CircuitBreaker(retry_after_default=3600, retry_after_max=3600)
    breaker.observe_error(429, retry_after="3600")
    stop = threading.Event()
    results = []
    thread = threading.Thread(
        target=lambda: results.append(run_campaign((job(0),), breaker=breaker, stop_event=stop)),
        daemon=True,
    )
    started = time.monotonic()
    thread.start()
    time.sleep(0.02)
    stop.set()
    thread.join(0.2)

    assert not thread.is_alive()
    assert time.monotonic() - started < 0.2
    assert results[0].interrupted
    assert results[0].pending == ()


def test_scheduler_rechecks_extended_backoff_and_auth_between_one_second_slices():
    from scripts.campaign_runner import run_campaign

    now = [0.0]
    breaker = CircuitBreaker(clock=lambda: now[0], retry_after_default=2, retry_after_max=10)
    breaker.observe_error(429, retry_after="2", now=0)
    sleeps = []

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay
        if len(sleeps) == 1:
            breaker.observe_error(429, retry_after="4", now=now[0])
        if len(sleeps) == 2:
            breaker.observe_error(401)

    result = run_campaign((job(0),), breaker=breaker, sleeper=sleep, clock=lambda: now[0])

    assert sleeps == [1, 1]
    assert result.paused
    assert result.pending == (job(0),)


def test_scheduler_persists_sanitized_bounded_campaign_log(tmp_path: Path):
    from scripts.campaign_runner import run_campaign

    secret = "not-for-log"
    state_path = tmp_path / "breaker.json"
    result = run_campaign(
        (job(0),),
        workers=1,
        state_path=state_path,
        execute=lambda _job, _stop: (_ for _ in ()).throw(RuntimeError(secret)),
    )

    assert result.failed == 1
    assert state_path.is_file()
    assert secret not in (tmp_path / "campaign.log").read_text()


def test_scheduler_logs_the_future_that_completed_not_last_admitted_job(tmp_path: Path):
    from scripts.campaign_runner import run_campaign

    def execute(candidate, _stop):
        if candidate.instance_id == "task-1":
            time.sleep(0.05)

    run_campaign(
        tuple(job(index) for index in range(2)), workers=2, state_path=tmp_path / "breaker.json", execute=execute
    )

    assert "task-0 procontract completed" in (tmp_path / "campaign.log").read_text()


def test_scheduler_counts_normalized_job_failure_as_failed():
    from scripts.campaign_runner import JobRunResult, run_campaign

    result = run_campaign(
        (job(0),),
        workers=1,
        execute=lambda candidate, _stop: JobRunResult(candidate, "failed"),
    )

    assert result.completed == 0
    assert result.failed == 1


def test_scheduler_counts_normalized_interruption_as_failed():
    from scripts.campaign_runner import JobRunResult, run_campaign

    result = run_campaign(
        (job(0),),
        workers=1,
        execute=lambda candidate, _stop: JobRunResult(candidate, "interrupted"),
    )

    assert result.completed == 0
    assert result.failed == 1


def test_keyboard_interrupt_stops_active_workers_and_cancels_admission():
    from scripts.campaign_runner import run_campaign

    second_started = threading.Event()
    observed_stop = threading.Event()

    def execute(candidate, stop):
        if candidate.instance_id == "task-0":
            assert second_started.wait(1)
            raise KeyboardInterrupt
        second_started.set()
        assert stop.wait(1)
        observed_stop.set()

    result = run_campaign(tuple(job(index) for index in range(4)), workers=2, execute=execute, interrupt_timeout=1)

    assert observed_stop.is_set()
    assert result.running == 0
    assert result.pending == ()


def test_keyboard_interrupt_force_stops_exact_registered_active_job_after_grace():
    from scripts.campaign_runner import run_campaign

    ready = threading.Barrier(2)
    released = threading.Event()
    forced = []
    terminalized = []

    def execute(candidate, stop):
        if candidate.instance_id == "task-0":
            ready.wait(1)
            raise KeyboardInterrupt
        handle = stop.registry.register(
            candidate,
            force_stop=lambda: (forced.append(candidate), released.set()),
            terminalize=lambda: terminalized.append(candidate),
        )
        try:
            ready.wait(1)
            assert released.wait(1)
        finally:
            stop.registry.unregister(handle)

    result = run_campaign(tuple(job(index) for index in range(3)), workers=2, execute=execute, interrupt_timeout=0.01)

    assert result.interrupted
    assert forced == [job(1)]
    assert terminalized == [job(1)]
    assert result.running == 0
    assert result.pending == ()


@pytest.mark.parametrize("mode", ["blocking_callback", "noncooperative_worker"])
def test_fatal_shutdown_exits_real_subprocess_with_only_daemon_threads(tmp_path: Path, mode: str):
    root = Path(__file__).parents[1]
    script = r"""
import sys
import threading
import time
from pathlib import Path
from scripts.campaign_runner import CampaignJob, CampaignShutdownError, run_campaign
from scripts.campaign_state import JobState, StateStore

state = StateStore(Path(sys.argv[1]) / "state.json")
state.write(JobState("planned"))
ready = threading.Event()
released = threading.Event()
mode = sys.argv[2]

def terminalize():
    current = state.read()
    if current.phase != "interrupted":
        state.transition(
            "interrupted",
            command_identity="shutdown",
            expected_generation=current.generation,
            exit_class="campaign_stop",
        )
    released.set()
    return True

def execute(candidate, stop):
    if candidate.instance_id == "interrupt":
        assert ready.wait(1)
        raise KeyboardInterrupt
    def force_stop():
        if mode == "blocking_callback":
            while True:
                time.sleep(1)
    handle = stop.registry.register(candidate, force_stop=force_stop, terminalize=terminalize)
    ready.set()
    try:
        if mode == "noncooperative_worker":
            while True:
                time.sleep(1)
        assert released.wait(1)
    finally:
        stop.registry.unregister(handle)

try:
    started = time.monotonic()
    run_campaign(
        (
            CampaignJob("interrupt", "vanilla", "image"),
            CampaignJob("active", "procontract", "image"),
        ),
        workers=2,
        execute=execute,
        interrupt_timeout=0.02,
    )
except CampaignShutdownError:
    print(state.read().phase, time.monotonic() - started, flush=True)
    raise SystemExit(9)
raise SystemExit(0)
"""
    started = time.monotonic()

    result = subprocess.run(
        (sys.executable, "-c", script, str(tmp_path), mode),
        cwd=root,
        capture_output=True,
        text=True,
        timeout=2,
    )

    assert result.returncode == 9
    phase, elapsed = result.stdout.split()
    assert phase == "interrupted"
    assert float(elapsed) < 0.1
    assert json.loads(tmp_path.joinpath("state.json").read_text())["phase"] == "interrupted"
    assert time.monotonic() - started < 2


def test_force_stop_or_terminal_cas_failure_is_fatal_even_when_worker_then_exits():
    from scripts.campaign_runner import CampaignShutdownError, run_campaign

    ready = threading.Barrier(2)
    released = threading.Event()
    finished = threading.Event()
    secret = "force-stop-secret"

    def execute(candidate, stop):
        if candidate.instance_id == "task-0":
            ready.wait(1)
            raise KeyboardInterrupt

        def force_stop():
            raise RuntimeError(secret)

        def terminalize():
            released.set()
            return False

        handle = stop.registry.register(candidate, force_stop=force_stop, terminalize=terminalize)
        try:
            ready.wait(1)
            assert released.wait(1)
            finished.set()
        finally:
            stop.registry.unregister(handle)

    with pytest.raises(CampaignShutdownError) as raised:
        run_campaign(tuple(job(index) for index in range(2)), workers=2, execute=execute, interrupt_timeout=0.01)

    assert finished.is_set()
    assert secret not in str(raised.value)


def test_persistent_hard_cost_stop_forces_registered_active_jobs():
    from scripts.campaign_runner import run_campaign
    from scripts.campaign_usage import TurnUsage, UsageSummary

    breaker = CircuitBreaker(hard_cost_cap="1")
    ready = threading.Barrier(2)
    released = threading.Event()
    forced = []
    turn = TurnUsage(input=1, prompt_total=1, cost=Decimal("1"))
    summary = UsageSummary(
        provider_turns=1,
        input=1,
        prompt_total=1,
        turns_without_cache=1,
        max_prompt_total=1,
        cost=Decimal("1"),
        turns=(turn,),
    )

    def execute(candidate, stop):
        if candidate.instance_id == "task-0":
            ready.wait(1)
            breaker.observe(candidate.key, 0, summary)
            return
        handle = stop.registry.register(
            candidate,
            force_stop=lambda: (forced.append(candidate), released.set()),
            terminalize=lambda: None,
        )
        try:
            ready.wait(1)
            assert released.wait(1)
        finally:
            stop.registry.unregister(handle)

    result = run_campaign(
        tuple(job(index) for index in range(3)), workers=2, breaker=breaker, execute=execute, interrupt_timeout=0.01
    )

    assert result.interrupted
    assert result.pause_reason == "campaign hard cost cap reached"
    assert forced == [job(1)]
    assert result.pending == ()


def test_noncooperative_hard_stop_persists_before_force_callback_and_restart_admission(tmp_path: Path):
    from scripts.campaign_runner import CampaignShutdownError, run_campaign
    from scripts.campaign_usage import TurnUsage, UsageSummary

    breaker = CircuitBreaker(hard_cost_cap="1")
    path = tmp_path / "breaker.json"
    observed = []
    turn = TurnUsage(input=1, prompt_total=1, cost=Decimal("1"))
    usage = UsageSummary(
        provider_turns=1,
        input=1,
        prompt_total=1,
        turns_without_cache=1,
        max_prompt_total=1,
        cost=Decimal("1"),
        turns=(turn,),
    )

    def execute(candidate, stop):
        stop.registry.register(
            candidate,
            force_stop=lambda: observed.append(CircuitBreaker.from_snapshot(json.loads(path.read_text())).hard_stopped),
            terminalize=lambda: True,
        )
        breaker.observe(candidate.key, 0, usage)
        while True:
            time.sleep(1)

    with pytest.raises(CampaignShutdownError):
        run_campaign((job(0),), workers=1, breaker=breaker, state_path=path, execute=execute, interrupt_timeout=0.01)

    restored = CircuitBreaker.from_snapshot(json.loads(path.read_text()))
    assert observed == [True]
    assert restored.hard_stopped
    assert not restored.admission_allowed


@pytest.mark.parametrize("kind", ["auth", "cache", "soft"])
def test_global_pause_persists_before_active_worker_completion(tmp_path: Path, kind: str):
    from scripts.campaign_runner import run_campaign
    from scripts.campaign_usage import TurnUsage, UsageSummary

    breaker = {
        "auth": CircuitBreaker(),
        "cache": CircuitBreaker(cache_warmup_turns=0, consecutive_failures=1),
        "soft": CircuitBreaker(soft_cost_cap="1", hard_cost_cap="2"),
    }[kind]
    path = tmp_path / "breaker.json"
    changed = threading.Event()
    release = threading.Event()
    turn = TurnUsage(input=1, prompt_total=1, cost=Decimal("1" if kind == "soft" else "0"))
    usage = UsageSummary(
        provider_turns=1,
        input=1,
        prompt_total=1,
        turns_without_cache=1,
        max_prompt_total=1,
        cost=turn.cost,
        turns=(turn,),
    )

    def execute(candidate, _stop):
        if kind == "auth":
            breaker.observe_error(401)
        else:
            breaker.observe(candidate.key, 0, usage)
        changed.set()
        assert release.wait(1)

    thread = threading.Thread(
        target=lambda: run_campaign((job(0),), breaker=breaker, state_path=path, execute=execute),
        daemon=True,
    )
    thread.start()
    try:
        assert changed.wait(1)
        deadline = time.monotonic() + 0.3
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert path.is_file()
        assert CircuitBreaker.from_snapshot(json.loads(path.read_text())).paused
    finally:
        release.set()
        thread.join(1)


def test_external_interrupt_persists_breaker_before_active_worker_drain(tmp_path: Path):
    from scripts.campaign_runner import run_campaign

    path = tmp_path / "breaker.json"
    stop = threading.Event()
    started = threading.Event()
    release = threading.Event()

    def execute(_candidate, _stop):
        started.set()
        assert release.wait(1)

    thread = threading.Thread(
        target=lambda: run_campaign((job(0),), state_path=path, stop_event=stop, execute=execute, interrupt_timeout=1),
        daemon=True,
    )
    thread.start()
    try:
        assert started.wait(1)
        stop.set()
        deadline = time.monotonic() + 0.3
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert path.is_file()
        CircuitBreaker.from_snapshot(json.loads(path.read_text()))
    finally:
        release.set()
        thread.join(1)


def test_breaker_persistence_failure_forces_cleanup_and_exposes_recovery_evidence(tmp_path: Path, monkeypatch):
    from scripts import campaign_runner
    from scripts.campaign_usage import TurnUsage, UsageSummary

    breaker = CircuitBreaker(hard_cost_cap="1")
    path = tmp_path / "breaker.json"
    forced = threading.Event()
    release = threading.Event()
    turn = TurnUsage(input=1, prompt_total=1, cost=Decimal("1"))
    usage = UsageSummary(
        provider_turns=1,
        input=1,
        prompt_total=1,
        turns_without_cache=1,
        max_prompt_total=1,
        cost=Decimal("1"),
        turns=(turn,),
    )

    def execute(candidate, stop):
        stop.registry.register(
            candidate,
            force_stop=lambda: (forced.set(), release.set()),
            terminalize=lambda: True,
        )
        breaker.observe(candidate.key, 0, usage)
        assert release.wait(1)

    monkeypatch.setattr(campaign_runner, "_atomic_json", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(campaign_runner.CampaignDurabilityError) as raised:
        campaign_runner.run_campaign(
            (job(0),),
            workers=1,
            breaker=breaker,
            state_path=path,
            execute=execute,
            interrupt_timeout=0.01,
        )

    assert forced.is_set()
    assert raised.value.recovery_path.is_file()
    assert CircuitBreaker.from_snapshot(json.loads(raised.value.recovery_path.read_text())).hard_stopped
    raised.value.recovery_path.unlink()


def test_secondary_recovery_failure_still_forces_cleanup_and_keeps_in_memory_evidence(tmp_path: Path, monkeypatch):
    from scripts import campaign_runner
    from scripts.campaign_usage import TurnUsage, UsageSummary

    breaker = CircuitBreaker(hard_cost_cap="1")
    forced = threading.Event()
    release = threading.Event()
    turn = TurnUsage(input=1, prompt_total=1, cost=Decimal("1"))
    usage = UsageSummary(
        provider_turns=1,
        input=1,
        prompt_total=1,
        turns_without_cache=1,
        max_prompt_total=1,
        cost=Decimal("1"),
        turns=(turn,),
    )

    def execute(candidate, stop):
        stop.registry.register(
            candidate,
            force_stop=lambda: (forced.set(), release.set()),
            terminalize=lambda: True,
        )
        breaker.observe(candidate.key, 0, usage)
        assert release.wait(1)

    monkeypatch.setattr(campaign_runner, "_atomic_json", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(
        campaign_runner,
        "_breaker_recovery",
        lambda *_: (_ for _ in ()).throw(OSError("recovery disk full")),
    )
    with pytest.raises(campaign_runner.CampaignDurabilityError) as raised:
        campaign_runner.run_campaign(
            (job(0),),
            workers=1,
            breaker=breaker,
            state_path=tmp_path / "breaker.json",
            execute=execute,
            interrupt_timeout=0.01,
        )

    assert forced.is_set()
    assert raised.value.recovery_path is None
    assert CircuitBreaker.from_snapshot(raised.value.recovery_snapshot).hard_stopped


def test_keyboard_interrupt_raises_when_worker_ignores_stop_until_released():
    from scripts.campaign_runner import CampaignShutdownError, run_campaign

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def execute(candidate, _stop):
        if candidate.instance_id == "task-0":
            assert started.wait(1)
            raise KeyboardInterrupt
        started.set()
        release.wait(2)
        finished.set()

    try:
        with pytest.raises(CampaignShutdownError):
            run_campaign(tuple(job(index) for index in range(2)), workers=2, execute=execute, interrupt_timeout=0.01)
    finally:
        release.set()
        assert finished.wait(1)


def test_expand_campaign_pairs_every_selected_instance_in_seeded_order():
    from scripts.campaign_runner import expand_campaign_jobs

    first = expand_campaign_jobs(("one", "two"), {"one": "one-image", "two": "two-image"}, seed=42)

    assert {(candidate.instance_id, candidate.arm) for candidate in first} == {
        ("one", "vanilla"),
        ("one", "procontract"),
        ("two", "vanilla"),
        ("two", "procontract"),
    }
    assert first == expand_campaign_jobs(("one", "two"), {"one": "one-image", "two": "two-image"}, seed=42)


def test_cli_parser_has_bounded_campaign_defaults_and_evaluator_flags():
    from scripts.campaign_runner import build_parser, parse_args

    parser = build_parser()
    args = parser.parse_args(["--config", "campaign.yaml", "--dry-run"])
    assert args.workers == 8
    assert args.limit == 8
    assert "8 active trajectories" in parser.format_help()
    evaluator = parse_args(["--config", "campaign.yaml", "--output", "out", "--evaluate"])
    assert evaluator.evaluate
    assert not evaluator.retry_evaluator


def test_cli_limit_and_all_conflict_and_non_dry_requires_output():
    from scripts.campaign_runner import parse_args

    with pytest.raises(SystemExit, match="2"):
        parse_args(["--config", "campaign.yaml", "--all", "--limit", "8", "--dry-run"])
    with pytest.raises(SystemExit, match="2"):
        parse_args(["--config", "campaign.yaml", "--all", "--limit=8", "--dry-run"])
    with pytest.raises(SystemExit, match="2"):
        parse_args(["--config", "campaign.yaml"])
    with pytest.raises(SystemExit, match="2"):
        parse_args(["--config", "campaign.yaml", "--workers", "9", "--dry-run"])
    with pytest.raises(SystemExit, match="2"):
        parse_args(["--config", "campaign.yaml", "--redo", "--retry-infra", "--dry-run"])


def test_direct_script_cli_help_is_import_safe():
    root = Path(__file__).parents[1]
    result = subprocess.run(
        (sys.executable, str(root / "scripts/run_opencode.py"), "--help"), cwd=root, capture_output=True, text=True
    )

    assert result.returncode == 0
    assert "8 active trajectories" in result.stdout
    for option in (
        "--workers",
        "--limit",
        "--seed",
        "--resume",
        "--acknowledge",
        "--all",
        "--evaluate",
        "--retry-evaluator",
        "--dry-run",
    ):
        assert option in result.stdout


def test_public_entrypoint_requires_resume_after_documented_dry_run(tmp_path: Path, monkeypatch):
    from scripts import campaign_runner, run_opencode

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    output = tmp_path / "pilot"
    guide = Path(__file__).parents[1].joinpath("docs/README.md").read_text()
    pilot = guide.split("### Pilot workflow", 1)[1].split("### Pauses, evaluation, and results", 1)[0]
    commands = [
        command
        for command in re.findall(r"```bash\n(.*?)```", pilot, re.DOTALL)
        if "scripts/run_opencode.py" in command
    ]
    documented = [_documented_script_arguments(command) for command in commands]
    dry_run = documented[0]
    inference = documented[1]
    assert all("--resume" in arguments for arguments in documented[1:] if "output/luna-pilot" in arguments)
    for arguments in (dry_run, inference):
        _replace_argument(arguments, "--config", str(config_path))
        _replace_argument(arguments, "--output", str(output))
        _replace_argument(arguments, "--workers", "1")
        _replace_argument(arguments, "--limit", "1")

    assert "--dry-run" in dry_run
    assert "--resume" in inference

    instances = [
        {
            "instance_id": "one",
            "difficulty": "easy",
            "language": "python",
            "image_name": "one-image",
            "repository": "owner/one",
            "commit": "a" * 40,
        }
    ]
    prepared = []
    executed = []
    original_plan_campaign = campaign_runner.plan_campaign

    def plan_campaign(*args, **kwargs):
        return original_plan_campaign(
            *args,
            **kwargs,
            prepare_install_fn=lambda arm, root: (
                prepared.append((arm.name, root))
                or {"arm": arm.name, "revision": arm.revision, "binary_sha256": arm.name[0] * 64}
            ),
        )

    def run_job(_plan, candidate, **_kwargs):
        executed.append(candidate)
        return campaign_runner.JobRunResult(candidate, "completed")

    monkeypatch.setattr(campaign_runner, "load_all_instances", lambda **_kwargs: instances)
    monkeypatch.setattr(campaign_runner, "plan_campaign", plan_campaign)
    monkeypatch.setattr(campaign_runner, "run_job", run_job)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "test-password")

    assert _invoke_public_main(run_opencode, monkeypatch, dry_run) == 0
    assert output.joinpath("campaign.json").is_file()
    assert len(prepared) == 2
    assert executed == []

    without_resume = [argument for argument in inference if argument != "--resume"]
    monkeypatch.setattr(sys, "argv", ["scripts/run_opencode.py", *without_resume])
    with pytest.raises(campaign_runner.CampaignError, match="use --resume"):
        run_opencode.main()
    assert executed == []

    assert _invoke_public_main(run_opencode, monkeypatch, inference) == 0
    assert {(candidate.instance_id, candidate.arm) for candidate in executed} == {
        ("one", "procontract"),
        ("one", "vanilla"),
    }
    assert len(prepared) == 2


def test_cli_resume_requires_existing_manifest_before_loading_dataset(tmp_path: Path, monkeypatch):
    from scripts import campaign_runner

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    monkeypatch.setattr(campaign_runner, "load_all_instances", lambda **_: pytest.fail("resume loaded dataset"))

    with pytest.raises(campaign_runner.CampaignError, match="manifest.*missing"):
        campaign_runner.main(
            ["--config", str(config_path), "--output", str(tmp_path / "missing"), "--resume", "--dry-run"]
        )


def test_cli_resume_rejects_corrupt_manifest_before_loading_dataset(tmp_path: Path, monkeypatch):
    from scripts import campaign_runner

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    output = tmp_path / "corrupt"
    output.mkdir()
    output.joinpath("campaign.json").write_text("{")
    monkeypatch.setattr(campaign_runner, "load_all_instances", lambda **_: pytest.fail("resume loaded dataset"))

    with pytest.raises(campaign_runner.CampaignError, match="invalid campaign data"):
        campaign_runner.main(["--config", str(config_path), "--output", str(output), "--resume", "--dry-run"])


def test_plan_dry_run_prepares_each_arm_once_without_workspace(tmp_path: Path):
    from scripts.campaign_runner import plan_campaign
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    prepared = []

    def prepare(arm, root):
        prepared.append((arm.name, root))
        return {"arm": arm.name, "revision": arm.revision, "binary_sha256": "a" * 64}

    plan = plan_campaign(
        load_config(config_path),
        [
            {
                "instance_id": "one",
                "difficulty": "easy",
                "language": "python",
                "image_name": "one-image",
                "repository": "owner/one",
                "commit": "a" * 40,
            },
            {
                "instance_id": "two",
                "difficulty": "hard",
                "language": "rust",
                "image_name": "two-image",
                "repository": "owner/two",
                "commit": "b" * 40,
            },
        ],
        output=tmp_path / "out",
        limit=2,
        seed=42,
        dry_run=True,
        prepare_install_fn=prepare,
    )

    assert len(prepared) == 2
    assert len(plan.jobs) == 4
    assert plan.manifest_path.is_file()
    assert not (tmp_path / "out" / "vanilla").exists()


def test_selection_instance_normalizes_missing_difficulty_without_inventing_metadata():
    from scripts.campaign_runner import _selection_instance

    assert _selection_instance({"instance_id": "one", "language": "python"}) == {
        "instance_id": "one",
        "difficulty": "unspecified",
        "language": "python",
    }
    assert _selection_instance({"instance_id": "two", "difficulty": "  custom  ", "language": "rust"}) == {
        "instance_id": "two",
        "difficulty": "  custom  ",
        "language": "rust",
    }


@pytest.mark.parametrize("difficulty", [[], True, 1])
def test_selection_instance_rejects_wrong_difficulty_types(difficulty):
    from scripts.campaign_runner import CampaignError, _selection_instance

    with pytest.raises(CampaignError, match="difficulty"):
        _selection_instance({"instance_id": "one", "difficulty": difficulty, "language": "python"})


@pytest.mark.parametrize("language", [None, "", "  ", [], True, 1])
def test_selection_instance_requires_nonempty_string_language(language):
    from scripts.campaign_runner import CampaignError, _selection_instance

    with pytest.raises(CampaignError, match="language"):
        _selection_instance({"instance_id": "one", "difficulty": "easy", "language": language})


@pytest.mark.parametrize("instance_id", [None, True, 1, "", "  "])
def test_plan_rejects_malformed_instance_id_before_fixture_or_filter(instance_id, tmp_path: Path):
    from scripts.campaign_runner import CampaignError, plan_campaign
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))

    with pytest.raises(CampaignError, match="instance_id"):
        plan_campaign(
            load_config(config_path),
            [{"instance_id": instance_id, "difficulty": "easy", "language": "python"}],
            output=tmp_path / "out",
            instance_filter="does-not-match",
            dry_run=True,
        )


def test_manifest_freezes_complete_selected_instance_records_and_resume_ignores_dataset(
    tmp_path: Path,
):
    from scripts.campaign_runner import plan_campaign
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    instance = {
        "instance_id": "owner__one.abc1234",
        "image_name": "owner-image",
        "repository": "owner/repo",
        "commit": "a" * 40,
        "difficulty": "",
        "language": "python",
    }
    plan = plan_campaign(
        load_config(config_path),
        [instance],
        output=tmp_path / "out",
        limit=1,
        seed=42,
        dry_run=True,
        prepare_install_fn=lambda arm, _root: {"arm": arm.name},
    )

    manifest = json.loads(plan.manifest_path.read_text())
    assert manifest["version"] == 2
    assert manifest["selected_instances"] == [
        {
            "instance_id": instance["instance_id"],
            "image_name": instance["image_name"],
            "repository": instance["repository"],
            "commit": instance["commit"],
            "difficulty": "unspecified",
            "language": instance["language"],
        }
    ]

    resumed = plan_campaign(
        load_config(config_path),
        [],
        output=tmp_path / "out",
        limit=1,
        seed=42,
        dry_run=True,
        resume=True,
        prepare_install_fn=lambda *_: pytest.fail("resume must not resolve installs"),
    )
    assert resumed.selected_instances == plan.selected_instances
    assert resumed.jobs == plan.jobs


@pytest.mark.parametrize(
    "mutation",
    ["missing", "none", "extra", "duplicate", "id_mismatch", "bad_type", "job_missing", "job_extra"],
)
def test_resume_rejects_malformed_selected_instance_records(tmp_path: Path, mutation: str):
    from scripts.campaign_runner import CampaignError, plan_campaign
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    instance = {
        "instance_id": "owner__one.abc1234",
        "image_name": "owner-image",
        "repository": "owner/repo",
        "commit": "a" * 40,
        "difficulty": "easy",
        "language": "python",
    }
    plan_campaign(
        load_config(config_path),
        [instance],
        output=tmp_path / "out",
        limit=1,
        dry_run=True,
        prepare_install_fn=lambda arm, _root: {"arm": arm.name},
    )
    manifest_path = tmp_path / "out" / "campaign.json"
    manifest = json.loads(manifest_path.read_text())
    if mutation == "missing":
        del manifest["selected_instances"]
    elif mutation == "none":
        manifest["selected_instances"] = None
    elif mutation == "extra":
        manifest["selected_instances"][0]["extra"] = "nope"
    elif mutation == "duplicate":
        manifest["selected_instances"].append(dict(manifest["selected_instances"][0]))
    elif mutation == "id_mismatch":
        manifest["selected_instances"][0]["instance_id"] = "owner__other.abc1234"
    elif mutation == "bad_type":
        manifest["selected_instances"][0]["commit"] = None
    elif mutation == "job_missing":
        manifest["jobs"] = manifest["jobs"][:-1]
    else:
        manifest["jobs"][0]["extra"] = "nope"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(CampaignError, match="manifest"):
        plan_campaign(
            load_config(config_path),
            [],
            output=tmp_path / "out",
            limit=1,
            dry_run=True,
            resume=True,
            prepare_install_fn=lambda *_: pytest.fail("malformed resume must fail before installs"),
        )


def test_real_dataset_selection_excludes_fixture_and_freezes_optional_difficulty(tmp_path: Path):
    from programbench.utils.load_data import load_all_instances
    from scripts.campaign_runner import plan_campaign
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    instances = load_all_instances(include_tests=False)
    assert len(instances) == 201
    assert sum(instance.get("difficulty") in (None, "") for instance in instances) == 35
    assert sum(instance["instance_id"].startswith("testorg__") for instance in instances) == 1

    plan = plan_campaign(
        load_config(config_path),
        instances,
        output=tmp_path / "out",
        limit=8,
        seed=42,
        dry_run=True,
        prepare_install_fn=lambda arm, _root: {"arm": arm.name},
    )

    assert len(plan.selected_ids) == 8
    assert len(plan.jobs) == 16
    assert not any(instance_id.startswith("testorg__") for instance_id in plan.selected_ids)
    manifest = json.loads(plan.manifest_path.read_text())
    assert len(manifest["selected_instances"]) == 8
    assert {item["difficulty"] for item in manifest["selected_instances"]} >= {"unspecified"}

    changed = [
        {**instance, "difficulty": "changed", "language": "changed"}
        for instance in instances
        if instance["instance_id"] in plan.selected_ids
    ]
    resumed = plan_campaign(
        load_config(config_path),
        changed,
        output=tmp_path / "out",
        limit=8,
        seed=42,
        dry_run=True,
        resume=True,
        prepare_install_fn=lambda *_: pytest.fail("resume must use frozen campaign metadata"),
    )
    assert resumed.selected_ids == plan.selected_ids
    assert json.loads(plan.manifest_path.read_text())["selected_instances"] == manifest["selected_instances"]

    all_plan = plan_campaign(
        load_config(config_path),
        instances,
        output=tmp_path / "all-out",
        all_instances=True,
        dry_run=True,
        prepare_install_fn=lambda arm, _root: {"arm": arm.name},
    )
    assert len(all_plan.selected_ids) == 200
    assert len(all_plan.jobs) == 400
    assert not any(instance_id.startswith("testorg__") for instance_id in all_plan.selected_ids)

    single_plan = plan_campaign(
        load_config(config_path),
        instances,
        output=tmp_path / "single-out",
        all_instances=True,
        arm_selection="procontract",
        dry_run=True,
        prepare_install_fn=lambda arm, _root: {"arm": arm.name},
    )
    assert len(single_plan.selected_ids) == 200
    assert len(single_plan.jobs) == 200
    assert {job.arm for job in single_plan.jobs} == {"procontract"}
    assert set(single_plan.installs) == {"procontract"}
    assert json.loads(single_plan.manifest_path.read_text())["selection"]["arm"] == "procontract"


def test_campaign_forwards_openai_compatible_gateway_environment() -> None:
    from scripts.campaign_runner import _CREDENTIAL_ENV

    assert _CREDENTIAL_ENV == (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_SERVER_PASSWORD",
    )


def test_all_selection_counts_only_real_benchmark_instances(tmp_path: Path):
    from scripts.campaign_runner import plan_campaign
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    instances = [
        {
            "instance_id": "real__one",
            "language": "python",
            "image_name": "one",
            "repository": "owner/one",
            "commit": "a" * 40,
        },
        {
            "instance_id": "real__two",
            "difficulty": "easy",
            "language": "rust",
            "image_name": "two",
            "repository": "owner/two",
            "commit": "b" * 40,
        },
        {
            "instance_id": "testorg__calculator.abc1234",
            "language": "python",
            "image_name": "fixture",
            "repository": "testorg/calculator",
            "commit": "c" * 40,
        },
    ]

    plan = plan_campaign(
        load_config(config_path),
        instances,
        output=tmp_path / "out",
        all_instances=True,
        dry_run=True,
        prepare_install_fn=lambda arm, _root: {"arm": arm.name},
    )

    assert set(plan.selected_ids) == {"real__one", "real__two"}


def test_plan_uses_programbench_regex_filter_and_all_after_exclusions(tmp_path: Path):
    from scripts.campaign_runner import plan_campaign
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo).replace("excluded: []", "excluded:\n  - skip__three"))
    plan = plan_campaign(
        load_config(config_path),
        [
            {
                "instance_id": "keep__one",
                "difficulty": "easy",
                "language": "python",
                "image_name": "one",
                "repository": "owner/one",
                "commit": "a" * 40,
            },
            {
                "instance_id": "keep__two",
                "difficulty": "hard",
                "language": "rust",
                "image_name": "two",
                "repository": "owner/two",
                "commit": "b" * 40,
            },
            {
                "instance_id": "skip__three",
                "difficulty": "hard",
                "language": "rust",
                "image_name": "three",
                "repository": "owner/three",
                "commit": "c" * 40,
            },
        ],
        output=tmp_path / "out",
        instance_filter="keep__.*",
        all_instances=True,
        dry_run=True,
        prepare_install_fn=lambda arm, _root: {"arm": arm.name},
    )

    assert set(plan.selected_ids) == {"keep__one", "keep__two"}


def test_resume_uses_frozen_install_data_without_resolving_moving_refs(tmp_path: Path):
    from scripts.campaign_runner import plan_campaign
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    instances = [
        {
            "instance_id": "one",
            "difficulty": "easy",
            "language": "python",
            "image_name": "one-image",
            "repository": "owner/one",
            "commit": "a" * 40,
        }
    ]
    plan_campaign(
        load_config(config_path),
        instances,
        output=tmp_path / "out",
        limit=1,
        dry_run=True,
        prepare_install_fn=lambda arm, _root: {"arm": arm.name, "revision": arm.revision, "binary_sha256": "a" * 64},
    )

    resumed = plan_campaign(
        load_config(config_path),
        instances,
        output=tmp_path / "out",
        limit=1,
        dry_run=True,
        resume=True,
        prepare_install_fn=lambda *_: pytest.fail("resume must not resolve a moving ref"),
    )

    assert len(resumed.jobs) == 2


def test_resume_rejects_changed_frozen_selection_options(tmp_path: Path):
    from scripts.campaign_runner import CampaignError, plan_campaign
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    instances = [
        {
            "instance_id": "one",
            "difficulty": "easy",
            "language": "python",
            "image_name": "one-image",
            "repository": "owner/one",
            "commit": "a" * 40,
        },
        {
            "instance_id": "two",
            "difficulty": "hard",
            "language": "rust",
            "image_name": "two-image",
            "repository": "owner/two",
            "commit": "b" * 40,
        },
    ]
    plan_campaign(
        load_config(config_path),
        instances,
        output=tmp_path / "out",
        limit=1,
        seed=42,
        dry_run=True,
        prepare_install_fn=lambda arm, _root: {"arm": arm.name, "revision": arm.revision, "binary_sha256": "a" * 64},
    )

    with pytest.raises(CampaignError, match="selection"):
        plan_campaign(
            load_config(config_path),
            instances,
            output=tmp_path / "out",
            limit=2,
            seed=42,
            dry_run=True,
            resume=True,
            prepare_install_fn=lambda *_: pytest.fail("resume must not resolve installs"),
        )


def test_resume_acknowledges_recoverable_breaker_pause_but_not_hard_stop(tmp_path: Path):
    from scripts.campaign_runner import plan_campaign
    from scripts.campaign_usage import TurnUsage, UsageSummary
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    instances = [
        {
            "instance_id": "one",
            "difficulty": "easy",
            "language": "python",
            "image_name": "one",
            "repository": "owner/one",
            "commit": "a" * 40,
        }
    ]
    config = load_config(config_path)
    plan_campaign(
        config,
        instances,
        output=tmp_path / "out",
        limit=1,
        dry_run=True,
        prepare_install_fn=lambda arm, _: {"arm": arm.name},
    )
    breaker = CircuitBreaker()
    breaker.observe_error(401)
    (tmp_path / "out/breaker.json").write_text(json.dumps(breaker.snapshot()))

    plan_campaign(config, instances, output=tmp_path / "out", limit=1, dry_run=True, resume=True, acknowledge="auth")

    plan_campaign(
        config,
        instances,
        output=tmp_path / "hard",
        limit=1,
        dry_run=True,
        prepare_install_fn=lambda arm, _: {"arm": arm.name},
    )
    turn = TurnUsage(input=1, prompt_total=1, cost=Decimal("2"))
    hard = CircuitBreaker(hard_cost_cap="2")
    hard.observe(
        "hard",
        0,
        UsageSummary(
            provider_turns=1,
            input=1,
            prompt_total=1,
            turns_without_cache=1,
            max_prompt_total=1,
            cost=Decimal("2"),
            turns=(turn,),
        ),
    )
    (tmp_path / "hard/breaker.json").write_text(json.dumps(hard.snapshot()))

    cleanup = plan_campaign(
        config,
        instances,
        output=tmp_path / "hard",
        limit=1,
        dry_run=True,
        resume=True,
        acknowledge="all",
    )
    assert cleanup.cleanup_only
    assert CircuitBreaker.from_snapshot(json.loads((tmp_path / "hard/breaker.json").read_text())).hard_stopped


def test_persisted_hard_stop_resume_runs_cleanup_only_without_admission(tmp_path: Path, monkeypatch):
    from scripts import campaign_runner
    from scripts.campaign_usage import TurnUsage, UsageSummary
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    config = load_config(config_path)
    instances = [
        {
            "instance_id": "one",
            "difficulty": "easy",
            "language": "python",
            "image_name": "one",
            "repository": "owner/one",
            "commit": "a" * 40,
        }
    ]
    plan = campaign_runner.plan_campaign(
        config,
        instances,
        output=tmp_path / "out",
        limit=1,
        prepare_install_fn=lambda arm, _: {"arm": arm.name},
    )
    breaker = CircuitBreaker(hard_cost_cap="1")
    turn = TurnUsage(input=1, prompt_total=1, cost=Decimal("1"))
    breaker.observe(
        "hard",
        0,
        UsageSummary(
            provider_turns=1,
            input=1,
            prompt_total=1,
            turns_without_cache=1,
            max_prompt_total=1,
            cost=Decimal("1"),
            turns=(turn,),
        ),
    )
    (plan.output / "breaker.json").write_text(json.dumps(breaker.snapshot()))
    terminal = plan.jobs[0]
    for candidate in plan.jobs:
        paths = campaign_runner.JobPaths(plan.output / candidate.arm / candidate.instance_id)
        store = campaign_runner.StateStore(paths.state)
        state = store.write(campaign_runner.JobState("planned"))
        if candidate == terminal:
            store.transition(
                "failed",
                command_identity="prior-cleanup",
                expected_generation=state.generation,
                exit_class="cleanup_failed",
            )
        paths.root.joinpath("cleanup-failure.json").write_text('{"status":"prior_force_failed"}\n')

    resumed = campaign_runner.plan_campaign(
        config,
        instances,
        output=plan.output,
        limit=1,
        resume=True,
        prepare_install_fn=lambda *_: pytest.fail("cleanup resume cannot resolve moving refs"),
    )
    monkeypatch.setattr(campaign_runner, "_install_for", lambda frozen, arm: frozen.installs[arm])
    calls = []
    workspaces = []

    class Workspace:
        base_url = "http://127.0.0.1:9999"
        port = 9999

        def __init__(self, request, lease):
            self.request = request
            self.lease = lease
            lease.claim_handle(request)

        def stop_for_freeze(self):
            self.request.job.root.joinpath("child-stop-observed").write_text("stopped")

    def workspace_factory(request, lease):
        workspace = Workspace(request, lease)
        workspaces.append(workspace)
        return workspace

    def adapter(_job, store, workspace, *_args, **kwargs):
        assert kwargs["stop_token"].is_set()
        calls.append((workspace.request.instance_id, workspace.request.arm))
        state = store.read()
        store.transition(
            "interrupted",
            command_identity=state.command_identity or "cleanup",
            expected_generation=state.generation,
            exit_class="hard_stop_cleanup",
        )
        workspace.stop_for_freeze()

    result = campaign_runner.run_cleanup_campaign(
        resumed,
        breaker,
        workspace_factory=workspace_factory,
        vanilla_runner=adapter,
        procontract_runner=adapter,
        vanilla_api_factory=lambda _: object(),
        procontract_api_factory=lambda _: object(),
    )

    assert resumed.cleanup_only
    assert result.paused
    assert result.interrupted
    assert result.failed == 0
    assert set(calls) == {(candidate.instance_id, candidate.arm) for candidate in resumed.jobs if candidate != terminal}
    assert len(workspaces) == 2
    assert all(workspace.request.job.root.joinpath("child-stop-observed").is_file() for workspace in workspaces)
    assert breaker.hard_stopped
    for candidate in resumed.jobs:
        state = campaign_runner.StateStore(resumed.output / candidate.arm / candidate.instance_id / "state.json").read()
        assert state.phase == "interrupted"
        assert state.exit_class == ("cleanup_recovered" if candidate == terminal else "hard_stop_cleanup")

    before = len(workspaces)
    second = campaign_runner.run_cleanup_campaign(
        resumed,
        breaker,
        workspace_factory=lambda *_: pytest.fail("cleanup_recovered job cannot reacquire workspace"),
        vanilla_runner=lambda *_args, **_kwargs: pytest.fail("cleanup_recovered job cannot rerun adapter"),
        procontract_runner=lambda *_args, **_kwargs: pytest.fail("cleanup_recovered job cannot rerun adapter"),
    )
    assert second.failed == 0
    assert len(workspaces) == before


@pytest.mark.parametrize("phase", ["planned", "preparing", "executing", "frozen", "packaged", "preflighted"])
def test_resume_preserves_unfinished_state_without_reset(tmp_path: Path, monkeypatch, phase: str):
    from scripts import campaign_runner
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    plan = campaign_runner.CampaignPlan(
        tmp_path / "out", tmp_path / "manifest", load_config(config_path), ("one",), (), {}, False
    )
    candidate = campaign_runner.CampaignJob("one", "vanilla", "one-image")
    paths = campaign_runner.JobPaths(plan.output / candidate.arm / candidate.instance_id)
    store = campaign_runner.StateStore(paths.state)
    store.write(campaign_runner.JobState("planned"))
    while store.read().phase != phase:
        state = store.read()
        store.transition(store.next_phase(state), command_identity="resume", expected_generation=state.generation)
    before = store.read().generation
    monkeypatch.setattr(campaign_runner, "_install_for", lambda *_: object())

    class Workspace:
        base_url = "http://127.0.0.1:9999"
        port = 9999

        def stop_for_freeze(self):
            pass

    seen = []
    campaign_runner.run_job(
        plan,
        candidate,
        breaker=CircuitBreaker(),
        resume=True,
        workspace_factory=lambda *_: Workspace(),
        vanilla_runner=lambda _lease, state, *_args, **_kwargs: seen.append(state.read().generation),
        vanilla_api_factory=lambda _: object(),
    )

    assert seen == [before]


def test_run_job_dispatches_vanilla_once_and_skips_a_terminal_retry(tmp_path: Path, monkeypatch):
    from scripts import campaign_runner
    from scripts.run_opencode import load_config

    config_path = tmp_path / "campaign.yaml"
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path.write_text(_config(repo))
    plan = campaign_runner.CampaignPlan(
        tmp_path / "out", tmp_path / "out/campaign.json", load_config(config_path), ("one",), (), {}, False
    )
    candidate = campaign_runner.CampaignJob("one", "vanilla", "one-image")
    calls = []

    class Workspace:
        base_url = "http://127.0.0.1:9999"
        port = 9999

        def stop_for_freeze(self):
            calls.append("stop")

    def vanilla(job_lease, store, _workspace, _install, **_kwargs):
        calls.append("vanilla")
        store.transition("failed", command_identity="test", expected_generation=store.read().generation)
        return "result"

    monkeypatch.setattr(campaign_runner, "_install_for", lambda *_: object())
    result = campaign_runner.run_job(
        plan,
        candidate,
        breaker=CircuitBreaker(),
        workspace_factory=lambda _request, _lease: Workspace(),
        vanilla_runner=vanilla,
        vanilla_api_factory=lambda _workspace: object(),
    )
    duplicate = campaign_runner.run_job(
        plan,
        candidate,
        breaker=CircuitBreaker(),
        workspace_factory=lambda *_: pytest.fail("duplicate should not allocate a workspace"),
        vanilla_runner=lambda *_args, **_kwargs: pytest.fail("duplicate should not create a Session"),
    )

    assert result.status == "completed"
    assert duplicate.status == "skipped"
    assert calls == ["vanilla"]


def test_run_job_packages_and_preflights_a_frozen_vanilla_result(tmp_path: Path, monkeypatch):
    from programbench.candidate import PreflightResult
    from scripts import campaign_runner
    from scripts.campaign_vanilla import VanillaCompletion, VanillaResult
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    plan = campaign_runner.CampaignPlan(
        tmp_path / "out", tmp_path / "out/campaign.json", load_config(config_path), ("one",), (), {}, False
    )
    candidate = campaign_runner.CampaignJob("one", "vanilla", "one-image")
    paths = campaign_runner.JobPaths(plan.output / candidate.arm / candidate.instance_id)
    subject = paths.root / "subject"
    preflights = []

    class Workspace:
        base_url = "http://127.0.0.1:9999"
        port = 9999
        manifest = SimpleNamespace(subject_dir=subject)

        def __init__(self, lease):
            self.lease = lease

        def stop_for_freeze(self):
            self.lease.release_handle()

    def workspace_factory(request, lease):
        lease.claim_handle(request)
        return Workspace(lease)

    result = VanillaResult(
        session_id="ses_test",
        message_id="msg_test",
        completion=VanillaCompletion.IDLE,
        provider_turns=1,
        actions=0,
        cost=Decimal(0),
        elapsed_seconds=1,
        input_tokens=1,
        output_tokens=1,
        reasoning_tokens=0,
        cache_read=0,
        cache_write=0,
        max_prompt_total=1,
        limit_overshoot=0,
        workspace_frozen=True,
        container_stopped_for_freeze=True,
        interrupt_attempted=False,
        forced_stop=False,
        exit_class="idle",
    )

    def vanilla(_job, store, workspace, *_args, **_kwargs):
        subject.mkdir(parents=True)
        subject.joinpath("compile.sh").write_text("#!/bin/sh\nprintf built > executable\nchmod +x executable\n")
        subject.joinpath("source").write_text("candidate\n")
        result_path = paths.root / "vanilla-result.json"
        result_path.write_text(json.dumps(result.to_dict(), sort_keys=True))
        state = store.read()
        for phase in ("preparing", "executing"):
            state = store.transition(phase, command_identity="vanilla", expected_generation=state.generation)
        store.transition(
            "frozen",
            command_identity="vanilla",
            expected_generation=state.generation,
            provider_turns=1,
            exit_class="idle",
            artifact_hashes={"vanilla-result.json": hashlib.sha256(result_path.read_bytes()).hexdigest()},
        )
        workspace.stop_for_freeze()
        return result

    monkeypatch.setattr(campaign_runner, "_install_for", lambda *_: object())
    monkeypatch.setattr(
        campaign_runner,
        "preflight_candidate",
        lambda archive, instance_id: (
            preflights.append((archive, instance_id)),
            PreflightResult("e" * 64, "preflight-ok"),
        )[1],
        raising=False,
    )

    outcome = campaign_runner.run_job(
        plan,
        candidate,
        breaker=CircuitBreaker(),
        workspace_factory=workspace_factory,
        vanilla_runner=vanilla,
        vanilla_api_factory=lambda _: object(),
    )

    state = campaign_runner.StateStore(paths.state).read()
    assert outcome == campaign_runner.JobRunResult(candidate, "completed", result)
    assert state.phase == "done"
    assert state.exit_class == "idle"
    assert paths.submission.is_file()
    assert preflights == [(paths.submission, "one")]
    assert paths.root.joinpath("vanilla-preflight.json").is_file()


@pytest.mark.parametrize("arm", ["vanilla", "procontract"])
def test_run_job_passes_campaign_stop_token_to_adapter(tmp_path: Path, monkeypatch, arm: str):
    from scripts import campaign_runner
    from scripts.campaign_stop import StopToken
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    plan = campaign_runner.CampaignPlan(
        tmp_path / "out", tmp_path / "out/campaign.json", load_config(config_path), ("one",), (), {}, False
    )
    token = StopToken()
    seen = []

    class Workspace:
        base_url = "http://127.0.0.1:9999"
        port = 9999

        def stop_for_freeze(self):
            return None

    def adapter(*_args, **kwargs):
        seen.append(kwargs["stop_token"])

    monkeypatch.setattr(campaign_runner, "_install_for", lambda *_: object())
    result = campaign_runner.run_job(
        plan,
        campaign_runner.CampaignJob("one", arm, "one-image"),
        breaker=CircuitBreaker(),
        stop_token=token,
        workspace_factory=lambda *_: Workspace(),
        vanilla_runner=adapter,
        procontract_runner=adapter,
        vanilla_api_factory=lambda _: object(),
        procontract_api_factory=lambda _: object(),
    )

    assert result.status == "completed"
    assert seen == [token]


def test_run_job_selects_semantic_protocol_from_frozen_install_revision(tmp_path: Path, monkeypatch):
    from scripts import campaign_runner
    from scripts.campaign_procontract import SEMANTIC_ATTEMPTS_REVISION
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    plan = campaign_runner.CampaignPlan(
        tmp_path / "out", tmp_path / "out/campaign.json", load_config(config_path), ("one",), (), {}, False
    )
    seen = []

    class Workspace:
        base_url = "http://127.0.0.1:9999"
        port = 9999

        def stop_for_freeze(self):
            return None

    def adapter(*_args, **kwargs):
        seen.append(kwargs["protocol"])

    monkeypatch.setattr(
        campaign_runner, "_install_for", lambda *_: SimpleNamespace(revision=SEMANTIC_ATTEMPTS_REVISION)
    )
    result = campaign_runner.run_job(
        plan,
        campaign_runner.CampaignJob("one", "procontract", "one-image"),
        breaker=CircuitBreaker(),
        workspace_factory=lambda *_: Workspace(),
        procontract_runner=adapter,
        procontract_api_factory=lambda _: object(),
    )

    assert result.status == "completed"
    assert seen == ["semantic-v0"]


def test_run_job_with_preexisting_campaign_stop_never_allocates_workspace(tmp_path: Path):
    from scripts import campaign_runner
    from scripts.campaign_stop import StopToken
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    plan = campaign_runner.CampaignPlan(
        tmp_path / "out", tmp_path / "out/campaign.json", load_config(config_path), ("one",), (), {}, False
    )
    token = StopToken()
    token.set("keyboard_interrupt")

    result = campaign_runner.run_job(
        plan,
        campaign_runner.CampaignJob("one", "vanilla", "one-image"),
        breaker=CircuitBreaker(),
        stop_token=token,
        workspace_factory=lambda *_: pytest.fail("stopped campaign cannot allocate a workspace"),
    )

    state = campaign_runner.StateStore(tmp_path / "out/vanilla/one/state.json").read()
    assert result.status == "interrupted"
    assert state.phase == "interrupted"
    assert state.exit_class == "campaign_stop"


def test_run_job_registers_exact_workspace_and_cas_terminalizes_forced_stop(tmp_path: Path, monkeypatch):
    from scripts import campaign_runner
    from scripts.campaign_stop import StopToken
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    plan = campaign_runner.CampaignPlan(
        tmp_path / "out", tmp_path / "out/campaign.json", load_config(config_path), ("one",), (), {}, False
    )
    candidate = campaign_runner.CampaignJob("one", "vanilla", "one-image")
    token = StopToken()
    started = threading.Event()
    released = threading.Event()
    results = []
    stops = []

    class Workspace:
        base_url = "http://127.0.0.1:9999"
        port = 9999

        def stop_for_freeze(self):
            stops.append(candidate)
            released.set()

    def adapter(*_args, **_kwargs):
        started.set()
        assert released.wait(1)

    monkeypatch.setattr(campaign_runner, "_install_for", lambda *_: object())
    thread = threading.Thread(
        target=lambda: results.append(
            campaign_runner.run_job(
                plan,
                candidate,
                breaker=CircuitBreaker(),
                stop_token=token,
                workspace_factory=lambda *_: Workspace(),
                vanilla_runner=adapter,
                vanilla_api_factory=lambda _: object(),
            )
        )
    )
    thread.start()
    try:
        assert started.wait(1)
        assert token.registry.active_jobs() == (candidate,)
        token.set("keyboard_interrupt")
        token.registry.force_stop_all()
        thread.join(1)
    finally:
        released.set()
        thread.join(1)

    state = campaign_runner.StateStore(tmp_path / "out/vanilla/one/state.json").read()
    assert results == [campaign_runner.JobRunResult(candidate, "interrupted")]
    assert stops == [candidate]
    assert state.phase == "interrupted"
    assert state.exit_class == "campaign_stop"
    assert token.registry.active_jobs() == ()


def test_run_job_preserves_native_terminal_result_observed_during_campaign_stop(tmp_path: Path, monkeypatch):
    from scripts import campaign_runner
    from scripts.campaign_stop import StopToken
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    plan = campaign_runner.CampaignPlan(
        tmp_path / "out", tmp_path / "out/campaign.json", load_config(config_path), ("one",), (), {}, False
    )
    candidate = campaign_runner.CampaignJob("one", "vanilla", "one-image")
    token = StopToken()

    class Workspace:
        base_url = "http://127.0.0.1:9999"
        port = 9999

        def stop_for_freeze(self):
            return None

    def adapter(_job_lease, store, *_args, **_kwargs):
        state = store.read()
        for phase in ("preparing", "executing"):
            state = store.transition(phase, command_identity="native", expected_generation=state.generation)
        store.transition(
            "frozen",
            command_identity="native",
            expected_generation=state.generation,
            exit_class="idle",
            artifact_hashes={"native-result.json": "a" * 64},
        )
        token.set("keyboard_interrupt")
        return "native-result"

    monkeypatch.setattr(campaign_runner, "_install_for", lambda *_: object())
    result = campaign_runner.run_job(
        plan,
        candidate,
        breaker=CircuitBreaker(),
        stop_token=token,
        workspace_factory=lambda *_: Workspace(),
        vanilla_runner=adapter,
        vanilla_api_factory=lambda _: object(),
    )

    state = campaign_runner.StateStore(tmp_path / "out/vanilla/one/state.json").read()
    assert result == campaign_runner.JobRunResult(candidate, "completed", "native-result")
    assert state.phase == "frozen"
    assert state.exit_class == "idle"
    assert state.artifact_hashes == {"native-result.json": "a" * 64}


def test_force_terminal_cas_preserves_native_frozen_state_and_artifact_hashes(tmp_path: Path):
    from scripts import campaign_runner

    store = campaign_runner.StateStore(tmp_path / "state.json")
    state = store.write(campaign_runner.JobState("planned"))
    for phase in ("preparing", "executing"):
        state = store.transition(phase, command_identity="native", expected_generation=state.generation)
    store.transition(
        "frozen",
        command_identity="native",
        expected_generation=state.generation,
        exit_class="idle",
        artifact_hashes={"native-result.json": "a" * 64},
    )

    assert campaign_runner._terminalize(store, "interrupted", "campaign_stop")
    assert store.read().phase == "frozen"
    assert store.read().exit_class == "idle"
    assert store.read().artifact_hashes == {"native-result.json": "a" * 64}


def test_run_job_normalizes_unexpected_error_without_secret(tmp_path: Path, monkeypatch):
    from scripts import campaign_runner
    from scripts.run_opencode import load_config

    config_path = tmp_path / "campaign.yaml"
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path.write_text(_config(repo))
    plan = campaign_runner.CampaignPlan(
        tmp_path / "out", tmp_path / "out/campaign.json", load_config(config_path), ("one",), (), {}, False
    )
    monkeypatch.setattr(campaign_runner, "_install_for", lambda *_: object())

    result = campaign_runner.run_job(
        plan,
        campaign_runner.CampaignJob("one", "vanilla", "one-image"),
        breaker=CircuitBreaker(),
        workspace_factory=lambda *_: (_ for _ in ()).throw(RuntimeError("secret-value")),
    )

    state = campaign_runner.StateStore(tmp_path / "out/vanilla/one/state.json").read()
    assert result.status == "failed"
    assert state.phase == "failed"
    assert "secret-value" not in (state.exit_class or "")


def test_run_job_force_stops_before_unregistering_unexpected_adapter_failure(tmp_path: Path, monkeypatch):
    from scripts import campaign_runner
    from scripts.campaign_stop import StopToken
    from scripts.run_opencode import load_config

    config_path = tmp_path / "campaign.yaml"
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path.write_text(_config(repo))
    plan = campaign_runner.CampaignPlan(
        tmp_path / "out", tmp_path / "out/campaign.json", load_config(config_path), ("one",), (), {}, False
    )
    candidate = campaign_runner.CampaignJob("one", "vanilla", "one-image")
    token = StopToken()
    workspaces = []

    class Workspace:
        base_url = "http://127.0.0.1:9999"
        port = 9999

        def __init__(self, request, lease):
            self.request = request
            self.lease = lease
            self.stops = 0
            lease.claim_handle(request)

        def stop_for_freeze(self):
            assert self.lease._lock_context is not None
            assert self.lease._handle_active
            assert token.registry.active_jobs() == (candidate,)
            self.request.job.root.joinpath("child-stop-observed").write_text("stopped")

    def workspace_factory(request, lease):
        workspace = Workspace(request, lease)
        workspaces.append(workspace)
        return workspace

    def adapter(_job, store, *_args, **_kwargs):
        state = store.read()
        state = store.transition("preparing", command_identity="adapter", expected_generation=state.generation)
        store.transition("executing", command_identity="adapter", expected_generation=state.generation)
        raise RuntimeError("adapter-secret")

    monkeypatch.setattr(campaign_runner, "_install_for", lambda *_: object())

    result = campaign_runner.run_job(
        plan,
        candidate,
        breaker=CircuitBreaker(),
        stop_token=token,
        workspace_factory=workspace_factory,
        vanilla_runner=adapter,
        vanilla_api_factory=lambda _: object(),
    )

    state = campaign_runner.StateStore(plan.output / "vanilla/one/state.json").read()
    assert result.status == "failed"
    assert state.phase == "failed"
    assert state.exit_class == "execution_failed"
    assert "adapter-secret" not in (state.exit_class or "")
    assert workspaces[0].request.job.root.joinpath("child-stop-observed").is_file()
    assert not workspaces[0].lease._handle_active
    assert token.registry.active_jobs() == ()


def test_run_job_retries_transient_cleanup_without_stopping_campaign(tmp_path: Path, monkeypatch):
    from scripts import campaign_runner
    from scripts.run_opencode import load_config

    config_path = tmp_path / "campaign.yaml"
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path.write_text(_config(repo))
    plan = campaign_runner.CampaignPlan(
        tmp_path / "out", tmp_path / "out/campaign.json", load_config(config_path), ("one",), (), {}, False
    )
    candidate = campaign_runner.CampaignJob("one", "vanilla", "one-image")
    workspaces = []

    class Workspace:
        base_url = "http://127.0.0.1:9999"
        port = 9999

        def __init__(self, request, lease):
            self.request = request
            self.lease = lease
            lease.claim_handle(request)

        def stop_for_freeze(self):
            path = self.request.job.root / "cleanup-attempts"
            attempts = int(path.read_text()) + 1 if path.is_file() else 1
            path.write_text(str(attempts))
            if attempts == 1:
                raise RuntimeError("transient stop failure")

    def workspace_factory(request, lease):
        workspace = Workspace(request, lease)
        workspaces.append(workspace)
        return workspace

    monkeypatch.setattr(campaign_runner, "_install_for", lambda *_: object())
    result = campaign_runner.run_campaign(
        (candidate,),
        workers=1,
        interrupt_timeout=0.05,
        execute=lambda current, stop: campaign_runner.run_job(
            plan,
            current,
            breaker=CircuitBreaker(),
            stop_token=stop,
            workspace_factory=workspace_factory,
            vanilla_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("adapter failed")),
            vanilla_api_factory=lambda _: object(),
        ),
    )

    state = campaign_runner.StateStore(plan.output / "vanilla/one/state.json").read()
    assert not result.interrupted
    assert result.failed == 1
    assert state.phase == "failed"
    assert state.exit_class == "infrastructure_failed_before_provider"
    assert workspaces[0].request.job.root.joinpath("cleanup-attempts").read_text() == "2"
    assert not workspaces[0].lease._handle_active


def test_persistent_workspace_cleanup_failure_is_bounded_and_durable(tmp_path: Path, monkeypatch):
    from scripts import campaign_runner
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    plan = campaign_runner.CampaignPlan(
        tmp_path / "out", tmp_path / "out/campaign.json", load_config(config_path), ("one",), (), {}, False
    )
    candidate = campaign_runner.CampaignJob("one", "vanilla", "one-image")
    workspaces = []

    class Workspace:
        base_url = "http://127.0.0.1:9999"
        port = 9999

        def __init__(self, request, lease):
            self.request = request
            self.lease = lease
            self.container_id = "a" * 64
            lease.claim_handle(request)

        def stop_for_freeze(self):
            attempts = self.request.job.root / "cleanup-attempts"
            attempts.mkdir(exist_ok=True)
            attempts.joinpath(str(campaign_runner.os.getpid())).write_text("attempted")
            raise RuntimeError("cleanup-secret")

    def workspace_factory(request, lease):
        workspace = Workspace(request, lease)
        workspaces.append(workspace)
        return workspace

    monkeypatch.setattr(campaign_runner, "_install_for", lambda *_: object())
    started = time.monotonic()
    with pytest.raises(campaign_runner.CampaignShutdownError):
        campaign_runner.run_campaign(
            (candidate,),
            workers=1,
            interrupt_timeout=0.02,
            execute=lambda current, stop: campaign_runner.run_job(
                plan,
                current,
                breaker=CircuitBreaker(),
                stop_token=stop,
                workspace_factory=workspace_factory,
                vanilla_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("adapter-secret")),
                vanilla_api_factory=lambda _: object(),
            ),
        )

    evidence = json.loads((plan.output / "vanilla/one/cleanup-failure.json").read_text())
    state = campaign_runner.StateStore(plan.output / "vanilla/one/state.json").read()
    assert time.monotonic() - started < 0.5
    attempts = tuple(workspaces[0].request.job.root.joinpath("cleanup-attempts").iterdir())
    assert len(attempts) == campaign_runner.JOB_CLEANUP_MAX_ATTEMPTS
    assert str(campaign_runner.os.getpid()) not in {path.name for path in attempts}
    assert evidence == {
        "version": 1,
        "attempts": campaign_runner.JOB_CLEANUP_MAX_ATTEMPTS,
        "container_ids": ["a" * 64],
        "status": "cleanup_failed",
    }
    assert state.phase == "failed"
    assert state.exit_class == "cleanup_failed"
    assert "secret" not in (plan.output / "vanilla/one/cleanup-failure.json").read_text()


def test_blocking_workspace_stop_hits_cleanup_deadline_without_worker_spin(tmp_path: Path, monkeypatch):
    from scripts import campaign_runner
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    plan = campaign_runner.CampaignPlan(
        tmp_path / "out", tmp_path / "out/campaign.json", load_config(config_path), ("one",), (), {}, False
    )
    candidate = campaign_runner.CampaignJob("one", "vanilla", "one-image")
    read_descriptor, write_descriptor = campaign_runner.os.pipe()

    class Workspace:
        base_url = "http://127.0.0.1:9999"
        port = 9999
        container_id = "a" * 64

        def __init__(self, request, lease):
            self.request = request
            self.lease = lease
            lease.claim_handle(request)

        def stop_for_freeze(self):
            pid = campaign_runner.os.getpid()
            identity = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19]
            workers = self.request.job.root / "cleanup-workers"
            workers.mkdir(exist_ok=True)
            workers.joinpath(str(pid)).write_text(identity)
            campaign_runner.os.read(read_descriptor, 1)
            self.request.job.root.joinpath("late-cleanup-write").write_text("unsafe")
            raise RuntimeError("blocked stop failed")

    monkeypatch.setattr(campaign_runner, "_install_for", lambda *_: object())
    started = time.monotonic()
    try:
        with pytest.raises(campaign_runner.CampaignShutdownError):
            campaign_runner.run_campaign(
                (candidate,),
                workers=1,
                interrupt_timeout=0.02,
                execute=lambda current, stop: campaign_runner.run_job(
                    plan,
                    current,
                    breaker=CircuitBreaker(),
                    stop_token=stop,
                    workspace_factory=lambda request, lease: Workspace(request, lease),
                    vanilla_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("adapter failed")),
                    vanilla_api_factory=lambda _: object(),
                ),
            )
    finally:
        campaign_runner.os.write(write_descriptor, b"release")
        campaign_runner.os.close(write_descriptor)
        campaign_runner.os.close(read_descriptor)

    assert time.monotonic() - started < 0.8
    evidence = json.loads((plan.output / "vanilla/one/cleanup-failure.json").read_text())
    assert evidence["attempts"] == 1
    assert campaign_runner.StateStore(plan.output / "vanilla/one/state.json").read().exit_class == "cleanup_failed"
    workers = tuple((plan.output / "vanilla/one/cleanup-workers").iterdir())
    assert len(workers) == 1
    child_pid = int(workers[0].name)
    child_identity = workers[0].read_text()
    assert child_pid != campaign_runner.os.getpid()
    try:
        current_identity = Path(f"/proc/{child_pid}/stat").read_text().rpartition(")")[2].split()[19]
    except FileNotFoundError:
        current_identity = None
    assert current_identity != child_identity
    assert not (plan.output / "vanilla/one/late-cleanup-write").exists()


def test_retry_infra_restarts_a_normalized_pre_provider_failure(tmp_path: Path, monkeypatch):
    from scripts import campaign_runner
    from scripts.run_opencode import load_config

    config_path = tmp_path / "campaign.yaml"
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path.write_text(_config(repo))
    plan = campaign_runner.CampaignPlan(
        tmp_path / "out", tmp_path / "out/campaign.json", load_config(config_path), ("one",), (), {}, False
    )
    candidate = campaign_runner.CampaignJob("one", "vanilla", "one-image")
    monkeypatch.setattr(campaign_runner, "_install_for", lambda *_: object())
    campaign_runner.run_job(
        plan,
        candidate,
        breaker=CircuitBreaker(),
        workspace_factory=lambda *_: (_ for _ in ()).throw(RuntimeError("infra")),
    )
    retried = []

    class Workspace:
        base_url = "http://127.0.0.1:9999"
        port = 9999

        def stop_for_freeze(self):
            return None

    result = campaign_runner.run_job(
        plan,
        candidate,
        breaker=CircuitBreaker(),
        retry_infra=True,
        workspace_factory=lambda *_: Workspace(),
        vanilla_runner=lambda *_args, **_kwargs: retried.append("vanilla"),
        vanilla_api_factory=lambda _workspace: object(),
    )

    assert result.status == "completed"
    assert retried == ["vanilla"]


def test_resume_refuses_an_install_record_that_differs_from_manifest(tmp_path: Path, monkeypatch):
    from scripts import campaign_runner
    from scripts import campaign_install

    plan = campaign_runner.CampaignPlan(
        tmp_path, tmp_path / "campaign.json", object(), (), (), {"vanilla": {"revision": "pinned"}}, False
    )
    monkeypatch.setattr(campaign_install, "load_install_record", lambda _path: {"revision": "moved"})

    with pytest.raises(campaign_runner.CampaignError, match="frozen install"):
        campaign_runner._install_for(plan, "vanilla")


@pytest.mark.parametrize("workers", [2, 8])
def test_fake_paired_campaign_script_main_resumes_without_adapter_calls(tmp_path: Path, monkeypatch, workers: int):
    from scripts import campaign_evaluate, campaign_runner

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    instances = [
        {
            "instance_id": "one",
            "difficulty": "easy",
            "language": "python",
            "image_name": "one-image",
            "repository": "owner/one",
            "commit": "a" * 40,
        },
        {
            "instance_id": "two",
            "difficulty": "hard",
            "language": "rust",
            "image_name": "two-image",
            "repository": "owner/two",
            "commit": "b" * 40,
        },
    ]
    prepared = []

    def prepare(arm, root):
        prepared.append((arm.name, arm.revision, root))
        return {"arm": arm.name, "revision": f"frozen-{arm.name}", "binary_sha256": arm.name[0] * 64}

    original_plan_campaign = campaign_runner.plan_campaign
    plans = []

    def plan_campaign(*args, **kwargs):
        plan = original_plan_campaign(*args, **kwargs, prepare_install_fn=prepare)
        plans.append(plan)
        return plan

    load_calls = []

    def load_instances(**_kwargs):
        load_calls.append(True)
        if len(load_calls) > 1:
            raise AssertionError("resume must not reload ProgramBench instances")
        return instances

    monkeypatch.setattr(campaign_runner, "load_all_instances", load_instances)
    monkeypatch.setattr(campaign_runner, "plan_campaign", plan_campaign)
    monkeypatch.setattr(campaign_runner, "_install_for", lambda frozen, arm: frozen.installs[arm])
    active = 0
    maximum = 0
    lock = threading.Lock()
    calls = []
    workspaces = []

    class Workspace:
        def __init__(self, request, lease):
            self.base_url = "http://127.0.0.1:9999"
            self.port = 9999
            self.request = request
            self.lease = lease
            self.stops = 0
            lease.claim_handle(request)

        def stop_for_freeze(self):
            self.stops += 1
            self.lease.release_handle()

    def workspace_factory(request, lease):
        workspace = Workspace(request, lease)
        workspaces.append(workspace)
        return workspace

    def adapter(job_lease, store, workspace, *_args, **_kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            calls.append((workspace.request.instance_id, workspace.request.arm))
        try:
            time.sleep(0.03)
            state = store.read()
            for phase in ("preparing", "executing", "frozen", "packaged", "preflighted"):
                state = store.transition(phase, command_identity="fake-adapter", expected_generation=state.generation)
            job_lease.job.submission.write_bytes(f"{workspace.request.instance_id}:{workspace.request.arm}".encode())
            job_lease.job.root.joinpath("usage.json").write_text('{"cost":"0"}\n')
            result_path = job_lease.job.root / f"{workspace.request.arm}-result.json"
            result = {
                "version": 1,
                "completion": "discharged" if workspace.request.arm == "procontract" else "idle",
                "provider_turns": 3,
                "actions": 4,
                "cost": "1.25",
                "elapsed_seconds": 12.5,
                "input_tokens": 2,
                "cache_read": 6,
                "cache_write": 2,
                "exit_class": "discharged" if workspace.request.arm == "procontract" else "idle",
                "executable_hash": "e" * 64,
            }
            if workspace.request.arm == "procontract":
                result.update(
                    {
                        "status": "discharged",
                        "quiet": True,
                        "outstanding": [],
                        "package_hash": hashlib.sha256(job_lease.job.submission.read_bytes()).hexdigest(),
                    }
                )
            result_path.write_text(json.dumps(result, sort_keys=True))
            store.transition(
                "done",
                command_identity="fake-adapter",
                expected_generation=state.generation,
                provider_turns=3,
                actions=4,
                cost=1.25,
                exit_class=result["exit_class"],
                artifact_hashes={
                    "submission.tar.gz": hashlib.sha256(job_lease.job.submission.read_bytes()).hexdigest(),
                    result_path.name: hashlib.sha256(result_path.read_bytes()).hexdigest(),
                },
            )
            workspace.stop_for_freeze()
        finally:
            with lock:
                active -= 1

    original_run_job = campaign_runner.run_job

    def run_job(plan, candidate, **kwargs):
        return original_run_job(
            plan,
            candidate,
            **kwargs,
            workspace_factory=(
                (lambda *_: pytest.fail("terminal resume cannot allocate workspace"))
                if kwargs.get("resume")
                else workspace_factory
            ),
            vanilla_runner=adapter,
            procontract_runner=adapter,
            vanilla_api_factory=lambda _: object(),
            procontract_api_factory=lambda _: object(),
        )

    monkeypatch.setattr(campaign_runner, "run_job", run_job)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "test-password")
    output = tmp_path / "out"

    assert (
        campaign_runner.main(
            [
                "--config",
                str(config_path),
                "--output",
                str(output),
                "--workers",
                str(workers),
                "--limit",
                "2",
                "--seed",
                "42",
            ]
        )
        == 0
    )

    plan = plans[0]

    assert maximum == min(workers, 4)
    assert set(calls) == {(instance, arm) for instance in ("one", "two") for arm in ("vanilla", "procontract")}
    assert len(prepared) == 2
    assert len(workspaces) == 4
    assert all(workspace.stops == 1 for workspace in workspaces)
    for candidate in plan.jobs:
        paths = campaign_runner.JobPaths(plan.output / candidate.arm / candidate.instance_id)
        assert campaign_runner.StateStore(paths.state).read().phase == "done"
        assert paths.submission.read_bytes() == f"{candidate.instance_id}:{candidate.arm}".encode()
        assert paths.root.joinpath("usage.json").is_file()
        assert paths.root.joinpath(f"{candidate.arm}-result.json").is_file()
        assert not tuple(paths.root.glob("*.eval.json"))

    manifest = json.loads(plan.manifest_path.read_text())
    jobs = campaign_evaluate._manifest_jobs(manifest)
    evidence = campaign_evaluate._prepare_evidence(plan.output, jobs)
    assert len(evidence) == 4
    assert all(value["result_error"] is None for value in evidence.values())

    calls.clear()

    assert (
        campaign_runner.main(
            [
                "--config",
                str(config_path),
                "--output",
                str(output),
                "--workers",
                str(workers),
                "--limit",
                "2",
                "--seed",
                "42",
                "--resume",
            ]
        )
        == 0
    )

    assert calls == []
    assert len(prepared) == 2
    assert len(load_calls) == 1


@pytest.mark.parametrize("mode", ["pause", "interrupt"])
def test_fake_paired_campaign_pause_and_interrupt_drain_real_active_job_paths(tmp_path: Path, monkeypatch, mode: str):
    from scripts import campaign_runner
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    plan = campaign_runner.plan_campaign(
        load_config(config_path),
        [
            {
                "instance_id": "one",
                "difficulty": "easy",
                "language": "python",
                "image_name": "one-image",
                "repository": "owner/one",
                "commit": "a" * 40,
            },
            {
                "instance_id": "two",
                "difficulty": "hard",
                "language": "rust",
                "image_name": "two-image",
                "repository": "owner/two",
                "commit": "b" * 40,
            },
        ],
        output=tmp_path / "out",
        limit=2,
        prepare_install_fn=lambda arm, _: {"arm": arm.name},
    )
    monkeypatch.setattr(campaign_runner, "_install_for", lambda frozen, arm: frozen.installs[arm])
    breaker = CircuitBreaker()
    ready = threading.Barrier(2)
    lock = threading.Lock()
    triggered = False
    calls = []

    class Workspace:
        base_url = "http://127.0.0.1:9999"
        port = 9999

        def __init__(self, request, lease):
            self.request = request
            self.lease = lease
            lease.claim_handle(request)

        def stop_for_freeze(self):
            self.lease.release_handle()

    def adapter(_job_lease, _store, workspace, *_args, **kwargs):
        nonlocal triggered
        calls.append((workspace.request.instance_id, workspace.request.arm))
        ready.wait(1)
        with lock:
            first = not triggered
            triggered = True
        if first:
            if mode == "pause":
                breaker.observe_error(401)
            else:
                kwargs["stop_token"].set("keyboard_interrupt")
        workspace.stop_for_freeze()

    result = campaign_runner.run_campaign(
        plan.jobs,
        workers=2,
        breaker=breaker,
        execute=lambda candidate, stop: campaign_runner.run_job(
            plan,
            candidate,
            breaker=breaker,
            stop_token=stop,
            workspace_factory=lambda request, lease: Workspace(request, lease),
            vanilla_runner=adapter,
            procontract_runner=adapter,
            vanilla_api_factory=lambda _: object(),
            procontract_api_factory=lambda _: object(),
        ),
    )

    assert len(calls) == 2
    assert result.failed == 2
    assert result.paused is (mode == "pause")
    assert result.interrupted is (mode == "interrupt")
    assert len(result.pending) == (2 if mode == "pause" else 0)
    for instance_id, arm in calls:
        state = campaign_runner.StateStore(plan.output / arm / instance_id / "state.json").read()
        assert state.phase == "interrupted"
        assert state.exit_class == "campaign_stop"


@pytest.mark.parametrize("outcome", ["paused", "interrupted", "fatal", "durability"])
def test_cli_returns_nonzero_for_paused_interrupted_and_fatal_campaigns(
    tmp_path: Path, monkeypatch, capsys, outcome: str
):
    from scripts import campaign_runner
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    config = load_config(config_path)
    plan = campaign_runner.CampaignPlan(tmp_path / "out", tmp_path / "out/campaign.json", config, (), (), {}, False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "test-password")
    monkeypatch.setattr(campaign_runner, "load_config", lambda _: config)
    monkeypatch.setattr(campaign_runner, "load_all_instances", lambda **_: [])
    monkeypatch.setattr(campaign_runner, "plan_campaign", lambda *_args, **_kwargs: plan)

    def run(*_args, **_kwargs):
        if outcome == "fatal":
            raise campaign_runner.CampaignShutdownError(
                f"noncooperative worker {campaign_runner.os.environ['OPENCODE_SERVER_PASSWORD']}"
            )
        if outcome == "durability":
            recovery = tmp_path / "breaker-recovery.json"
            recovery.write_text("{}")
            raise campaign_runner.CampaignDurabilityError(
                plan.output / "breaker.json",
                recovery,
                CircuitBreaker().snapshot(),
            )
        return campaign_runner.CampaignResult(
            0,
            0,
            (),
            0,
            "paused" if outcome == "paused" else None,
            paused=outcome == "paused",
            interrupted=outcome == "interrupted",
        )

    monkeypatch.setattr(campaign_runner, "run_campaign", run)

    assert campaign_runner.main(["--config", str(config_path), "--output", str(plan.output)]) == 1
    if outcome == "durability":
        assert str(tmp_path / "breaker-recovery.json") in capsys.readouterr().err
    if outcome == "fatal":
        stderr = capsys.readouterr().err
        assert "noncooperative worker" in stderr
        assert "test-password" not in stderr


def test_cli_dispatches_cleanup_only_before_provider_credential_preflight(tmp_path: Path, monkeypatch):
    from scripts import campaign_runner
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    config = load_config(config_path)
    plan = campaign_runner.CampaignPlan(
        tmp_path / "out",
        tmp_path / "out/campaign.json",
        config,
        (),
        (),
        {},
        False,
        cleanup_only=True,
    )
    plan.output.mkdir()
    plan.manifest_path.write_text("{}")
    called = []
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_SERVER_PASSWORD", raising=False)
    monkeypatch.setattr(campaign_runner, "load_config", lambda _: config)
    monkeypatch.setattr(campaign_runner, "load_all_instances", lambda **_: [])
    monkeypatch.setattr(campaign_runner, "plan_campaign", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(
        campaign_runner,
        "run_cleanup_campaign",
        lambda *_args, **_kwargs: called.append("cleanup"),
    )

    assert campaign_runner.main(["--config", str(config_path), "--output", str(plan.output), "--resume"]) == 1
    assert called == ["cleanup"]

    monkeypatch.setattr(campaign_runner, "plan_campaign", lambda *_args, **_kwargs: replace(plan, cleanup_only=False))
    with pytest.raises(campaign_runner.CampaignError, match="credential"):
        campaign_runner.main(["--config", str(config_path), "--output", str(plan.output)])


def _config(repo: Path) -> str:
    return f"""
model: openai/gpt-5.6-luna
variant: max
limits:
  wall_time_seconds: 21600
  provider_turns: 1000
  procontract_actions: 1000
  total_cost_soft: 1
  total_cost_hard: 2
cache:
  warmup_turns: 2
  minimum_read_ratio: 0.8
  stop_after_consecutive_failures: 2
arms:
  procontract:
    repo: {repo}
    revision: HEAD
    mode: procontract
  vanilla:
    repo: {repo}
    revision: HEAD
    mode: vanilla
excluded: []
"""


def _documented_script_arguments(command: str) -> list[str]:
    values = shlex.split(command.replace("\\\n", " "))
    assert values[:4] == ["uv", "run", "python", "scripts/run_opencode.py"]
    return values[4:]


def _replace_argument(arguments: list[str], option: str, value: str) -> None:
    arguments[arguments.index(option) + 1] = value


def _invoke_public_main(run_opencode: object, monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["scripts/run_opencode.py", *arguments])
    with pytest.raises(SystemExit) as exited:
        run_opencode.main()  # type: ignore[attr-defined]
    return int(exited.value.code)
