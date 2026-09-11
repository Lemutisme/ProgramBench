import hashlib
import io
import json
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

from scripts.campaign_offline import attest_reference, prepare_reference_binding


def test_reference_binding_uses_the_immutable_image_and_is_not_model_editable(tmp_path, monkeypatch):
    digest = 'a' * 64
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, digest + '  /workspace/executable\n', '')
    monkeypatch.setattr(subprocess, 'run', execute)
    manifest = SimpleNamespace(campaign='test', job_id='job', image_digest='sha256:fixed', state_root=tmp_path)
    binding = json.loads(prepare_reference_binding(manifest))
    assert binding['contractID'] == 'pct_' + hashlib.sha256(b'test\0job').hexdigest()
    assert binding['hash'] == digest
    assert binding['candidateDirectory'] == '/candidate'
    assert binding['executable'] == '/workspace/executable'
    assert binding['attestation'] == '/run/programbench-provider/reference.json'
    assert calls[0][calls[0].index('--network') + 1] == 'none'
    assert calls[0][calls[0].index('--user') + 1] == '0:0'
    assert calls[0][calls[0].index('--cap-add') + 1] == 'DAC_OVERRIDE'
    assert calls[0][-2:] == ['sha256:fixed', '/workspace/executable']
    assert json.loads(prepare_reference_binding(manifest)) == binding
    digest = 'b' * 64
    with pytest.raises(ValueError, match='binding changed'):
        prepare_reference_binding(manifest)
    assert json.loads((tmp_path / 'reference-binding.json').read_text()) == binding


@pytest.mark.parametrize('change', ['none', 'bytes', 'stat', 'symlink'])
def test_runtime_attestation_keeps_execute_only_access_and_rejects_substitution(tmp_path, monkeypatch, change):
    content = b'opaque executable'
    digest = hashlib.sha256(content).hexdigest()
    (tmp_path / 'reference-binding.json').write_text(json.dumps({'executable': '/workspace/executable', 'hash': digest}))
    channel = tmp_path / 'provider-channel'
    channel.mkdir()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        member = tarfile.TarInfo('executable')
        member.size = len(content)
        if change == 'symlink':
            member.type = tarfile.SYMTYPE
            member.linkname = '/candidate/fake'
        archive.addfile(member, io.BytesIO(b'x' * len(content) if change == 'bytes' else content))
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        if command[1] == 'cp':
            return subprocess.CompletedProcess(command, 0, output.getvalue(), b'')
        value = {'dev': 1, 'ino': 2, 'mode': 0o111, 'uid': 0, 'size': len(content), 'mtime': 0}
        if change == 'stat' and len(calls) == 3:
            value['ino'] = 3
        return subprocess.CompletedProcess(command, 0, json.dumps(value), '')
    monkeypatch.setattr(subprocess, 'run', execute)
    manifest = SimpleNamespace(state_root=tmp_path, container_id='test-container')
    if change != 'none':
        with pytest.raises(ValueError, match='reference'):
            attest_reference(manifest)
        assert not (channel / 'reference.json').exists()
        return
    attest_reference(manifest)
    receipt = json.loads((channel / 'reference.json').read_text())
    assert receipt['hash'] == digest and receipt['stat']['mode'] == 0o111
    assert (channel / 'reference.json').stat().st_mode & 0o777 == 0o444
    assert all('--privileged' not in call and '--cap-add' not in call for call in calls)
