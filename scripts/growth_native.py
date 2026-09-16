"""Small native generation adapter and the existing frozen campaign entrypoint."""

from contextlib import contextmanager
from decimal import Decimal
from fractions import Fraction
import json
import os
from pathlib import Path
import re
import selectors
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode

import yaml

from scripts.campaign_policy import load_policy_artifact
from scripts.growth_loop import GrowthError, GrowthInfrastructureError, encode, evaluator_environment, frozen_input, read, record, seal, sha256


REVISION_ARTIFACTS = {
    "revision_proposal": ("agent-revision.json", 65536, False),
    "source_patch": ("source.patch", 16 * 1024 * 1024, True),
    "solver_policy": ("solver-policy.txt", 65536, False),
    "generator_policy": ("generator-policy.txt", 65536, False),
}


def generator_artifacts(specification):
    if specification["mutable_objects"] == ["agent_revision"]:
        return REVISION_ARTIFACTS
    return {key: (key.replace("_", "-") + ".txt", 65536, False) for key in specification["mutable_objects"]}


def install_revision_source(workspace, reference, *, output=None):
    """Create a verified parent tree as the container agent, without ownership repair."""
    from scripts.agent_revision import artifact_bytes, validate_revision

    output = Path(output) if output is not None else Path(tempfile.mkdtemp(prefix="generator-source-import-"))
    phase = "validate-parent"
    try:
        revision = validate_revision(reference)
        files = json.loads(frozen_input(revision["source"]["files"]))
        checker = """import hashlib,json,os,pathlib,pwd,stat,subprocess,sys,tarfile
os.umask(0o022)
owner=pwd.getpwnam('agent');assert os.getuid()==owner.pw_uid and os.getuid()!=0
root=pathlib.Path('/candidate/agent-source');stream=sys.stdin.buffer
size=int.from_bytes(stream.read(8),'big');assert 0<size<=16777216
expected=json.loads(stream.read(size));assert type(expected) is dict
links={name for name,item in expected.items() if item['mode']=='120000'}
for name in expected:
 path=pathlib.PurePosixPath(name)
 assert not path.is_absolute() and str(path)==name and not set(path.parts)&{'.','..','.git'}
 assert not any(str(parent) in links for parent in path.parents)
root.mkdir(mode=0o755,exist_ok=False);seen=set()
with tarfile.open(fileobj=stream,mode='r|') as archive:
 for member in archive:
  assert member.name in expected and member.name not in seen
  seen.add(member.name);item=expected[member.name]
  if item['mode']=='120000':
   assert member.issym() and member.linkname==item['target'];continue
  assert item['mode'] in {'100644','100755'} and member.isfile() and member.size==item['size']
  assert bool(member.mode&0o111)==(item['mode']=='100755')
  path=root/member.name;path.parent.mkdir(parents=True,exist_ok=True)
  digest=hashlib.sha256();length=0
  with archive.extractfile(member) as source,path.open('xb') as target:
   while data:=source.read(1048576):
    length+=len(data);assert length<=item['size'];digest.update(data);target.write(data)
  assert length==item['size'] and digest.hexdigest()==item['sha256']
  path.chmod(0o755 if item['mode']=='100755' else 0o644)
assert seen==set(expected)
for name in links:
 path=root/name;target=expected[name]['target']
 assert not pathlib.PurePosixPath(target).is_absolute()
 path.parent.mkdir(parents=True,exist_ok=True);path.symlink_to(target)
 assert path.resolve().is_relative_to(root)
actual={}
for path in root.rglob('*'):
 name=path.relative_to(root).as_posix();info=path.lstat();assert info.st_uid==os.getuid()
 if stat.S_ISDIR(info.st_mode):continue
 assert name in expected
 if stat.S_ISLNK(info.st_mode):
  target=os.readlink(path);data=target.encode()
  actual[name]={'mode':'120000','sha256':hashlib.sha256(data).hexdigest(),'size':len(data),'target':target}
 else:
  assert stat.S_ISREG(info.st_mode)
  actual[name]={'mode':'100755' if info.st_mode&0o111 else '100644','sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'size':info.st_size}
assert actual==expected
assert all((root/name).resolve().is_relative_to(root) for name in links)
env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/tmp','GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_NOSYSTEM':'1',
 'GIT_AUTHOR_NAME':'AgentRevision','GIT_AUTHOR_EMAIL':'revision@localhost','GIT_COMMITTER_NAME':'AgentRevision',
 'GIT_COMMITTER_EMAIL':'revision@localhost','GIT_AUTHOR_DATE':'2000-01-01T00:00:00+00:00','GIT_COMMITTER_DATE':'2000-01-01T00:00:00+00:00'}
for args in [['init','-q'],['add','--force','--all'],['commit','-q','--allow-empty','-m','Frozen parent source']]:
 subprocess.run(['git','-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false','-c','core.autocrlf=false',
  '-c','gc.auto=0','-c','maintenance.auto=false',*args],
  cwd=root,env=env,check=True,timeout=60)
tracked=subprocess.check_output(['git','ls-files','-z'],cwd=root,env=env,timeout=60)
assert set(tracked.decode().rstrip('\\0').split('\\0'))==set(expected)
print(json.dumps({'passed':True,'files':len(actual),'uid':os.getuid(),'gid':os.getgid(),
 'tree_sha256':hashlib.sha256((json.dumps(actual,sort_keys=True,indent=2)+'\\n').encode()).hexdigest()}))
"""
        phase = "agent-import"
        with tempfile.TemporaryFile() as payload:
            header = encode(files)
            payload.write(len(header).to_bytes(8, "big"))
            payload.write(header)
            payload.write(artifact_bytes(revision["source"]["archive"]))
            payload.seek(0)
            result, stdout = _source_import_process([
                "docker", "exec", "--user", "agent", "-i", workspace.container_id,
                "timeout", "--signal=TERM", "--kill-after=5s", "240s", "python3", "-I", "-S", "-c", checker,
            ], payload, output)
        if result["exit_code"] != 0 or result["timed_out"] or result["output_limited"]:
            raise GrowthError("container source import did not complete successfully")
        phase = "verify-import"
        verified = json.loads(stdout)
        if (verified.get("passed") is not True or verified.get("tree_sha256") != revision["source"]["tree_sha256"]
                or verified.get("files") != len(files) or type(verified.get("uid")) is not int or verified["uid"] <= 0):
            raise GrowthError("generator parent source import changed its frozen tree or execution identity")
        return {"agent_revision_sha256": reference["sha256"], "source_archive_sha256": revision["source"]["archive"]["sha256"],
                "container": workspace.container_id, "destination": "/candidate/agent-source", "verified": verified,
                "execution_user": "agent", "host_checkout_mounted": False, "host_git_metadata_copied": False,
                "diagnostic": {"path": str(output / "process.json"), "sha256": sha256(encode(result))}}
    except Exception as error:
        try:
            record(output / "failure.json", {"phase": phase, "agent_revision": reference, "container": workspace.container_id,
                   "error_type": type(error).__name__, "error": str(error)[:4096]})
        except Exception as publication:
            raise GrowthInfrastructureError(f"generator source import failed; evidence publication also failed: {type(publication).__name__}") from error
        raise GrowthInfrastructureError(f"generator source import failed during {phase}; evidence {output}") from error


