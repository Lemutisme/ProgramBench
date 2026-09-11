import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from programbench.candidate import CandidateError, PreflightInfrastructureError, _step
from scripts import campaign_workflow


def test_preparation_failure_is_not_a_candidate_failure():
    class Failed:
        def execute(self,*args,**kwargs):
            return {'returncode':1,'output':'permission denied','exception_info':''}
    with pytest.raises(PreflightInfrastructureError):
        _step(Failed(),'trusted preparation',[])
    with pytest.raises(CandidateError):
        _step(Failed(),'./compile.sh',[],candidate=True)


def test_workflow_does_not_send_infrastructure_failure_to_model(tmp_path,monkeypatch):
    candidate=tmp_path/'candidate';candidate.mkdir()
    for name in ('compile.sh','validate.sh'):
        (candidate/name).write_text('#!/bin/sh\nexit 0\n')
    job=tmp_path/'job';job.mkdir()
    class Workspace:
        manifest=SimpleNamespace(job_root=job,instance_id='example__test.abc',image_digest='sha256:unused')
        def stop(self):pass
        def restart(self):raise AssertionError('infrastructure errors must not restart the model')
    workspace=Workspace();workspace.candidate=candidate
    class API:
        def feedback(self,*args):raise AssertionError('infrastructure errors must not become model feedback')
    def failure(*args,**kwargs):raise PreflightInfrastructureError('trusted preparation failed')
    monkeypatch.setattr(campaign_workflow,'preflight_candidate',failure)
    with pytest.raises(PreflightInfrastructureError):
        campaign_workflow.check_and_continue(workspace,API(),'ses_same',12,time.time()+60)
    receipt=json.loads(next((job/'workflow-checks').glob('*/CHECK.json')).read_text())
    assert receipt['status']=='infrastructure_error'
    assert receipt['feedback_status']=='not_sent'
