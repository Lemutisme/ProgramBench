import json
import shutil
import threading

import pytest

from scripts.campaign_admission import disk_admission, study_disk_minimum
from scripts.campaign_runner import CampaignJob, run_campaign


def test_real_disk_threshold_records_only_admission_transitions(tmp_path):
    receipt = tmp_path / "disk.json"
    assert not disk_admission(tmp_path, shutil.disk_usage(tmp_path).total + 1, receipt)
    assert json.loads(receipt.read_bytes())["allowed"] is False
    assert disk_admission(tmp_path, 1, receipt)
    unchanged = receipt.read_bytes()
    assert disk_admission(tmp_path, 1, receipt)
    assert receipt.read_bytes() == unchanged
    assert disk_admission(tmp_path, None, receipt)


@pytest.mark.parametrize("value", [0, -1, True, "20GB", 1.5])
def test_study_rejects_invalid_disk_floor(value):
    with pytest.raises(ValueError):
        study_disk_minimum({"infrastructure_guard": {"minimum_disk_free_bytes": value}})


def test_paused_admission_keeps_active_work_and_resumes_pending_jobs():
    first_started = threading.Event()
    first_completed = threading.Event()
    release = threading.Event()
    admitted = []
    decisions = []

    def check():
        decisions.append(len(admitted))
        return not decisions[:-1] or release.is_set()

    def execute(job, stop):
        admitted.append(job.instance_id)
        if job.instance_id == "one":
            first_started.set()
            assert release.wait(5)
            assert not stop.is_set()
            first_completed.set()

    result = []
    thread = threading.Thread(target=lambda: result.append(run_campaign(
        [CampaignJob(name, "procontract", "image") for name in ["one", "two"]],
        workers=2, admission_check=check, execute=execute,
    )))
    thread.start()
    assert first_started.wait(5)
    assert admitted == ["one"]
    release.set()
    thread.join(5)
    assert not thread.is_alive() and first_completed.is_set()
    assert admitted == ["one", "two"]
    assert result[0].completed == 2 and not result[0].interrupted


def test_cancel_while_all_new_admission_is_paused():
    stop = threading.Event()
    result = run_campaign([CampaignJob("unstarted", "procontract", "image")],
                          admission_check=lambda: False, stop_event=stop,
                          sleeper=lambda _: stop.set(),
                          execute=lambda *_: pytest.fail("paused job must not start"))
    assert result.interrupted and result.completed == 0 and result.failed == 0


def test_admission_probe_fault_cleans_up_already_active_work():
    started = threading.Event()
    closed = threading.Event()
    checks = []

    def check():
        checks.append(True)
        if len(checks) == 1:
            return True
        assert started.wait(5)
        raise OSError("injected admission infrastructure fault")

    def execute(job, stop):
        started.set()
        assert stop.wait(5)
        closed.set()

    with pytest.raises(OSError, match="admission infrastructure fault"):
        run_campaign([CampaignJob(name, "procontract", "image") for name in ["one", "two"]],
                     workers=2, admission_check=check, execute=execute)
    assert closed.is_set()
