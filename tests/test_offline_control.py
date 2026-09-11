import base64
import json
import subprocess
import pytest
import os
from types import SimpleNamespace

from scripts.campaign_offline import DockerHttpClient, HeadlessInteractionError, HEADLESS_CONFIG, prepare_headless_config
from scripts.campaign_vanilla_http import HttpOpenCodeApi


def test_missing_session_404_remains_an_admissible_create_path(monkeypatch):
    def execute(argv, **kwargs):
        payload = json.loads(kwargs['input'])
        assert payload['method'] == 'GET'
        return subprocess.CompletedProcess(argv, 0, json.dumps({
            'status': 404, 'headers': {}, 'body': base64.b64encode(b'not found').decode(),
        }).encode(), b'')
    monkeypatch.setattr(subprocess, 'run', execute)
    api = HttpOpenCodeApi('http://127.0.0.1:4096', expected_port=4096,
                         http_client=DockerHttpClient('a' * 64))
    assert api.get_session('ses_missing') is None


def test_pending_permission_fails_before_unattended_poll(monkeypatch):
    paths=[]
    def execute(argv, **kwargs):
        payload=json.loads(kwargs['input'])
        paths.append(payload['path'])
        assert payload['method']=='GET'
        assert payload['path']=='/api/session/ses_pending/permission'
        body=json.dumps({'data':[{'id':'per_pending','action':'external_directory','resources':['/tmp/*']}]}).encode()
        return subprocess.CompletedProcess(argv,0,json.dumps({'status':200,'headers':{},'body':base64.b64encode(body).decode()}).encode(),b'')
    monkeypatch.setattr(subprocess,'run',execute)
    api=HttpOpenCodeApi('http://127.0.0.1:4096',expected_port=4096,http_client=DockerHttpClient('a'*64))
    with pytest.raises(HeadlessInteractionError,match='external_directory'):
        api.messages('ses_pending')
    assert paths==['/api/session/ses_pending/permission']


def test_headless_config_is_exact_and_never_overwrites_existing_state(tmp_path):
    manifest=SimpleNamespace(state_dir=tmp_path)
    previous=os.umask(0o077)
    try:
        prepare_headless_config(manifest)
    finally:
        os.umask(previous)
    path=tmp_path/'config/opencode/opencode.jsonc'
    assert json.loads(path.read_text())==HEADLESS_CONFIG
    assert path.parent.stat().st_mode & 0o777 == 0o755
    assert path.stat().st_mode & 0o777 == 0o444
    before=path.stat().st_ino
    prepare_headless_config(manifest)
    assert path.stat().st_ino==before
    path.chmod(0o644)
    path.write_text('{"permission":{"*":"deny"}}')
    with pytest.raises(ValueError,match='do not overwrite'):
        prepare_headless_config(manifest)
    assert json.loads(path.read_text())=={'permission':{'*':'deny'}}


def test_headless_config_rejects_parent_symlink(tmp_path):
    target=tmp_path/'elsewhere'
    target.mkdir()
    (tmp_path/'config').symlink_to(target,target_is_directory=True)
    with pytest.raises(ValueError,match='symlink'):
        prepare_headless_config(SimpleNamespace(state_dir=tmp_path))
    assert list(target.iterdir())==[]