def _source_import_process(argv, payload, output, *, timeout=250):
    """Drain both Docker streams with bounded diagnostics and a host watchdog."""
    started = time.monotonic()
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    process = None
    timed_out = output_limited = False
    try:
        process = subprocess.Popen(argv, stdin=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                timed_out = time.monotonic() - started >= timeout
                if timed_out or output_limited:
                    break
                for key, _ in selector.select(timeout=0.05):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = 65536 - len(streams[key.data])
                    streams[key.data].extend(data[:remaining])
                    output_limited = len(data) > remaining
                    if output_limited:
                        break
            if not timed_out and not output_limited:
                try:
                    process.wait(timeout=max(0.1, min(10, timeout - (time.monotonic() - started))))
                except subprocess.TimeoutExpired:
                    timed_out = True
    finally:
        if process is not None:
            if timed_out or output_limited or process.poll() is None or sys.exc_info()[1] is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=10)
            process.stdout.close()
            process.stderr.close()
        for name, data in streams.items():
            seal(output / f"{name}.log", bytes(data))
        result = record(output / "process.json", {
            "argv": argv, "exit_code": process.returncode if process is not None else None,
            "timeout_seconds": timeout, "timed_out": timed_out, "output_limited": output_limited,
            "stdout": {"path": str(output / "stdout.log"), "sha256": sha256(bytes(streams["stdout"]))},
            "stderr": {"path": str(output / "stderr.log"), "sha256": sha256(bytes(streams["stderr"]))},
        })
    return result, bytes(streams["stdout"])


def generator_context(root, output):
    # Admission and cached validation must consume the same sealed bytes, including key order.
    return (root / "generator-context.txt").read_text() + "\n" + (output / "feedback.json").read_text()


def generator_payload(output, specification, instruction, context, model, variant, directory="/candidate", *, started_at=None):
    """Native proposal production; host builds and evaluation stay outside this Contract."""
    identity = sha256(encode([str(output), specification]))
    started_at = time.time() if started_at is None else started_at
    revision = specification["mutable_objects"] == ["agent_revision"]
    files = generator_artifacts(specification)
    artifacts = [item[0] for item in files.values()]
    check = ("import json,os,stat\n"
             f"for name,limit,empty in {list(files.values())!r}:\n"
             " fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)\n"
             " with os.fdopen(fd,'rb') as f:\n"
             "  s=os.fstat(f.fileno()); assert stat.S_ISREG(s.st_mode) and (empty or s.st_size>0) and s.st_size<=limit\n"
             "  data=f.read(limit+1); assert len(data)<=limit and b'\\0' not in data\n"
             "  assert data.decode('utf-8').strip() or empty\n")
    if revision:
        from inspect import getsource
        from scripts.campaign_experiments import MAX_OPERATION_MS, validate_experiments, validate_runtime_options
        # Freeze the same pure validation code into the four-artifact replay;
        # the candidate never imports mutable host modules during validation.
        check += (f"MAX_OPERATION_MS={MAX_OPERATION_MS}\n" + getsource(validate_experiments) + "\n"
                  + getsource(validate_runtime_options) + "\n"
                  "with open('agent-revision.json',encoding='utf-8') as f: proposal=json.load(f)\n"
                  "assert type(proposal) is dict and {'parent_sha256','rationale','metadata'} <= set(proposal) and not set(proposal)-{'parent_sha256','rationale','metadata','runtime_options'}\n"
                  "if 'runtime_options' in proposal: validate_runtime_options(proposal['runtime_options'])\n"
                  f"assert proposal['parent_sha256']=={specification['binding']['agent_revision']['sha256']!r}\n"
                  "assert type(proposal['rationale']) is str and proposal['rationale'].strip() and '\\0' not in proposal['rationale']\n"
                  "metadata=proposal['metadata']\n"
                  "assert type(metadata) is dict and set(metadata)=={'expected_effect','failure_modes','permission_changes','state_migration','recovery'}\n"
                  "for key in ['expected_effect','recovery']:\n"
                  " assert type(metadata[key]) is str and metadata[key].strip() and '\\0' not in metadata[key]\n"
                  "for key in ['failure_modes','permission_changes']:\n"
                  " assert type(metadata[key]) is list and all(type(item) is str and item.strip() and '\\0' not in item for item in metadata[key])\n"
                  "assert metadata['failure_modes']\n"
                  "migration=metadata['state_migration']\n"
                  "assert migration is None or (type(migration) is str and migration.strip() and '\\0' not in migration)\n")
    instructions = (
        "Write agent-revision.json containing parent_sha256, a nonempty rationale, metadata, and optionally runtime_options; parent_sha256 must be "
        + specification["binding"]["agent_revision"]["sha256"] + ". Write source.patch as a UTF-8 git patch against "
        "the exact parent checkout at /candidate/agent-source. You may edit that checkout; only the patch and the "
        "two policy artifacts are proposed outputs. Include new files in the patch. An empty patch is allowed. "
        "The patch may be at most 16777216 bytes; the JSON and each policy at most 65536 bytes. "
        "All files must be regular UTF-8 files without NUL; both policies must be nonempty. "
        "metadata must contain exactly expected_effect (nonempty text), failure_modes (nonempty list of nonempty text), "
        "permission_changes (list of nonempty text, empty when none), state_migration (nonempty text or null), "
        "and recovery (nonempty text describing how to withdraw the change). Requested permission or state changes "
        "remain proposals and cannot authorize their own execution. "
        "Omitting runtime_options inherits the exact parent's tool configuration. To disable native experiments, "
        "set runtime_options to {\"experiments\":null}. To enable them, its only key experiments must contain exactly "
        "candidate (\"executable\"), interpreter (\"/usr/bin/python3\"), timeout and maxDuration (integer milliseconds "
        "from 1 to 600000), and prepare ({\"argv\":[\"/bin/sh\",\"./compile.sh\"],\"timeout\":integer milliseconds}). "
        "Both process timeouts must not exceed maxDuration. These bound individual operations within the original "
        "Contract deadline; they do not grant additional time or cumulative budgets. No other runtime option, "
        "environment setting or execution path is accepted. Enabling requires a runtime that actually provides "
        "the native experiment tools and passes independent qualification. "
        "Do not put build outputs or test claims in the JSON. External trusted execution reconstructs, builds, "
        "qualifies and evaluates a proposed revision. Source edits cannot alter this production Contract, "
        "the host build recipe, task goals, authority, evaluation rules or version adoption. "
        if revision else "Each must be nonempty UTF-8 without NUL, at most 65536 bytes. "
    )
    return {
        "id": "pct_" + identity, "scope": "growth-" + identity,
        "goal": "Produce the requested candidate AgentRevision artifacts for independent development evaluation." if revision
                else "Produce the requested candidate policy text artifacts for independent development evaluation.",
        "brief": instruction + "\n\nWrite exactly these required artifacts in the working directory: "
                 + ", ".join(artifacts) + ". " + instructions +
                 "These are proposals. You cannot alter evaluation, scores, study assignments or adoption. "
                 "Use contract_check and report ready when these artifacts are complete. "
                 "Your obligation ends with this artifact handoff; future scoring is out of scope.\n\n"
                 + ("Research-parent" if specification.get("binding_role") == "research-parent" else "Incumbent")
                 + " solver policy (an artifact to improve, distinct from your execution policy):\n"
                 + frozen_input(specification["binding"]["solver_policy"]).decode() + "\n\n"
                 "Frozen development context and completed development feedback:\n" + context,
        "executionPolicy": frozen_input(specification["binding"]["generator_policy"]).decode(),
        "requires": [], "location": {"directory": directory},
        "model": {"providerID": model.split("/", 1)[0], "id": model.split("/", 1)[1], "variant": variant},
        "trigger": {"type": "immediate"},
        "authority": ["filesystem.read", "filesystem.write", "process.execute"],
        "budget": {"deadline": int(started_at * 1000) + 21600000},
        "evidence": {"type": "principal", "claim": "The frozen native handoff contains syntactically valid proposal artifacts; no build, qualification, adoption or performance claim.",
                     "replay": {"checks": [{"argv": ["python3", "-I", "-c", check], "cwd": ".", "timeout": 10000, "exit": 0}],
                                "protected": [], "artifacts": artifacts}},
        "resolution": {"maxAttempts": 3, "retryDelay": 0},
    }


