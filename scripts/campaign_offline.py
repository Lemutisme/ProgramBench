"""Host-owned lifecycle and control transport for offline inference workspaces."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from scripts.campaign_provider_gateway import process_identity
from scripts.campaign_vanilla_http import HttpResponse, VanillaHttpError


CHANNEL_PATH = "/run/programbench-provider"
BOOTSTRAP = "import os,signal,sys; os.kill(os.getpid(),signal.SIGSTOP); os.execv('/opencode',['/opencode',*sys.argv[1:]])"
OFFLINE_ENV = {
    "OPENAI_API_KEY": "programbench-offline-channel",
    "OPENAI_BASE_URL": "http://programbench-provider.invalid/v1",
    "OPENCODE_PROVIDER_SOCKET": CHANNEL_PATH + "/provider.sock",
    "OPENCODE_DISABLE_MODELS_FETCH": "true",
    "OPENCODE_PROCONTRACT_DISABLED": "0",
}
HEADLESS_CONFIG = {"$schema": "https://opencode.ai/config.json", "permission": {
    "*": "allow", "question": "deny", "contract_issue": "deny", "contract_revision": "deny",
}}
CONTROL_PROGRAM = r'''
import base64,http.client,json,sys
value=json.load(sys.stdin)
connection=http.client.HTTPConnection('127.0.0.1',4096,timeout=value['timeout'])
connection.request(value['method'],value['path'],body=base64.b64decode(value['body']) if value['body'] is not None else None,headers=value['headers'])
response=connection.getresponse()
body=response.read(value['max_bytes']+1)
if len(body)>value['max_bytes']:raise ValueError('response size limit')
print(json.dumps({'status':response.status,'headers':dict(response.getheaders()),'body':base64.b64encode(body).decode()}))
'''


def alive(identity):
    try:
        state = Path(f"/proc/{identity['pid']}/stat").read_text().rpartition(') ')[2].split()[0]
        return state not in ('Z', 'X') and identity == process_identity(identity['pid'])
    except (OSError, ValueError, KeyError, IndexError):
        return False


def prepare_headless_config(manifest):
    # This V2 runtime reads files, not OPENCODE_CONFIG_CONTENT or legacy Session
    # permissions. Restrict the profile to the isolated benchmark container.
    path = manifest.state_dir / 'config/opencode/opencode.jsonc'
    for parent in (path.parent.parent, path.parent):
        if parent.is_symlink():
            raise ValueError('headless configuration parent is a symlink')
        created = not parent.exists()
        parent.mkdir(exist_ok=True, mode=0o755)
        if created:
            # Host controller umask is 077; the container's different UID must
            # be able to traverse the configuration directory, not just file.
            parent.chmod(0o755)
    if path.is_symlink():
        raise ValueError('headless configuration is a symlink')
    if path.exists():
        if json.loads(path.read_text()) != HEADLESS_CONFIG:
            raise ValueError('headless configuration changed; do not overwrite experiment state')
        return
    with path.open('x') as stream:
        stream.write(json.dumps(HEADLESS_CONFIG,sort_keys=True)+'\n')
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o444)


def prepare_tooling(manifest):
    """Freeze an explicitly supplied portable rg before the offline runtime starts."""
    source = os.environ.get("PROGRAMBENCH_RIPGREP")
    if not source:
        return  # Images with rg need no injected executable; readiness is mandatory.
    executable = Path(source).read_bytes()
    digest = hashlib.sha256(executable).hexdigest()
    target = manifest.cache_dir / "opencode/bin/rg"
    for parent in (target.parent.parent, target.parent):
        if parent.is_symlink():
            raise ValueError("tooling directory is a symlink")
        created = not parent.exists()
        parent.mkdir(exist_ok=True, mode=0o755)
        if created:
            parent.chmod(0o755)  # Controller umask 077 must not hide tools from the agent UID.
    if target.is_symlink():
        raise ValueError("tooling executable is a symlink")
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError("frozen ripgrep changed; use new experiment coordinates")
        return
    with target.open("xb") as stream:
        stream.write(executable)
        stream.flush()
        os.fsync(stream.fileno())
    target.chmod(0o555)


def verify_tooling(manifest, docker="docker"):
    probe = r"""
