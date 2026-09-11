"""Exercise the repaired evaluator on a preserved submission, without a model."""
import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

from programbench.eval.eval import Evaluator
from programbench.submission import score_instance, sha256_file


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--campaign',type=Path,required=True)
    p.add_argument('--instance',required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--arm',choices=('procontract','vanilla'),default='procontract')
    args=p.parse_args()
    os.umask(0o077)
    root=args.output
    root.mkdir(parents=True,exist_ok=False)
    state=json.loads((args.campaign/f'evaluation-state/{args.arm}.json').read_text())
    meta=state['scoring_metadata'][args.instance]
    original=args.campaign/args.arm/args.instance/'submission.tar.gz'
    checksum=sha256_file(original)
    assert checksum==state['submission_sha256'][args.instance]
    archive=root/'submission.tar.gz'
    shutil.copyfile(original,archive)
    os.environ['PROGRAMBENCH_SAFE_REPORTING']='1'
    os.environ['PROGRAMBENCH_CONTAINER_PREFIX']='pipeline-eval-check-'+hashlib.sha256(str(root).encode()).hexdigest()[:10]
    for key in ('OPENAI_API_KEY','OPENAI_BASE_URL','OPENCODE_SERVER_PASSWORD'):
        os.environ.pop(key,None)
    (root/'STATUS.json').write_text(json.dumps({'status':'evaluating','model_calls':0}))
    result=Evaluator(tests_branches=meta['active_branches'],tests_by_branch=meta['expected_tests'],
                     ignored_tests=set(meta['ignored_tests']),image_name=meta['image_name'],
                     submission_archive=archive,solution_branch='submission',
                     blob_dir=Path(state['blob_root'])/args.instance,instance_id=args.instance,
                     docker_cpus=4,branch_retries=0).run()
    target=root/(args.instance+'.eval.json')
    target.write_text(result.model_dump_json(indent=2)+'\n')
    instance={'branches':{b:{'tests':meta['expected_tests'][b],
               'ignored_tests':[{'name':t[len(b)+1:]} for t in meta['ignored_tests'] if t.startswith(b+'/')]}
               for b in meta['active_branches']}}
    filtered=result.for_branches(meta['active_branches']).without_ignored(set(meta['ignored_tests']))
    expected={f'{b}/{t}' for b in meta['active_branches'] for t in meta['expected_tests'][b]}-set(meta['ignored_tests'])
    actual={t.full_name for t in filtered.test_results}
    unrun=sum(t.status in ('not_run','system_error') for t in filtered.test_results)
    report={'status':'finished','instance_id':args.instance,'score':score_instance(target,instance),
            'top_error':result.error_code,'branch_errors':{k:[e.error_code for e in v] for k,v in result.test_branch_errors.items()},
            'missing':sorted(expected-actual),'extra':sorted(actual-expected),'unrun':unrun,
            'complete':not result.error_code and not result.test_branch_errors and not unrun and actual==expected,
            'submission_sha256':checksum,'eval_sha256':sha256_file(target),'model_calls':0}
    assert sha256_file(original)==checksum
    (root/'STATUS.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report),flush=True)
    raise SystemExit(0 if report['complete'] else 1)


if __name__=='__main__':main()
