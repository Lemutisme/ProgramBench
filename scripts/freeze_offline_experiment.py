"""Freeze or verify the implementation and inputs used by the offline study."""

import argparse
import hashlib
import json
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path('/home/duozhou/run-artifacts/offline-inference-sol-20260909-v2')
RUNNER = Path(__file__).resolve().parents[1]
OPENCODE = Path('/home/duozhou/opencode-offline-inference')


def sha(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda:stream.read(1024*1024),b''):
            value.update(data)
    return value.hexdigest()


def source_files(root):
    result=subprocess.run(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=root,
                          check=True,capture_output=True).stdout.decode().split('\0')
    return sorted({value for value in result if value and (root/value).is_file()})


def coordinate():
    return {
        'runner_base':subprocess.run(['git','rev-parse','HEAD'],cwd=RUNNER,check=True,capture_output=True,text=True).stdout.strip(),
        'opencode_base':subprocess.run(['git','rev-parse','HEAD'],cwd=OPENCODE,check=True,capture_output=True,text=True).stdout.strip(),
        'runner':{name:sha(RUNNER/name) for name in source_files(RUNNER)},
        'opencode_changes':subprocess.run(['git','diff','HEAD'],cwd=OPENCODE,check=True,capture_output=True,text=True).stdout,
        'opencode_untracked':{name:sha(OPENCODE/name) for name in subprocess.run(
            ['git','ls-files','--others','--exclude-standard'],cwd=OPENCODE,check=True,capture_output=True,text=True
        ).stdout.splitlines() if (OPENCODE/name).is_file()},
        'binary_sha256':sha(ROOT/'build-v3/opencode'),
        'catalog_sha256':sha(ROOT/'models.json'),
        'wheelhouse':{path.name:sha(path) for path in sorted((ROOT/'evaluator-wheelhouse').glob('*.whl'))},
    }


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['freeze','verify'])
    args=parser.parse_args()
    target=ROOT/'source'
    manifest=target/'FROZEN.json'
    current=coordinate()
    build=json.loads((ROOT/'build-v3/opencode.build.json').read_text())
    assert current['binary_sha256']==build['binarySha256']
    assert current['catalog_sha256']==build['catalogSha256']
    if manifest.exists():
        frozen=json.loads(manifest.read_text())
        assert frozen['coordinate']==current, 'source/config/binary/wheelhouse drift; create a new experiment version'
        print(json.dumps({'status':'verified','binary_sha256':current['binary_sha256']}))
        return
    assert args.action=='freeze','experiment source has not been frozen'
    target.mkdir(parents=True,exist_ok=False)
    with tarfile.open(target/'runner.tar.gz','x:gz') as archive:
        for name in current['runner']:archive.add(RUNNER/name,arcname=name,recursive=False)
    (target/'opencode.patch').write_text(current['opencode_changes'])
    with tarfile.open(target/'opencode-untracked.tar.gz','x:gz') as archive:
        for name in current['opencode_untracked']:archive.add(OPENCODE/name,arcname=name,recursive=False)
    manifest.write_text(json.dumps({'created_at':datetime.now(timezone.utc).isoformat(),'coordinate':current,
                                   'archives':{p.name:sha(p) for p in target.iterdir() if p.is_file()}},indent=2)+'\n')
    print(json.dumps({'status':'frozen','runner_files':len(current['runner']),'binary_sha256':current['binary_sha256']}))


if __name__=='__main__':main()