def native_generation(api, output, specification, payload, export, cancelled=lambda: False):
    """Observe native execution; never perform a provider/tool loop in Python."""
    record(output / "issue.json", payload)
    session_id = None
    try:
        if cancelled():
            raise GrowthError("generation cancelled before native admission")
        admitted = api.issue(payload)
        record(output / "admission.json", admitted)
        if admitted["data"]["id"] != payload["id"]:
            raise GrowthError("generator admission identity mismatch")
        while True:
            execution = api.get_execution(payload["id"])
            if execution:
                session_id = execution["sessionID"]
                if execution.get("executionPolicy") != payload["executionPolicy"]:
                    raise GrowthError("native generator rho binding mismatch")
            contract = api.get_contract(payload["id"])
            if contract and contract["status"] in {"verification", "discharged", "escalated", "released"}:
                break
            if cancelled() or time.time() * 1000 >= payload["budget"]["deadline"]:
                raise GrowthError("generator cancelled or original six-hour deadline reached")
            time.sleep(0.2)
        record(output / "native-terminal.json", {"contract": contract, "execution": execution})
        if contract["revision"] != 1 or contract["spec"]["budget"] != payload["budget"] or contract.get("pendingRevision"):
            raise GrowthError("generator terms or original deadline changed")
        if contract["status"] != "verification" or not contract.get("handoff", {}).get("replay", {}).get("passed"):
            raise GrowthError("generator did not provide a replayed native handoff")
        destination, receipt = export(payload["id"])
        record(output / "export.json", receipt)
        if receipt["contractID"] != payload["id"] or receipt["subjectHash"] != contract["handoff"]["subjectHash"]:
            raise GrowthError("export does not match native handoff")
        artifacts = {}
        for key, (name, limit, empty) in generator_artifacts(specification).items():
            source = destination / name
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or not (empty or info.st_size > 0) or info.st_size > limit:
                    raise GrowthError("exported proposal must be a bounded regular file")
                data = stream.read(limit + 1)
            if len(data) > limit or b"\0" in data or not (data.decode("utf-8").strip() or empty):
                raise GrowthError("exported proposal violates its UTF-8 artifact bounds")
            if key in {"solver_policy", "generator_policy"}:
                load_policy_artifact(source, sha256(data))
            if key == "revision_proposal":
                from scripts.agent_revision import validate_proposal

                validate_proposal(json.loads(data), specification["binding"]["agent_revision"]["sha256"])
            path = output / "artifacts" / source.name
            seal(path, data)
            artifacts[key] = {"path": str(path), "sha256": sha256(data)}
        # This attests only the native artifact handoff, never policy adoption.
        evidence = record(output / "handoff-evidence.json", {"contractID": payload["id"],
                           "handoff": contract["handoff"], "artifacts": artifacts})
        record(output / "attestation.json", api.attest(payload["id"], sha256(encode(evidence))))
        final = api.get_contract(payload["id"])
        if final["status"] != "discharged":
            raise GrowthError("native proposal-production Contract did not discharge")
        record(output / "final-contract.json", final)
        return {"status": "completed", "artifacts": artifacts, "contract_id": payload["id"],
                "session_id": session_id, "generator_binding": specification["binding"]["generator_policy"],
                "solver_input": specification["binding"]["solver_policy"],
                **({"parent_revision": specification["binding"]["agent_revision"]}
                   if "agent_revision" in specification["binding"] else {}),
                "subject_hash": receipt["subjectHash"], "deadline": payload["budget"]["deadline"]}
    finally:
        if session_id:
            # Full pagination: no cumulative message, turn or request sentinel.
            cursor, pages, seen = None, [], set()
            try:
                while True:
                    query = {"limit": 200, **({"cursor": cursor} if cursor else {"order": "asc"})}
                    page = api._mapping(api._request("GET", f"/api/session/{session_id}/message?{urlencode(query)}"))
                    record(output / "messages" / f"{len(pages):06d}.json", page)
                    pages.extend(page["data"])
                    cursor = page["cursor"].get("next")
                    if cursor is None:
                        break
                    if cursor in seen:
                        raise GrowthError("native message cursor cycle")
                    seen.add(cursor)
                usage = [message for message in pages if message["type"] in {"assistant", "compaction"}]
                record(output / "usage.json", {
                    "known_cost": str(sum((Decimal(str(message["cost"])) for message in usage if message.get("cost") is not None), Decimal())),
                    "unknown_cost_messages": sum(message.get("cost") is None for message in usage),
                    "unknown_token_messages": sum(message.get("tokens") is None for message in usage),
                    "provider_turns_projected": len(usage), "complete": all(message.get("cost") is not None and message.get("tokens") is not None for message in usage),
                    "scope": "native projection; gateway request ledger retained separately, never assume unknown usage is zero",
                })
            finally:
                api.interrupt(session_id)


def _close_generator_workspace(workspace, job, output, *, original=None):
    """Cleanup faults remain infrastructure failures, including during another failure."""
    errors = []
    try:
        workspace.stop()
    except BaseException as error:
        errors.append(("workspace-stop", error))
    usage, accounting = {}, {}
    try:
        if (output / "usage.json").exists():
            usage = read(output / "usage.json")
    except Exception as error:
        errors.append(("native-usage", error))
    try:
        accounting = native_accounting(job)
    except Exception as error:
        errors.append(("gateway-accounting", error))
    try:
        gateway = accounting.get("gateway", {})
        record(output / "accounting.json", {**accounting, "native_projection": usage,
            "known_cost": usage.get("known_cost"),
            "usage_incomplete": bool(errors) or not usage.get("complete", False) or not gateway.get("present")
                or gateway.get("unreadable", False) or bool(gateway.get("open") or gateway.get("missing_usage")),
            "scope": "generation accounting retained on success, failure and cancellation; projection and gateway overlap"})
    except Exception as error:
        errors.append(("accounting-publication", error))
    if errors:
        try:
            record(output / "cleanup-failure.json", {
                "original": None if original is None else {"type": type(original).__name__, "error": str(original)[:4096]},
                "errors": [{"phase": phase, "type": type(error).__name__, "error": str(error)[:4096]} for phase, error in errors],
            })
        except Exception as publication:
            raise GrowthInfrastructureError(f"generator cleanup failed; evidence publication also failed: {type(publication).__name__}") from (original or errors[0][1])
        for _, error in errors:
            if not isinstance(error, Exception):
                raise error
        raise GrowthInfrastructureError(f"generator cleanup or accounting failed; evidence {output}") from (original or errors[0][1])


