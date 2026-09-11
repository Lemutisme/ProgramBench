import json
import sys

import pytest

from programbench.eval.eval import Evaluator, EvalStepError
from scripts import campaign_process
from scripts.campaign_evaluate import EVALUATOR_ENVIRONMENT_NAMES


def test_actual_contained_child_receives_only_declared_evaluator_environment(tmp_path):
    environment={
        'PROGRAMBENCH_EVAL_WHEELHOUSE':str(tmp_path),
        'PROGRAMBENCH_SAFE_REPORTING':'1',
        'OPENAI_API_KEY':'must-not-reach-evaluator',
    }
    result=campaign_process.run_checked(
        (sys.executable,'-c',"import os,json; print(json.dumps({k:v for k,v in os.environ.items() if k.startswith(('PROGRAMBENCH_','OPENAI_'))}))"),
        None,10,1,4096,environment=environment,extra_environment_names=EVALUATOR_ENVIRONMENT_NAMES,
    )
    assert result.returncode==0
    assert json.loads(result.stdout)=={k:environment[k] for k in EVALUATOR_ENVIRONMENT_NAMES}


def test_missing_wheelhouse_is_rejected_before_container_start(monkeypatch):
    monkeypatch.delenv('PROGRAMBENCH_EVAL_WHEELHOUSE',raising=False)
    with pytest.raises(EvalStepError,match='missing_wheelhouse'):
        Evaluator._new_env(type('Settings',(),{'docker_cpus':1})(),'unused-image')