import hashlib,json,os,pathlib,shutil,subprocess,tempfile
rg=shutil.which('rg') or '/cache/opencode/bin/rg'
with tempfile.TemporaryDirectory(prefix='programbench-readiness-') as root:
 p=pathlib.Path(root)/'probe.txt';p.write_text('programbench-tool-ready\n')
 assert p.read_text()=='programbench-tool-ready\n'
 glob=subprocess.run([rg,'--files','--hidden',root],capture_output=True,text=True,check=True,timeout=10)
 assert str(p) in glob.stdout.splitlines()
 grep=subprocess.run([rg,'-n','programbench-tool-ready',str(p)],capture_output=True,text=True,check=True,timeout=10)
 assert grep.stdout.strip()=='1:programbench-tool-ready'
 subprocess.run(['/bin/sh','-c','test -r "$1"','readiness',str(p)],check=True,timeout=10)
 print(json.dumps({'rg_sha256':hashlib.sha256(pathlib.Path(rg).read_bytes()).hexdigest(),'read_write':True,'glob':True,'grep':True,'shell':True}))
"""
    result = subprocess.run([docker, 'exec', manifest.container_id, 'python3', '-I', '-S', '-c', probe],
                            capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise ValueError("offline tool readiness failed; freeze a portable rg with PROGRAMBENCH_RIPGREP before execution")
    return json.loads(result.stdout)


def prepare_reference_binding(manifest, docker='docker'):
    """Bind a model-visible capability from the immutable task image, outside candidate state."""
    result = subprocess.run([
        docker, 'run', '--rm', '--network', 'none', '--cap-drop', 'ALL',
        '--cap-add', 'DAC_OVERRIDE', '--security-opt', 'no-new-privileges', '--user', '0:0',
        '--entrypoint', 'sha256sum', manifest.image_digest, '/workspace/executable',
    ], capture_output=True, text=True, timeout=30, check=True)
    digest = result.stdout.split()[0]
    if len(digest) != 64 or any(character not in '0123456789abcdef' for character in digest):
        raise ValueError('reference identity probe returned an invalid digest')
    identity = f'{manifest.campaign}\0{manifest.job_id}'.encode()
    binding = {
        'contractID': 'pct_' + hashlib.sha256(identity).hexdigest(),
        'candidateDirectory': '/candidate', 'directory': '/workspace',
        'executable': '/workspace/executable', 'hash': digest, 'timeout': 120000,
        'attestation': CHANNEL_PATH + '/reference.json',
    }
    target = manifest.state_root / 'reference-binding.json'
    if target.exists():
        if target.is_symlink() or json.loads(target.read_text()) != binding:
            raise ValueError('reference binding changed; refuse to overwrite its identity')
    else:
        with target.open('x') as stream:
            stream.write(json.dumps(binding, sort_keys=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        target.chmod(0o400)
    return json.dumps(binding, sort_keys=True)


def attest_reference(manifest, docker='docker'):
    """Read execute-only bytes on the trusted host, never grant the agent read access."""
    binding = json.loads((manifest.state_root / 'reference-binding.json').read_text())
    inspect = [docker, 'exec', manifest.container_id, 'python3', '-I', '-S', '-c',
               "import json,os,stat; p='/workspace/executable'; s=os.lstat(p); "
               "assert stat.S_ISREG(s.st_mode) and s.st_uid == 0 and not s.st_mode & 0o222; "
               "print(json.dumps(dict(dev=s.st_dev,ino=s.st_ino,mode=stat.S_IMODE(s.st_mode),"
               "uid=s.st_uid,size=s.st_size,mtime=s.st_mtime_ns//1000000)))"]
    before = json.loads(subprocess.run(inspect, capture_output=True, text=True, check=True, timeout=20).stdout)
    # The Docker daemon can read the root-owned 0111 file. No candidate process
    # gains DAC_OVERRIDE, no executable bytes are mounted/readable by the agent.
    copied = subprocess.run([docker, 'cp', manifest.container_id + ':/workspace/executable', '-'],
                            capture_output=True, check=True, timeout=60)
    with tarfile.open(fileobj=io.BytesIO(copied.stdout), mode='r:*') as archive:
        members = archive.getmembers()
        if len(members) != 1 or not members[0].isfile() or members[0].size != before['size']:
            raise ValueError('reference copy is not the expected regular file')
        checksum = hashlib.sha256()
        with archive.extractfile(members[0]) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                checksum.update(chunk)
        digest = checksum.hexdigest()
    after = json.loads(subprocess.run(inspect, capture_output=True, text=True, check=True, timeout=20).stdout)
    if before != after or digest != binding['hash']:
        raise ValueError('runtime reference differs from its immutable image')
    target = manifest.state_root / 'provider-channel/reference.json'
    temporary = target.with_suffix('.pending')
    with temporary.open('w') as stream:
        stream.write(json.dumps({'executable': binding['executable'], 'hash': digest, 'stat': after}, sort_keys=True))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o444)
    temporary.replace(target)


def ensure_gateway(request):
    channel = request.state_root / "provider-channel"
    control = request.state_root / "provider-control"
    channel.mkdir(parents=True, exist_ok=True, mode=0o755)
    channel.chmod(0o755)
    control.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile = {'model': request.provider_model, 'effort': request.provider_effort, 'job': str(request.job.root),
               'network': 'none', 'transport': 'unix-model-only-peer-pinned-v1'}
    profile_path = control / 'profile.json'
    if profile_path.exists() and json.loads(profile_path.read_text()) != profile:
        raise ValueError('offline gateway profile changed')
    profile_path.write_text(json.dumps(profile, sort_keys=True))
    pidfile = control / 'gateway.json'
    if pidfile.exists() and alive(json.loads(pidfile.read_text())):
        if not stat.S_ISSOCK((channel / 'provider.sock').lstat().st_mode):
            raise ValueError('live gateway has no socket')
        return channel
    target = channel / 'provider.sock'
    if target.exists() or target.is_symlink():
        if not stat.S_ISSOCK(target.lstat().st_mode) or target.lstat().st_uid != os.getuid():
            raise ValueError('refusing foreign gateway socket')
        target.unlink()  # Only the owned dead gateway endpoint, never experiment data.
    environment = {key: value for key, value in os.environ.items() if key in ('PATH','HOME','LANG','LC_ALL','OPENAI_API_KEY','OPENAI_BASE_URL','SSL_CERT_FILE','SSL_CERT_DIR')}
    if not environment.get('OPENAI_API_KEY'):
        raise ValueError('provider credential missing on trusted side')
    with (control / 'gateway.log').open('ab') as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('campaign_provider_gateway.py')),
                                    '--channel',str(channel),'--control',str(control),
                                    '--model',request.provider_model,'--effort',request.provider_effort],
                                   stdin=subprocess.DEVNULL,stdout=log,stderr=log,env=environment,start_new_session=True)
    deadline = time.monotonic()+10
    while time.monotonic()<deadline:
        if process.poll() is not None:
            raise RuntimeError('trusted gateway failed to start; see private gateway log')
        if target.exists() and pidfile.exists() and json.loads(pidfile.read_text()).get('pid') == process.pid:
            return channel
        time.sleep(.05)
    process.terminate()
    raise RuntimeError('trusted gateway startup timeout')


def admit_runtime(manifest, docker='docker'):
    configuration = subprocess.run([docker,'inspect','--format','{{json .HostConfig}}',manifest.container_id],
                                   check=True,capture_output=True,text=True,timeout=20)
    host = json.loads(configuration.stdout)
    if host.get('NetworkMode') != 'none' or host.get('Privileged') or host.get('CapAdd') or host.get('PidMode') == 'host':
        raise ValueError('container isolation configuration rejected')
    if host.get('CapDrop') != ['ALL'] or not any(value.startswith('no-new-privileges') for value in host.get('SecurityOpt',[])):
        raise ValueError('container privilege restrictions missing')
    initialized = subprocess.run([docker,'inspect','--format','{{.State.Pid}}',manifest.container_id],
                                 check=True,capture_output=True,text=True,timeout=20)
    parent = int(initialized.stdout.strip())
    deadline = time.monotonic() + 15
    while True:
        listed = subprocess.run([docker,'top',manifest.container_id,'-eo','pid,ppid,comm'],
                                check=True,capture_output=True,text=True,timeout=20)
        children = [line.split() for line in listed.stdout.splitlines()[1:]
                    if len(line.split()) == 3 and int(line.split()[1]) == parent]
        runtimes = [item for item in children if item[2].startswith('opencode')]
        bootstraps = [item for item in children if item[2] == 'python3']
        selected = runtimes if runtimes else bootstraps
        if len(selected) == 1:
            pid = int(selected[0][0])
            status = Path(f'/proc/{pid}/status').read_text()
            stopped = any(line.startswith('State:') and 'T (stopped)' in line for line in status.splitlines())
            if runtimes or stopped:
                break
        if time.monotonic() >= deadline:
            raise ValueError('runtime did not reach the controlled startup barrier')
        time.sleep(.05)
    path = manifest.state_root / 'provider-control/runtime.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(process_identity(pid)))
    temporary.replace(path)
    if 'OPENCODE_REFERENCE_BINDING' in manifest.environment_names:
        try:
            attest_reference(manifest, docker)
        except (subprocess.SubprocessError, tarfile.TarError) as error:
            raise ValueError('host reference attestation failed before runtime admission') from error
    # The immutable reference remains available even when an executor removed
    # its convenience symlink during final cleanup. It is excluded from exports.
    subprocess.run([docker,'exec',manifest.container_id,'python3','-I','-S','-c',
                    "import os; p='/candidate/reference'; os.symlink('/workspace/executable',p) if not os.path.lexists(p) else None"],
                   check=True,capture_output=True,timeout=20)
    if stopped:
        namespace_pid = next(int(line.split()[-1]) for line in status.splitlines() if line.startswith('NSpid:'))
        subprocess.run([docker,'exec',manifest.container_id,'python3','-I','-S','-c',
                        'import os,signal,sys; os.kill(int(sys.argv[1]),signal.SIGCONT)',str(namespace_pid)],
                       check=True,capture_output=True,timeout=20)


def verify_network(manifest, docker='docker'):
    from scripts.campaign_network_probe import PROBE
    completed = subprocess.run([docker,'exec',manifest.container_id,'python3','-I','-S','-c',PROBE],
                               capture_output=True,text=True,timeout=40)
    if completed.returncode:
        raise ValueError('offline network admission probe failed')
    result = json.loads(completed.stdout)
    result['tooling'] = verify_tooling(manifest, docker)
    if 'UNEXPECTED' in completed.stdout or result.get('interfaces') != ['lo'] or result.get('child_gateway_status') != 403:
        raise ValueError('candidate network boundary is not closed')
    if not result.get('credential_placeholder_only') or not result.get('reference_executable'):
        raise ValueError('candidate credential/reference admission failed')
    if result.get('contract_disabled') != (manifest.arm == 'vanilla'):
        raise ValueError('baseline ProContract feature gate does not match its arm')
    if not all(result[key].get('failed') or result[key].get('unavailable') for key in ('https','git','cargo')):
        raise ValueError('candidate package-manager egress is not blocked')
    headers={'Authorization':'Basic '+base64.b64encode(('opencode:'+os.environ['OPENCODE_SERVER_PASSWORD']).encode()).decode()}
    response=DockerHttpClient(manifest.container_id,docker).request('GET',
        'http://127.0.0.1:4096/api/agent?location%5Bdirectory%5D=%2Fcandidate',
        body=None,headers=headers,timeout=10,max_bytes=1024*1024)
    build=next((agent for agent in json.loads(response.body)['data'] if agent['id']=='build'),None)
    # prepare_headless_config serializes with sort_keys=True; the runtime keeps
    # that rule order because later permissions override earlier ones.
    expected=[{'action':action,'resource':'*','effect':effect} for action,effect in sorted(HEADLESS_CONFIG['permission'].items())]
    if build is None or build['permissions'][-len(expected):] != expected:
        raise ValueError('effective V2 headless permissions do not match the benchmark profile: ' + json.dumps(None if build is None else build['permissions']))
    record = result | {'container_id':manifest.container_id,'binary_sha256':manifest.binary_sha256,
                       'network_mode':manifest.network_mode,'checked_at':time.time(),
                       'headless_permissions':expected,
                       'runtime':json.loads((manifest.state_root/'provider-control/runtime.json').read_text())}
    history = manifest.state_root / 'isolation-receipts'
    history.mkdir(exist_ok=True)
    with (history / f'{time.time_ns()}.json').open('x') as receipt:
        receipt.write(json.dumps(record,sort_keys=True,indent=2)+'\n')
    path = manifest.state_root / 'isolation-receipt.json'
    path.write_text(json.dumps(record,sort_keys=True,indent=2)+'\n')
    return record


def stop_gateway(manifest):
    path = manifest.state_root / 'provider-control/gateway.json'
    if path.exists():
        identity = json.loads(path.read_text())
        if alive(identity):
            os.kill(identity['pid'], signal.SIGTERM)


class HeadlessInteractionError(RuntimeError):
    """An unattended job requested an operator; do not wait or retry a model."""


class DockerHttpClient:
    """Fixed OpenCode control endpoint over Docker exec; no candidate-facing host TCP port."""
    def __init__(self, container_id, docker='docker'):
        self.container_id = container_id
        self.docker = docker

    def request(self, method, url, *, body, headers, timeout, max_bytes):
        parsed = urlsplit(url)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.port != 4096 or parsed.username or parsed.fragment:
            raise VanillaHttpError('offline control endpoint not admitted')
        parts = parsed.path.split('/')
        if method == 'GET' and len(parts) == 5 and parts[1:3] == ['api', 'session'] and parts[4] == 'message':
            pending = self.request('GET', f'http://127.0.0.1:4096/api/session/{parts[3]}/permission',
                                   body=None, headers=headers, timeout=timeout, max_bytes=65536)
            requests = json.loads(pending.body)['data']
            if requests:
                raise HeadlessInteractionError('unattended permission request: ' + json.dumps([
                    {key: request.get(key) for key in ('id', 'action', 'resources')} for request in requests
                ]))
        value = {'method':method,'path':parsed.path + ('?'+parsed.query if parsed.query else ''),
                 'body':base64.b64encode(body).decode() if body is not None else None,
                 'headers':dict(headers),'timeout':timeout,'max_bytes':max_bytes}
        try:
            completed = subprocess.run([self.docker,'exec','-i',self.container_id,'python3','-I','-S','-c',CONTROL_PROGRAM],
                                       input=json.dumps(value).encode(),capture_output=True,timeout=timeout+5)
            if completed.returncode:
                raise VanillaHttpError('offline control process unavailable',transient=True)
            result = json.loads(completed.stdout)
            payload = base64.b64decode(result['body'],validate=True)
            if len(payload)>max_bytes:
                raise VanillaHttpError('offline control response size limit',transient=True)
            if result['status'] >= 400:
                raise VanillaHttpError('OpenCode control request failed',status_code=result['status'],transient=result['status']>=500)
            return HttpResponse(result['status'],{key.lower():value for key,value in result['headers'].items()},payload)
        except (subprocess.TimeoutExpired,OSError,ValueError,KeyError) as error:
            raise VanillaHttpError('offline control transport failed',transient=True) from error


def wait_health(manifest, timeout, docker='docker'):
    deadline=time.monotonic()+timeout
    headers={'Authorization':'Basic '+base64.b64encode(('opencode:'+os.environ['OPENCODE_SERVER_PASSWORD']).encode()).decode()}
    while time.monotonic()<deadline:
        try:
            response=DockerHttpClient(manifest.container_id,docker).request('GET','http://127.0.0.1:4096/global/health',
                       body=None,headers=headers,timeout=min(2,max(.1,deadline-time.monotonic())),max_bytes=65536)
            if response.status==200 and json.loads(response.body).get('healthy') is True:
                return
        except (VanillaHttpError,ValueError):
            pass
        time.sleep(.1)
    raise RuntimeError('offline OpenCode health timeout')