@contextmanager
def _generator_workspace_lease(request, output):
    from scripts.campaign_workspace import WorkspaceError, WorkspaceLease

    try:
        with WorkspaceLease(request) as lease:
            yield lease
    except WorkspaceError as error:
        try:
            record(output / "workspace-failure.json", {"type": type(error).__name__, "error": str(error)[:4096]})
        except Exception as publication:
            raise GrowthInfrastructureError(f"generator workspace failed; evidence publication also failed: {type(publication).__name__}") from error
        raise GrowthInfrastructureError(f"generator workspace boundary failed; evidence {output}") from error


def runtime_configuration(study, root, binding):
    """Change only the bound Agent implementation and policy within frozen study coordinates."""
    raw = yaml.safe_load((root / "config.yaml").read_bytes())
    if "agent_revision" in binding:
        from scripts.agent_revision import runtime_config, validate_revision
        from scripts.campaign_experiments import validate_experiments

        revision = validate_revision(binding["agent_revision"], study["revision_recipe"])
        if "experiments" in raw:
            initial = validate_revision(study["initial"]["agent_revision"], study["revision_recipe"])
            try:
                initial_experiments = validate_experiments(raw["experiments"])
            except ValueError as error:
                raise GrowthError(f"invalid initial configuration experiments: {error}") from error
            if initial_experiments != initial.get("runtime_options", {}).get("experiments"):
                raise GrowthError("initial configuration experiments differ from its complete AgentRevision")
            # This is an initial-revision-derived field, not a study-wide option.
            # The selected revision below supplies its own validated tool configuration.
            raw.pop("experiments")
        for key in ("solver_policy", "generator_policy"):
            if key in binding and binding[key] != revision[key]:
                raise GrowthError("runtime revision and policy binding disagree")
        if "runtime_sha256" in binding and binding["runtime_sha256"] != revision["binary"]["sha256"]:
            raise GrowthError("runtime revision and score runtime identity disagree")
        raw = runtime_config(raw, binding["agent_revision"], study["revision_recipe"])
    raw["arms"]["procontract"].update(execution_policy="artifact", runtime_protocol="policy-split-v1",
        policy_file=binding["solver_policy"]["path"], policy_sha256=binding["solver_policy"]["sha256"])
    return raw


def generator_runtime(binding, experiments):
    """Normalize the revision request and its fixed generator-role projection."""
    from scripts.campaign_experiments import validate_runtime_options

    try:
        requested = validate_runtime_options({"experiments": experiments})
    except ValueError as error:
        raise GrowthError(f"invalid generator runtime options: {error}") from error
    return {
        "parent_revision": binding.get("agent_revision"),
        "solver_policy": binding["solver_policy"],
        "generator_policy": binding["generator_policy"],
        "requested_runtime_options": requested,
        "effective_runtime_options": {"experiments": None},
        "runtime_options_reason": "generator_contract_has_no_reference_authority",
    }


def validate_native_handoff(output, specification, payload, result):
    """Recheck sealed native receipts without admitting a Session or issuing provider work."""
    issue = read(output / "issue.json")
    terminal = read(output / "native-terminal.json")
    admission = read(output / "admission.json")
    exported = read(output / "export.json")
    evidence = read(output / "handoff-evidence.json")
    final = read(output / "final-contract.json")
    attestation = read(output / "attestation.json")
    artifacts = result.get("proposal_artifacts", result["artifacts"])
    if issue != payload or result["contract_id"] != payload["id"] or result["deadline"] != payload["budget"]["deadline"]:
        raise GrowthError("native generation no longer matches its admitted payload or original deadline")
    if (result.get("parent_revision") != specification["binding"].get("agent_revision")
            or result.get("generator_binding") != specification["binding"]["generator_policy"]
            or result.get("solver_input") != specification["binding"]["solver_policy"]):
        raise GrowthError("native generation result changed its parent or policy input binding")
    expected_spec = {key: payload[key] for key in ("goal", "brief", "requires", "trigger", "authority", "budget", "evidence", "resolution")}
    for contract in (admission["data"], terminal["contract"], final):
        if (contract["id"] != payload["id"] or contract["revision"] != 1 or contract.get("pendingRevision")
                or contract["scope"] != payload["scope"] or contract["spec"] != expected_spec
                or contract["specHash"] != terminal["contract"]["specHash"]):
            raise GrowthError("native generation Contract receipts changed their fixed terms")
    execution = terminal["execution"]
    if (not execution or execution["contractID"] != payload["id"] or execution["revision"] != 1
            or execution["sessionID"] != result["session_id"] or execution["executionPolicy"] != payload["executionPolicy"]
            or execution["location"] != payload["location"] or execution["model"] != payload["model"]):
        raise GrowthError("native generation execution does not match the bound revision and Session")
    handoff = terminal["contract"].get("handoff", {})
    if (terminal["contract"]["status"] != "verification" or handoff.get("replay", {}).get("passed") is not True
            or handoff.get("subjectHash") != result["subject_hash"] or final["status"] != "discharged"
            or final.get("handoff") != handoff or not final.get("attestationID")
            or exported.get("contractID") != payload["id"] or exported.get("subjectHash") != result["subject_hash"]
            or set(artifacts) != set(generator_artifacts(specification))
            or evidence != {"contractID": payload["id"], "handoff": handoff, "artifacts": artifacts}
            or not re.fullmatch(r"[0-9a-f]{64}", attestation.get("hash", ""))
            or type(attestation.get("frontier")) is not int or attestation["frontier"] < 0):
        raise GrowthError("native generation lacks a matching replayed, exported and discharged handoff")
    for artifact in artifacts.values():
        frozen_input(artifact)
    return result


