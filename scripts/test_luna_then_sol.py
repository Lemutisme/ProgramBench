"""Frozen end-to-end qualification: Luna Max first, Sol xhigh only after passing.

This is one known task in three profiles per model, not a headline benchmark.
Passing means valid delivery and complete evaluation, not a 100% task score.
"""

import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

from programbench.eval.eval import EvaluationResult
from programbench.submission import score_instance


REPO = Path(__file__).resolve().parents[1]
ROOT = Path('/home/duozhou/run-artifacts/offline-pipeline-luna-20260910-v3')
PREVIOUS = Path('/home/duozhou/run-artifacts/offline-pipeline-luna-20260910-v1')
IID = 'abishekvashok__cmatrix.5c082c6'
PROFILES = ('default', 'workflow', 'procontract')
BINARY = Path('/home/duozhou/run-artifacts/offline-inference-sol-20260909-v1/build-v3/opencode')
WHEELS = Path('/home/duozhou/run-artifacts/offline-inference-sol-20260909-v1/evaluator-wheelhouse')


def sha(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):value.update(chunk)
    return value.hexdigest()


def write(path, value):
    data=(json.dumps(value,sort_keys=True,indent=2)+'\n').encode()
    temporary=path.with_name(path.name+'.tmp')
    with temporary.open('wb') as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())
    temporary.replace(path)


def group_root(root, model, profile):
    # Workspace container ownership uses the campaign leaf name. Distinct
    # artifact roots must not collide with retained prior-run containers.
    identity=hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]
    return root/model/f'{model}-{profile}-cmatrix-{identity}'


def inputs():
    names=subprocess.run(['git','ls-files','--cached','--others','--exclude-standard','-z'],
                         cwd=REPO,check=True,capture_output=True).stdout.decode().split('\0')
    return {'source':{n:sha(REPO/n) for n in sorted(set(names)) if n and (REPO/n).is_file()},
            'binary_sha256':sha(BINARY),'wheelhouse':{p.name:sha(p) for p in sorted(WHEELS.glob('*.whl'))}}


def qualify(model, roots, evaluations=None):
    findings=[]
    for profile,root in roots.items():
        arm='procontract' if profile=='procontract' else 'vanilla'
        job=root/arm/IID
        row={'profile':profile,'root':str(root),'passed':False}
        try:
            state=json.loads((job/'state.json').read_text())
            receipt=json.loads((job/'workspace-state/isolation-receipt.json').read_text())
            result_name='procontract-result.json' if arm=='procontract' else 'vanilla-result.json'
            native=json.loads((job/result_name).read_text())
            assert state['phase']=='done', 'delivery did not reach done'
            assert native['completion']==('discharged' if arm=='procontract' else 'idle'), 'delivery completion mismatch'
            assert receipt['network_mode']=='none' and receipt['child_gateway_status']==403
            assert receipt['contract_disabled']==(arm=='vanilla')
            assert not (job/'campaign-exception.json').exists(), 'campaign infrastructure exception'
            assert all(json.loads(p.read_text())['status']!='infrastructure_error' for p in (job/'workflow-checks').glob('*/CHECK.json'))
            metadata=json.loads((root/f'evaluation-state/{arm}.json').read_text())['scoring_metadata'][IID]
            evaluation=(evaluations or {}).get(profile,root/'evaluation'/arm/IID/(IID+'.eval.json'))
            data=EvaluationResult.model_validate_json(evaluation.read_text())
            filtered=data.for_branches(metadata['active_branches']).without_ignored(set(metadata['ignored_tests']))
            expected={f'{b}/{name}' for b in metadata['active_branches'] for name in metadata['expected_tests'][b]}-set(metadata['ignored_tests'])
            assert not data.error_code and not data.test_branch_errors, 'evaluator error'
            assert {t.full_name for t in filtered.test_results}==expected, 'test inventory mismatch'
            assert not any(t.status in ('not_run','system_error') for t in filtered.test_results), 'evaluation incomplete'
            instance={'branches':{b:{'tests':metadata['expected_tests'][b],
                       'ignored_tests':[{'name':n[len(b)+1:]} for n in metadata['ignored_tests'] if n.startswith(b+'/')]}
                       for b in metadata['active_branches']}}
            database=job/'workspace-state/provider-control/requests.db'
            with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
                calls=db.execute('SELECT metadata FROM request WHERE metadata IS NOT NULL').fetchall()
            assert calls, 'no recorded model requests'
            for call in calls:
                request=json.loads(call[0])
                assert request['model']==f'gpt-5.6-{model}'
                assert request['reasoning']['effort']==('max' if model=='luna' else 'xhigh')
                if arm=='vanilla':assert not any((n or '').startswith('contract_') for n in request['tool_names'])
            row.update(passed=True,score=score_instance(evaluation,instance),submission_sha256=sha(job/'submission.tar.gz'),
                       eval_sha256=sha(evaluation),eval_path=str(evaluation),provider_turns=native['provider_turns'],cost=str(native['cost']))
        except (OSError,ValueError,KeyError,AssertionError) as error:
            row['reason']=str(error) or type(error).__name__
        findings.append(row)
    return {'model':model,'passed':all(row['passed'] for row in findings),'groups':findings,
            'rule':'delivery done + complete evaluation + isolated fixed model; no score threshold'}


