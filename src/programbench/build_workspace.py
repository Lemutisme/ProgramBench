"""Prepare an empty /workspace before untrusted submission code is admitted.

Only this fixed preparation process has filesystem capabilities. It has no
network and receives no submission. The resulting image is used by a separate
capability-free build container. Original task images are never changed.
"""

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path


PREPARE = "import os,shutil; p='/workspace'; assert not os.path.islink(p); shutil.rmtree(p); os.mkdir(p,0o755)"


class WorkspacePreparationError(RuntimeError):
    pass


def prepare_build_image(image: str, docker: str = 'docker') -> tuple[str, dict]:
    def run(args, *, timeout=60):
        result = subprocess.run([docker, *args], capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise WorkspacePreparationError(f'Trusted workspace preparation failed ({args[0]}): {result.stderr[:1500]}')
        return result.stdout.strip()

    original = run(['image', 'inspect', '--format', '{{.Id}}', image])
    digest = hashlib.sha256((PREPARE + '\nprepare=env-python3-I-S;entrypoint=[/usr/bin/env];cmd=[sleep,2h];user=0:0').encode()).hexdigest()
    key = hashlib.sha256((original + digest).encode()).hexdigest()
    label = 'programbench-build-base:' + key[:24]
    lock_root = Path(tempfile.gettempdir()) / f'programbench-build-base-{os.getuid()}'
    lock_root.mkdir(mode=0o700, exist_ok=True)
    if lock_root.is_symlink() or lock_root.stat().st_uid != os.getuid():
        raise WorkspacePreparationError('Unsafe build preparation lock directory')
    with (lock_root / (key + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = subprocess.run([docker, 'image', 'inspect', '--format', '{{json .Config.Labels}}', label],
                                  capture_output=True, text=True, timeout=20)
        labels = {'programbench.preparation-source': original, 'programbench.preparation-code': digest}
        if existing.returncode == 0:
            observed = json.loads(existing.stdout)
            if not isinstance(observed, dict) or any(observed.get(k) != v for k, v in labels.items()):
                raise WorkspacePreparationError('Prepared image identity conflicts')
            result = run(['image', 'inspect', '--format', '{{.Id}}', label])
            return result, {'source_image': original, 'prepared_image': result, 'preparation_sha256': digest}
        name = 'programbench-build-prepare-' + uuid.uuid4().hex[:16]
        arguments = ['create', '--name', name, '--network', 'none', '--user', '0:0',
                     '--cap-drop', 'ALL', '--cap-add', 'DAC_OVERRIDE', '--cap-add', 'FOWNER',
                     '--security-opt', 'no-new-privileges', '--entrypoint', '/usr/bin/env']
        for k, v in labels.items():
            arguments.extend(['--label', f'{k}={v}'])
        container = run([*arguments, original, 'python3', '-I', '-S', '-c', PREPARE])
        try:
            run(['start', '-a', container], timeout=90)
            if run(['inspect', '--format', '{{.State.ExitCode}}', container]) != '0':
                raise WorkspacePreparationError('Trusted workspace initialization returned nonzero')
            result = run(['commit', '--change', 'ENTRYPOINT ["/usr/bin/env"]', '--change', 'CMD ["sleep","2h"]', container, label], timeout=90)
            return result, {'source_image': original, 'prepared_image': result, 'preparation_sha256': digest}
        finally:
            # This container contains only the trusted image preparation, no experiment candidate.
            run(['rm', '-f', container])