class ProgramBenchBackend:
    def __init__(self, study, root):
        evaluator_environment(study)
        self.study, self.root = study, root

    def generate(self, output, specification, block):
        evaluator_environment(self.study)
        from scripts.growth_feedback import install_feedback
        previous = closed_feedback(self.study, self.root, specification["generation"])
        record(output / "feedback.json", previous)
        from scripts.campaign_install import prepare_install
        from scripts.campaign_offline import DockerHttpClient
        from scripts.campaign_procontract import HttpProContractApi
        from scripts.campaign_state import JobPaths
        from scripts.campaign_workspace import WorkspaceRequest, ensure_workspace
        from scripts.run_opencode import load_config

        if (self.root / "CANCEL").exists():
            raise GrowthError("generation cancelled before workspace preparation")
        seal(output / "generator-config.yaml", yaml.safe_dump(
            runtime_configuration(self.study, self.root, specification["binding"]), sort_keys=False).encode())
        config = load_config(output / "generator-config.yaml")
        record(output / "generator-runtime.json", generator_runtime(specification["binding"], config.experiments))
        arm = next(arm for arm in config.arms if arm.name == "procontract")
        install_root = self.root / "generator-installs"
        if "agent_revision" in specification["binding"]:
            install_root /= specification["binding"]["agent_revision"]["sha256"]
        install = prepare_install(arm, install_root)
        campaign = "generator-" + sha256(str(output).encode())[:12]
        job = JobPaths(output / campaign / "procontract/policy-generator")
        image = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", self.study["generator"]["image"]],
                                        timeout=30, text=True).strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise GrowthError("generator image did not resolve to immutable local image bytes")
        record(output / "generator-image.json", {"requested": self.study["generator"]["image"], "local_image_id": image})
        # Generator Contracts do not grant reference.run and the neutral generator
        # image is not a ProgramBench oracle. Never activate solver probes here.
        request = WorkspaceRequest(job=job, campaign_root=output, campaign=campaign,
                    instance_id="policy-generator", arm="procontract", image=image, install=install,
                    candidate_root=job.root / "candidates", state_root=job.root / "workspace-state", log_dir=job.root / "logs",
                    credential_env=tuple(key for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENCODE_SERVER_PASSWORD") if os.environ.get(key)),
                    isolated=True, deadline_only=True, reference_binding=False, health_timeout=45,
                    provider_model=config.model.split("/", 1)[1], provider_effort=config.variant)
        with _generator_workspace_lease(request, output) as lease:
            from scripts.campaign_admission import disk_admission, study_disk_minimum
            while not disk_admission(self.root, study_disk_minimum(self.study), output / "disk-admission.json"):
                if (self.root / "CANCEL").exists():
                    raise GrowthError("generation cancelled while disk admission paused")
                time.sleep(0.25)
            if (self.root / "CANCEL").exists():
                raise GrowthError("generation cancelled before container admission")
            workspace = ensure_workspace(request, lease)
            try:
                if workspace.manifest.network_mode != "none":
                    raise GrowthError("generator workspace network isolation absent")
                if "agent_revision" in specification["binding"]:
                    record(output / "source-install.json", install_revision_source(
                        workspace, specification["binding"]["agent_revision"], output=output / "source-import"))
                api = HttpProContractApi(workspace.base_url, expected_port=workspace.port,
                                         http_client=DockerHttpClient(workspace.container_id))
                feedback = install_feedback(self.study, self.root, specification["generation"], workspace)
                if feedback is not None:
                    record(output / "feedback-install.json", feedback)
                context = generator_context(self.root, output)
                started_at = time.time()
                payload = generator_payload(output, specification, self.study["generator"]["instruction"], context, config.model, config.variant,
                                            started_at=started_at)
                # The gateway pins this envelope and reads its exact original deadline.
                record(job.root / "procontract-issue.json", {"payload": payload, "startedAt": started_at})

                def export(identifier):
                    destination = workspace.state_dir / "growth-export"
                    return destination, api.export(workspace, identifier, destination)

                result = native_generation(api, output, specification, payload, export, lambda: (self.root / "CANCEL").exists())
                usage = read(output / "usage.json")
                gateway = native_accounting(job.root)["gateway"]
                result = {**result, **usage, "gateway": gateway,
                          "usage_incomplete": not usage["complete"] or not gateway.get("present") or gateway.get("unreadable", False)
                              or bool(gateway.get("open") or gateway.get("missing_usage"))}
            finally:
                _close_generator_workspace(workspace, job.root, output, original=sys.exc_info()[1])
        if specification["mutable_objects"] == ["agent_revision"]:
            from scripts.agent_revision import build_revision, validate_revision

            reference = build_revision(output / "revision", specification["binding"]["agent_revision"],
                result["artifacts"], self.study["revision_recipe"], cancelled=lambda: (self.root / "CANCEL").exists())
            revision = validate_revision(reference, self.study["revision_recipe"], executable=False)
            origin = record(output / "revision-origin.json", {"parent_revision": result["parent_revision"],
                "contract_id": result["contract_id"], "subject_hash": result["subject_hash"],
                "proposal_artifacts": result["artifacts"], "agent_revision": reference})
            result = {**result, "status": "completed" if revision["status"] == "qualified" else "proposal-only",
                      "proposal_artifacts": result["artifacts"],
                      "artifacts": {"solver_policy": revision["solver_policy"],
                                    "generator_policy": revision["generator_policy"], "agent_revision": reference},
                      "revision_origin_sha256": sha256(encode(origin))}
        return result

    def validate_generation(self, output, specification, result):
        from scripts.agent_revision import artifact_bytes, validate_revision

        if result["status"] != "completed":
            return result
        evaluator_environment(self.study)
        parent = validate_revision(specification["binding"]["agent_revision"], self.study["revision_recipe"])
        config = runtime_configuration(self.study, self.root, specification["binding"])
        if encode(yaml.safe_load((output / "generator-config.yaml").read_bytes())) != encode(config):
            raise GrowthError("native generator configuration changed its frozen runtime coordinates")
        runtime = generator_runtime(specification["binding"], config.get("experiments"))
        if encode(read(output / "generator-runtime.json")) != encode(runtime):
            raise GrowthError("native generator changed its assigned full parent revision or effective runtime options")
        campaign = "generator-" + sha256(str(output).encode())[:12]
        job = output / campaign / "procontract/policy-generator"
        envelope = read(job / "procontract-issue.json")
        context = generator_context(self.root, output)
        payload = generator_payload(output, specification, self.study["generator"]["instruction"], context,
            config["model"], config["variant"], started_at=envelope["startedAt"])
        if envelope != {"payload": payload, "startedAt": envelope["startedAt"]}:
            raise GrowthError("native generator gateway envelope no longer matches its frozen payload or original deadline")
        validate_native_handoff(output, specification, payload, result)
        workspace = read(job / "workspace-state/workspace.json")
        image = read(output / "generator-image.json")
        if (workspace["binary_sha256"] != parent["binary"]["sha256"] or workspace["network_mode"] != "none"
                or workspace["image"] != image["local_image_id"] or image["requested"] != self.study["generator"]["image"]
                or not isinstance(workspace.get("environment_names"), list)
                or not all(isinstance(name, str) for name in workspace["environment_names"])
                or "OPENCODE_REFERENCE_BINDING" in workspace["environment_names"]
                or workspace["phase"] != "stopped"):
            raise GrowthError("native generator workspace used a different runtime or isolation boundary")
        imported = read(output / "source-install.json")
        if (imported.get("agent_revision_sha256") != specification["binding"]["agent_revision"]["sha256"]
                or imported.get("source_archive_sha256") != parent["source"]["archive"]["sha256"]
                or imported.get("container") != workspace["container_id"]
                or imported.get("destination") != "/candidate/agent-source"
                or imported.get("execution_user") != "agent"
                or type(imported.get("verified", {}).get("uid")) is not int or imported["verified"]["uid"] <= 0
                or imported.get("verified", {}).get("passed") is not True
                or imported["verified"].get("tree_sha256") != parent["source"]["tree_sha256"]
                or imported.get("host_checkout_mounted") is not False or imported.get("host_git_metadata_copied") is not False):
            raise GrowthError("native generator source import does not match its full parent revision")
        exported = read(output / "export.json")
        destination = Path(workspace["state_dir"]) / "growth-export"
        if exported.get("directory") != "/state/growth-export":
            raise GrowthError("native generator exported a different bound state destination")
        for key, (name, limit, _) in generator_artifacts(specification).items():
            artifact_bytes({"path": str(destination / name), "sha256": result["proposal_artifacts"][key]["sha256"]}, limit)
        return result

    def evaluate(self, output, specification):
        # The solver gets only its exact policy and its original task workspace.
        # Neither generation artifacts nor score/adoption directories are mounted.
        evaluator_environment(self.study)
        if (self.root / "CANCEL").exists():
            raise KeyboardInterrupt("explicit study cancellation before evaluation")
        record(output / "evaluator-inputs.json", self.study["evaluator"])
        raw = runtime_configuration(self.study, self.root, specification)
        seal(output / "config.yaml", yaml.safe_dump(raw, sort_keys=False).encode())
        # The frozen runner derives Docker ownership from the campaign basename.
        # Bind it to the full evaluation directory, including study/stage/subject/seed.
        campaign = output / ("growth-eval-" + sha256(str(output.resolve()).encode()))
        arguments = [self.study["runner"].get("python", sys.executable), "-m", "scripts.run_opencode",
                     "--config", str(output / "config.yaml"), "--output", str(campaign), "--arm", "procontract",
                     "--filter", "^(" + "|".join(re.escape(task) for task in specification["instances"]) + ")$",
                     "--limit", str(len(specification["instances"])), "--seed", str(specification["seed"]),
                     "--workers", str(self.study.get("workers", 1))]
        from scripts.campaign_admission import study_disk_minimum
        minimum = study_disk_minimum(self.study)
        if minimum is not None:
            arguments += ["--minimum-disk-free-bytes", str(minimum)]
        for phase, options in [("inference", []), ("scoring", ["--resume", "--evaluate-only"])]:
            scoring_environment = evaluator_environment(self.study)
            if (self.root / "CANCEL").exists():
                raise KeyboardInterrupt("explicit study cancellation before runner phase")
            record(output / f"{phase}-invocation.json", {"argv": arguments + options, "cwd": self.study["runner"]["path"],
                **({"evaluator_environment": scoring_environment,
                    "evaluator_inputs_sha256": sha256((output / "evaluator-inputs.json").read_bytes())} if phase == "scoring" else {})})
            with (output / f"{phase}.log").open("wb") as log:
                runner = Path(self.study["runner"]["path"])
                environment = dict(os.environ) if phase == "inference" else {key: value for key, value in os.environ.items()
                    if key in ["PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR", "XDG_CACHE_HOME", "DOCKER_HOST", "PROGRAMBENCH_BLOB_DIR"]}
                process = subprocess.Popen(arguments + options, cwd=self.study["runner"]["path"], stdout=log, stderr=log,
                                           stdin=subprocess.DEVNULL, start_new_session=True,
                                           env={**environment, "PYTHONPATH": os.pathsep.join([str(runner), str(runner / "src")]),
                                                "PYTHONDONTWRITEBYTECODE": "1",
                                                **(scoring_environment if phase == "scoring" else {})})
                try:
                    while process.poll() is None:
                        if (self.root / "CANCEL").exists():
                            raise KeyboardInterrupt("explicit study cancellation")
                        time.sleep(0.2)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGINT)
                        try:
                            process.wait(timeout=45)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=10)
            record(output / f"{phase}-exit.json", {"exit_code": process.returncode})
        evaluator_environment(self.study)
        record(output / "accounting.json", {"rows": [{"instance_id": task, **native_accounting(campaign / "procontract" / task)}
                                                       for task in specification["instances"]],
                                            "scope": "all assigned solver jobs, including failures without a score report"})
        return self.validate_evaluation(output, specification, None)

    def validate_evaluation(self, output, specification, result):
        """Revalidate a cached canonical score without model, tools or grading."""
        from scripts.campaign_runner import _config_manifest
        from scripts.run_opencode import load_config

        evaluator_environment(self.study)
        expected = runtime_configuration(self.study, self.root, specification)
        if (yaml.safe_load((output / "config.yaml").read_bytes()) != expected
                or read(output / "evaluator-inputs.json") != self.study["evaluator"]):
            raise GrowthError("cached evaluation changed its frozen configuration or evaluator inputs")
        campaign = output / ("growth-eval-" + sha256(str(output.resolve()).encode()))
        codes = [read(output / f"{phase}-exit.json")["exit_code"] for phase in ["inference", "scoring"]]
        actual = independent_scores(campaign, specification, codes, read(self.root / "metadata.json"),
                                    _config_manifest(load_config(output / "config.yaml")))
        if result is not None and encode(actual) != encode(result):
            raise GrowthError("cached canonical evaluation no longer matches its sealed result")
        return actual