def main():
    os.umask(0o077)
    ROOT.mkdir(parents=True,exist_ok=True)
    source=ROOT/'frozen'
    source.mkdir(exist_ok=False)
    frozen=inputs()
    write(source/'INPUTS.json',frozen)
    with tarfile.open(source/'runner.tar.gz','x:gz') as archive:
        for name in frozen['source']:archive.add(REPO/name,arcname=name,recursive=False)
    write(ROOT/'STUDY.json',{'status':'starting','instances':[IID],'profiles':PROFILES,
                             'sequence':['luna max','sol xhigh'],'started_at':datetime.now(timezone.utc).isoformat(),
                             'source_root':str(REPO),'binary_sha256':frozen['binary_sha256']})
    for model in ('luna','sol'):
        assert inputs()==frozen,'frozen runner inputs changed'
        free=shutil.disk_usage(ROOT).free
        if free<30*1024**3:
            write(ROOT/'STUDY.json',{'status':'blocked_capacity','next_model':model,'available_bytes':free})
            return
        roots={profile:group_root(ROOT,model,profile) for profile in PROFILES}
        evaluations={}
        if model=='luna':
            roots.update({profile:PREVIOUS/'luna'/f'luna-{profile}-cmatrix-v1' for profile in ('workflow','procontract')})
        children=[]
        for profile,root in roots.items():
            root.parent.mkdir(parents=True,exist_ok=True)
            config=REPO/'scripts'/f'opencode_programbench_pipeline_{model}{"_workflow" if profile=="workflow" else ""}.yaml'
            environment=dict(os.environ,OPENCODE_REPO='/home/duozhou/opencode-offline-inference',
                             OPENCODE_SERVER_PASSWORD=hashlib.sha256(('pipeline-control\0'+str(root)).encode()).hexdigest(),
                             PROGRAMBENCH_SAFE_REPORTING='1',PROGRAMBENCH_EVAL_WHEELHOUSE=str(WHEELS))
            log_root=ROOT/model
            log_root.mkdir(parents=True,exist_ok=True)
            log=(log_root/f'{profile}.controller.log').open('ab')
            command=['bash',str(REPO/'scripts/run_all.sh'),'--output',str(root),'--config',str(config),
                                      '--workers','1','--seed','42','--expected-instances','1',
                                      '--arm','procontract' if profile=='procontract' else 'vanilla',
                                      '--allow-quarantined-key','--filter',r'^abishekvashok__cmatrix\.5c082c6$']
            if model=='luna' and profile!='default':
                destination=ROOT/model/f'{profile}-reevaluation'
                evaluations[profile]=destination/(IID+'.eval.json')
                command=['uv','run','python','-m','scripts.check_saved_evaluation','--campaign',str(root),
                         '--instance',IID,'--arm','procontract' if profile=='procontract' else 'vanilla',
                         '--output',str(destination)]
            process=subprocess.Popen(command,
                                     cwd=REPO,env=environment,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                                     start_new_session=True)
            log.close();children.append((profile,process))
        write(ROOT/'STUDY.json',{'status':'running','model':model,'groups':{k:str(v) for k,v in roots.items()},
                                 'pids':{k:p.pid for k,p in children},'sol_started':model=='sol',
                                 'reevaluations':{k:str(v) for k,v in evaluations.items()},
                                 'started_at':datetime.now(timezone.utc).isoformat()})
        while any(process.poll() is None for _,process in children):
            if any(process.poll() not in (None,0) for _,process in children):
                # A pipeline fault invalidates qualification. Stop only this
                # controller's peer groups; never spend the remaining 6h idle.
                for _,process in children:
                    if process.poll() is None:
                        os.killpg(process.pid,signal.SIGINT)
                deadline=time.monotonic()+40
                while time.monotonic()<deadline and any(process.poll() is None for _,process in children):
                    time.sleep(1)
                for _,process in children:
                    if process.poll() is None:
                        os.killpg(process.pid,signal.SIGTERM)
                break
            time.sleep(5)
        gate=qualify(model,roots,evaluations)
        gate['controller_exits']={profile:process.returncode for profile,process in children}
        gate['reused_inference_profiles']=list(evaluations)
        if any(process.returncode != 0 for _,process in children):
            gate['passed']=False
            gate['controller_error']=True
        write(ROOT/f'{model.upper()}_GATE.json',gate)
        if not gate['passed']:
            write(ROOT/'STUDY.json',{'status':'qualification_failed','model':model,'gate':str(ROOT/f'{model.upper()}_GATE.json'),
                                     'sol_started':model=='sol'})
            print(json.dumps({'model':model,'gate':'failed','next_model_started':False}),flush=True)
            return
        print(json.dumps({'model':model,'gate':'passed'}),flush=True)
    write(ROOT/'STUDY.json',{'status':'completed','luna_gate':'passed','sol_gate':'passed','instances_per_group':1,
                             'full_200_started':False})


if __name__=='__main__':main()
