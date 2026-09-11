"""Persistent, ProContract-free frozen check/repair baseline.

Triggered only after the ordinary Session becomes idle. It preserves every
snapshot/check, returns deterministic diagnostics to the same Session, and keeps
the outer trajectory's original cumulative turns and wall deadline.
"""

import hashlib
import json
import time
import uuid
from pathlib import Path

from programbench.candidate import CandidateError, package_workspace, preflight_candidate, sha256


def check_and_continue(workspace, api, session_id, turns, wall_deadline):
    root = workspace.manifest.job_root / 'workflow-checks'
    root.mkdir(exist_ok=True)
    check = root / f'{turns:04d}-{uuid.uuid4().hex[:12]}'
    check.mkdir()
    workspace.stop()
    record = {'version':1,'session_id':session_id,'provider_turns_before':turns,'started_at':time.time(),
              'network':'none','status':'checking','contract_used':False}
    receipt = check / 'CHECK.json'
    receipt.write_text(json.dumps(record,sort_keys=True,indent=2)+'\n')
    failure = None
    archive = None
    try:
        if not (workspace.candidate / 'validate.sh').is_file():
            raise CandidateError('Required self-contained validate.sh is missing')
        archive = package_workspace(workspace.candidate,check,workspace.manifest.instance_id)
        record['submission_sha256'] = sha256(archive)
        receipt.write_text(json.dumps(record,sort_keys=True,indent=2)+'\n')
        result = preflight_candidate(archive,workspace.manifest.instance_id,
                                     deadline=time.monotonic()+max(0,wall_deadline-time.time()),
                                     image_ref=workspace.manifest.image_digest)
        record['executable_sha256'] = result.executable_hash
        (check / 'output.log').write_text(result.output)
    except CandidateError as error:
        failure = str(error)
        (check / 'output.log').write_text(failure+'\n')
    except Exception as error:
        record.update(status='infrastructure_error', finished_at=time.time(), feedback_status='not_sent', error_type=type(error).__name__)
        (check / 'output.log').write_text(str(error)+'\n')
        record['output_sha256'] = sha256(check / 'output.log')
        receipt.write_text(json.dumps(record,sort_keys=True,indent=2)+'\n')
        raise
    record.update(status='failed' if failure else 'passed',finished_at=time.time())
    record['output_sha256'] = sha256(check / 'output.log')
    receipt.write_text(json.dumps(record,sort_keys=True,indent=2)+'\n')
    if not failure or time.time() >= wall_deadline:
        return workspace, False
    restarted = workspace.restart()
    message_id = 'msg_' + hashlib.sha256((str(check)+record['output_sha256']).encode()).hexdigest()[:32]
    record['feedback_message_id'] = message_id
    record['feedback_status'] = 'admitting'
    receipt.write_text(json.dumps(record,sort_keys=True,indent=2)+'\n')
    api.feedback(session_id,message_id,
        'The host-owned frozen delivery check failed. Continue repairing this candidate in the same Session; '
        'the original time/turn budget and no-internet rules are unchanged. No hidden benchmark test was used. '
        f'Checked source archive: {record.get("submission_sha256", "not packageable")}. '
        'Repair the reported build/regression problem and finish when the documented behavior is implemented.\n\n'
        + failure[:8000])
    record['feedback_status'] = 'admitted'
    receipt.write_text(json.dumps(record,sort_keys=True,indent=2)+'\n')
    return restarted, True