def independent_scores(campaign, specification, exit_codes, metadata, expected_config):
    """Verify publication/runner/subject provenance and independently recompute official scores."""
    from scripts.campaign_evaluate import validate_evaluation
    from programbench.eval.eval import EvaluationResult
    from programbench.submission import score_instance, test_results_map
    from programbench.utils.load_data import get_active_branches

    def canonical(value):
        return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())

    def digest(path):
        return sha256(path.read_bytes()) if path.is_file() and not path.is_symlink() else None

    path = campaign / "comparison.json"
    if not path.exists():
        return {"status": "missing-evaluation", "rows": [{"instance_id": task, "status": "missing", "score": None,
                    "errors": ["comparison_missing"], **native_accounting(campaign / "procontract" / task)} for task in specification["instances"]], "exit_codes": exit_codes,
                "known_cost": None, "usage_incomplete": True}
    report, manifest = read(path), read(campaign / "campaign.json")
    if manifest.get("seed") != specification["seed"]:
        raise GrowthError("campaign repeat seed changed")
    bundle = read(campaign / "comparison.bundle.json")
    if ((campaign / "comparison.publish.json").exists() or bundle.get("comparison") != report
            or bundle.get("comparison_sha256") != digest(path)
            or bundle.get("result_markdown_sha256") != digest(campaign / "RESULT.md")
            or sha256(bundle.get("result_markdown", "").encode()) != bundle.get("result_markdown_sha256")):
        raise GrowthError("independent comparison publication is incomplete or changed")
    if report["coordinates"]["campaign_manifest_sha256"] != sha256((campaign / "campaign.json").read_bytes()):
        raise GrowthError("independent report has a different campaign identity")
    if manifest["config"] != expected_config:
        raise GrowthError("scored campaign changed the frozen runtime/model/budget configuration")
    if manifest["programbench"] != {"sha": specification["runner"]["commit"], "dirty": False}:
        raise GrowthError("evaluation runner source changed")
    if (manifest["installs"]["procontract"]["binary_sha256"] != specification["runtime_sha256"]
            or report["coordinates"]["installs"] != manifest["installs"]):
        raise GrowthError("native runtime installation identity changed")
    arms = manifest["config"]["arms"]
    arm = arms["procontract"] if isinstance(arms, dict) else next(arm for arm in arms if arm["name"] == "procontract")
    if arm["policy_artifact"]["sha256"] != specification["solver_policy"]["sha256"]:
        raise GrowthError("scored campaign used a different solver policy")
    if set(manifest["selected_ids"]) != set(specification["instances"]):
        raise GrowthError("scored campaign changed assigned tasks")
    jobs = [[job["instance_id"], job["arm"]] for job in manifest["jobs"]]
    if (sorted(jobs) != sorted([[task, "procontract"] for task in specification["instances"]])
            or report["coordinates"]["campaign_jobs_sha256"] != canonical(jobs)):
        raise GrowthError("campaign job assignments changed")
    if report.get("mode") != "single" or report.get("arms") != ["procontract"]:
        raise GrowthError("wrong comparison mode")
    if len(report["pairs"]) != len(specification["instances"]) or len({pair["instance_id"] for pair in report["pairs"]}) != len(report["pairs"]):
        raise GrowthError("independent report lost or duplicated an assigned task")
    evaluator = report["coordinates"]["evaluator"]["arms"]["procontract"]
    state = read(campaign / "evaluation-state/procontract.json")
    invocation = evaluator["invocation"]
    if (evaluator["state_sha256"] != digest(campaign / "evaluation-state/procontract.json")
            or state.get("status") != "done" or evaluator.get("status") != "done"
            or state.get("exit_code") != 0 or evaluator.get("exit_code") != 0
            or state.get("invocation_hash") != evaluator.get("invocation_hash")
            or canonical(invocation) != evaluator["invocation_hash"]
            or canonical(state["invocation"]) != evaluator["invocation_hash"]
            or invocation["campaign_manifest_sha256"] != digest(campaign / "campaign.json")):
        raise GrowthError("independent evaluator state/invocation is incomplete or changed")
    if state.get("artifact_hashes") != evaluator.get("artifact_hashes"):
        raise GrowthError("evaluator artifact map changed")
    rows = []
    for pair in report["pairs"]:
        item = pair["procontract"]
        task = pair["instance_id"]
        eval_root = campaign / "evaluation/procontract" / task
        native_root = campaign / "procontract" / task
        eval_path = eval_root / f"{task}.eval.json"
        expected_mapping = invocation.get("expected_executable_sha256")
        expected = expected_mapping.get(task) if isinstance(expected_mapping, dict) else None
        problems = []
        if (not isinstance(expected_mapping, dict) or task not in expected_mapping
                or expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected))):
            problems.append("expected_executable_identity_missing_or_invalid")
        submission = invocation.get("submission_sha256", {}).get(task)
        if submission is None or digest(native_root / "submission.tar.gz") != submission or digest(eval_root / "submission.tar.gz") != submission:
            problems.append("submission_identity_missing_or_changed")
        checked = validate_evaluation(eval_path, metadata[task], expected)
        compatibility = [reason for reason in checked.reasons if reason.startswith("unexpected_test:")]
        accepted = compatibility if specification["test_map"] == "official-full-map-v1" else []
        problems.extend(reason for reason in checked.reasons if reason not in accepted)
        problems.extend(reason for reason in item.get("errors", []) if reason not in accepted)
        utility = item.get("utility")
        published = Fraction(utility["numerator"], utility["denominator"]) if utility else None
        if (checked.utility != published or checked.resolved != item.get("resolved") or checked.total != item.get("total")
                or checked.valid != item.get("valid") or checked.valid != pair.get("instance_valid")):
            problems.append("published_canonical_result_disagrees")
        if (checked.eval_sha256 != item.get("artifact_hashes", {}).get(eval_path.name)
                or checked.eval_sha256 != state["artifact_hashes"].get(eval_path.name)):
            problems.append("evaluation_bytes_changed")
        sidecar = eval_root / f"{task}.eval.provenance.json"
        if digest(sidecar) != state["artifact_hashes"].get(sidecar.name) or not sidecar.is_file():
            problems.append("evaluation_provenance_changed")
        elif read(sidecar).get("invocation_hash") != evaluator["invocation_hash"] or read(sidecar).get("eval_sha256") != checked.eval_sha256:
            problems.append("evaluation_provenance_identity_mismatch")
        if checked.utility is not None:
            actual = EvaluationResult.model_validate_json(eval_path.read_text())
            if any(test.branch not in set(get_active_branches(metadata[task])) for test in actual.test_results):
                problems.append("test_from_inactive_branch")
            tests = test_results_map(eval_path, metadata[task])
            canonical_score = Fraction(sum(tests.values()), len(tests)) if tests else None
            if canonical_score is None or canonical_score != checked.utility or float(canonical_score) != score_instance(eval_path, metadata[task]):
                problems.append("official_full_map_score_disagrees")
        native_expected = item.get("artifact_hashes", {}).get("procontract-result.json")
        if native_expected is not None and native_expected != digest(native_root / "procontract-result.json"):
            problems.append("native_result_changed")
        valid = not problems and checked.utility is not None
        rows.append({"instance_id": pair["instance_id"], "status": "valid" if valid else "invalid",
                     "score": float(checked.utility) if valid else None, "resolved": checked.resolved, "total": checked.total,
                     "strict_valid": checked.valid, "compatibility_reasons": compatibility,
                     **native_accounting(native_root), "errors": sorted(set(problems)),
                     "eval_sha256": checked.eval_sha256, "artifact_hashes": item.get("artifact_hashes", {})})
    return {"status": report["status"], "rows": rows, "exit_codes": exit_codes,
            "comparison_sha256": sha256(path.read_bytes()), "campaign_manifest_sha256": sha256((campaign / "campaign.json").read_bytes())}


