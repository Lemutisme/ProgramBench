import threading
import time
import subprocess
import sys
from pathlib import Path

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

    run_campaign(tuple(job(index) for index in range(2)), workers=2, state_path=tmp_path / "breaker.json", execute=execute)

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


def test_cli_parser_has_bounded_campaign_defaults_and_rejects_evaluator():
    from scripts.campaign_runner import build_parser, parse_args

    parser = build_parser()
    args = parser.parse_args(["--config", "campaign.yaml", "--dry-run"])
    assert args.workers == 8
    assert args.limit == 8
    assert "8 active trajectories" in parser.format_help()
    with pytest.raises(SystemExit, match="2"):
        parse_args(["--config", "campaign.yaml", "--evaluate"])


def test_cli_limit_and_all_conflict_and_non_dry_requires_output():
    from scripts.campaign_runner import parse_args

    with pytest.raises(SystemExit, match="2"):
        parse_args(["--config", "campaign.yaml", "--all", "--limit", "8", "--dry-run"])
    with pytest.raises(SystemExit, match="2"):
        parse_args(["--config", "campaign.yaml", "--all", "--limit=8", "--dry-run"])
    with pytest.raises(SystemExit, match="2"):
        parse_args(["--config", "campaign.yaml"])


def test_direct_script_cli_help_is_import_safe():
    root = Path(__file__).parents[1]
    result = subprocess.run(
        (sys.executable, str(root / "scripts/run_opencode.py"), "--help"), cwd=root, capture_output=True, text=True
    )

    assert result.returncode == 0
    assert "8 active trajectories" in result.stdout


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
            {"instance_id": "one", "difficulty": "easy", "language": "python", "image_name": "one-image"},
            {"instance_id": "two", "difficulty": "hard", "language": "rust", "image_name": "two-image"},
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


def test_resume_uses_frozen_install_data_without_resolving_moving_refs(tmp_path: Path):
    from scripts.campaign_runner import plan_campaign
    from scripts.run_opencode import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_config(repo))
    instances = [{"instance_id": "one", "difficulty": "easy", "language": "python", "image_name": "one-image"}]
    plan_campaign(
        load_config(config_path), instances, output=tmp_path / "out", limit=1, dry_run=True,
        prepare_install_fn=lambda arm, _root: {"arm": arm.name, "revision": arm.revision, "binary_sha256": "a" * 64},
    )

    resumed = plan_campaign(
        load_config(config_path), instances, output=tmp_path / "out", limit=1, dry_run=True, resume=True,
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
        {"instance_id": "one", "difficulty": "easy", "language": "python", "image_name": "one-image"},
        {"instance_id": "two", "difficulty": "hard", "language": "rust", "image_name": "two-image"},
    ]
    plan_campaign(
        load_config(config_path), instances, output=tmp_path / "out", limit=1, seed=42, dry_run=True,
        prepare_install_fn=lambda arm, _root: {"arm": arm.name, "revision": arm.revision, "binary_sha256": "a" * 64},
    )

    with pytest.raises(CampaignError, match="selection"):
        plan_campaign(
            load_config(config_path), instances, output=tmp_path / "out", limit=2, seed=42, dry_run=True, resume=True,
            prepare_install_fn=lambda *_: pytest.fail("resume must not resolve installs"),
        )


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
        plan, candidate, breaker=CircuitBreaker(), workspace_factory=lambda *_: (_ for _ in ()).throw(RuntimeError("infra"))
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