def native_accounting(root):
    present = (root / "procontract-result.json").is_file()
    result = {}
    accounting_source = "unavailable"
    error_type = None
    if present:
        try:
            result = read(root / "procontract-result.json")
            if not isinstance(result, dict):
                raise ValueError("native result is not an object")
            accounting_source = "native-result"
        except (OSError, ValueError, TypeError) as error:
            result = {}
            accounting_source = "unavailable-native-result"
            error_type = type(error).__name__
    if not present and (root / "procontract-terminal-error.json").exists():
        try:
            result = terminal_failure_accounting(root)
            accounting_source = "terminal-error-receipt"
        except (OSError, ValueError, TypeError, KeyError, ArithmeticError):
            accounting_source = "unavailable-terminal-error-receipt"
    ledger = root / "workspace-state/provider-control/requests.db"
    gateway = {"present": ledger.is_file()}
    if ledger.is_file():
        try:
            connection = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=5)
            try:
                entries = connection.execute("SELECT finished,usage,sent,terminal_at FROM request WHERE peer_pid IS NOT NULL").fetchall()
                gateway.update(admitted=len(entries), open=sum(row[0] is None for row in entries), missing_usage=sum(row[1] is None for row in entries),
                               sent=sum(row[2] is not None for row in entries), terminal=sum(row[3] is not None for row in entries))
            finally:
                connection.close()
        except (sqlite3.Error, OSError):
            gateway["unreadable"] = True
    accounting = {"known_cost": result.get("cost"), "usage_incomplete": result.get("usage_incomplete"),
            "native_usage_incomplete": result.get("usage_incomplete"),
            "completion": result.get("completion"), "gateway": gateway,
            "provider_turns": result.get("provider_turns"), "actions": result.get("actions"),
            "accounting_source": accounting_source, "accounting_error_type": error_type,
            "accounting_note": "Native known cost and gateway requests overlap; unknown usage is never zero-filled"}
    return {**accounting, "usage_incomplete": accounting_incomplete(accounting)}


def accounting_incomplete(accounting):
    """Native completeness cannot close a retained gateway usage gap."""
    gateway = accounting.get("gateway")
    if gateway is None:
        gateway = {}
    return (accounting.get("usage_incomplete") is not False
            or not isinstance(gateway, dict) or gateway.get("unreadable") is True
            or bool(gateway.get("open")) or bool(gateway.get("missing_usage")))


def terminal_failure_accounting(root):
    """Recover only usage from an issuer-bound failed finalization, never a score."""
    path = root / "procontract-terminal-error.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("terminal failure receipt is not a regular file")
    data = path.read_bytes()
    receipt = json.loads(data)
    state = read(root / "state.json")
    issue = (root / "procontract-issue.json").read_bytes()
    declared = json.loads(issue)
    if (not isinstance(receipt, dict) or not isinstance(state, dict)
            or not isinstance(state.get("artifact_hashes"), dict)
            or not isinstance(declared, dict) or not isinstance(declared.get("payload"), dict)):
        raise ValueError("terminal failure binding is malformed")
    if (receipt.get("version") != 1 or receipt.get("deliveryAccepted") is not False
            or receipt.get("containerStopped") is not True
            or state.get("phase") not in {"failed", "interrupted"}
            or receipt.get("phase") not in {"executing", "frozen", "packaged", "preflighted"}
            or type(receipt.get("processGeneration")) is not int
            or receipt["processGeneration"] != state.get("process_generation")
            or receipt.get("resetAt") != state.get("reset_at")
            or receipt.get("containerID") not in state.get("container_ids", [])
            or not isinstance(receipt.get("sessionID"), str) or not receipt["sessionID"]
            or state.get("artifact_hashes", {}).get(path.name) != sha256(data)
            or not receipt.get("commandIdentity")
            or receipt["commandIdentity"] != state.get("command_identity")
            or receipt.get("issueHash") != sha256(issue)
            or receipt.get("contractID") != declared["payload"].get("id")):
        raise ValueError("terminal failure receipt is unbound")
    primary = receipt.get("primaryDiagnosticSHA256")
    if primary is not None:
        path = root / "procontract-delivery-error.json"
        if (path.is_symlink() or not path.is_file() or sha256(path.read_bytes()) != primary
                or state.get("artifact_hashes", {}).get(path.name) != primary):
            raise ValueError("primary failure diagnostic changed")
    usage = receipt["usage"]
    if not isinstance(usage, dict):
        raise ValueError("terminal failure usage is malformed")
    cost = None if usage.get("cost") is None else Decimal(usage["cost"]) if isinstance(usage["cost"], str) else Decimal("NaN")
    if (cost is None and usage.get("usage_incomplete") is not True
            or cost is not None and (not cost.is_finite() or cost < 0)
            or type(usage.get("usage_incomplete")) is not bool
            or any(type(usage.get(key)) is not int or usage[key] < 0 for key in ("provider_turns", "actions"))):
        raise ValueError("terminal failure usage is invalid")
    return {**usage, "completion": "terminal_scan_failed"}


def closed_feedback(study, root, generation):
    """Explicitly expose closed D/P_g and archived proposals, never future P_g."""
    from scripts.growth_feedback import prepare_feedback
    publication = prepare_feedback(study, root, generation)
    previous = []
    for earlier in study["rounds"][:generation]:
        decision = read(root / earlier["id"] / "decision.json")
        from scripts import growth_research_parent
        research = (growth_research_parent.closed(study, root, study["rounds"].index(earlier))
                    if growth_research_parent.validate_config(study) else None)
        proposals = {}
        for name, result in decision["generation_results"].items():
            artifacts = {}
            for key, artifact in result.get("artifacts", {}).items():
                if key != "agent_revision":
                    artifacts[key] = {"sha256": artifact["sha256"], "text": frozen_input(artifact).decode()}
                    continue
                revision = json.loads(frozen_input(artifact))
                proposal = json.loads(frozen_input(revision["proposal"]))
                artifacts[key] = {"sha256": artifact["sha256"], "kind": "agent-revision",
                    "parent_sha256": revision["parent"]["sha256"] if revision["parent"] else None,
                    "source_sha256": revision["source"]["tree_sha256"], "changed_paths": revision["changed_paths"],
                    "rationale": proposal["rationale"] if proposal else None,
                    "metadata": proposal["metadata"] if proposal else None,
                    "source_patch": {"sha256": revision["source_patch"]["sha256"],
                                     "text": frozen_input(revision["source_patch"]).decode()}}
            proposals[name] = {"status": result["status"], "error_type": result.get("error_type"),
                               **({"adopted": decision["winner"] == name} if publication is None else {}),
                               "artifacts": artifacts}
        previous.append({"generation": earlier["id"], "proposals_including_rejected": proposals,
            **({"research_parent": growth_research_parent.public(research)} if research is not None else {}),
            "development_screen": {"instances": study["development_instances"], "reusable": True,
                "results": {name: {key: value for key, value in item.items() if key != "rows"}
                            for name, item in decision["screen"]["comparisons"].items()}},
            "consumed_confirmation": {"instances": earlier["confirmation_instances"], "consumed": True, "fresh_for_future_use": False,
                "winner": decision["winner"], "results": {name: {key: value for key, value in item.items() if key != "rows"}
                                                          for name, item in decision["comparisons"].items()}}
                if decision["confirmation_consumed"] and publication is None else None,
            "decision_sha256": sha256((root / earlier["id"] / "decision.json").read_bytes())})
    if publication is not None:
        return {"generations": [{key: value for key, value in item.items() if key != "consumed_confirmation"} for item in previous],
                "tool_feedback": read(publication["path"])["public"],
                "exposure": "closed D aggregates and native tool-only archives, proposal text; no explicit P results, decisions, trajectories, assistant prose or reasoning",
                "binding_note": "The current pi/rho binding is supplied separately by the controller; this feedback makes no claim about how that binding was selected"}
    return {"generations": previous, "exposure": "completed D and consumed P_g aggregates, proposal text; no reasoning or private test assertions",
            "parent_selection": "current incumbent only; archived proposals inform generation but no multiple-lineage search is implemented"}
