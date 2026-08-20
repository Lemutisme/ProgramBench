from __future__ import annotations

import json
import os
import subprocess
import tarfile
import textwrap
import uuid
from pathlib import Path

import pytest


def test_calculator_fixture_builder_never_uses_root_agent_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = []
    context = tmp_path / "image"
    monkeypatch.setattr(os, "getuid", lambda: 0)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: commands.append(command) or subprocess.CompletedProcess(command, 0),
    )

    _build_calculator_images(context, ("programbench/root-safe:task_cleanroom",))

    dockerfile = context.joinpath("Dockerfile").read_text()
    assert "useradd --uid 0" not in dockerfile
    assert "agent_id=10000" in dockerfile
    assert "getent passwd" in dockerfile and "getent group" in dockerfile
    assert 'groupadd --gid "$agent_id" agent' in dockerfile
    assert 'useradd --uid "$agent_id" --gid "$agent_id"' in dockerfile
    assert commands == [("docker", "build", "-t", "programbench/root-safe:task_cleanroom", str(context))]


@pytest.mark.slow
def test_real_docker_paired_fake_binaries_run_four_jobs_and_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from programbench.candidate import package_workspace, preflight_candidate
    from scripts.campaign_install import load_install_record
    from scripts.campaign_runner import (
        StateStore,
        plan_campaign,
        run_campaign,
        run_job,
    )
    from scripts.campaign_state import JobLease, JobLockedError, JobPaths
    from scripts.campaign_usage import CircuitBreaker
    from scripts.campaign_workspace import (
        DockerCommandError,
        WorkspaceError,
        WorkspaceLease,
        WorkspaceRequest,
        _run_docker,
    )
    from scripts.run_opencode import load_config

    try:
        _run_docker(("docker", "info", "--format", "{{.ServerVersion}}"), 30)
    except DockerCommandError as error:
        pytest.skip(f"Docker daemon unavailable: {error}")

    suffix = uuid.uuid4().hex[:12]
    instance_ids = (f"testorg__calculator-a.{suffix}", f"testorg__calculator-b.{suffix}")
    image_names = tuple(f"programbench/{instance_id.replace('__', '_1776_')}" for instance_id in instance_ids)
    image_tags = tuple(f"{image}:{tag}" for image in image_names for tag in ("task_cleanroom", "task_cleanroom_v6"))
    campaign_root = tmp_path / "paired-campaign"
    campaign_name = campaign_root.name
    repos = {arm: _fake_repo(tmp_path / arm, arm, f"0.0.0-{arm}-{suffix}") for arm in ("procontract", "vanilla")}
    config_path = tmp_path / "campaign.yaml"
    config_path.write_text(_campaign_config(repos))
    instances = [
        {
            "instance_id": instance_id,
            "difficulty": "easy",
            "language": "bash",
            "image_name": f"{image}:task_cleanroom",
        }
        for instance_id, image in zip(instance_ids, image_names, strict=True)
    ]
    monkeypatch.setenv("OPENAI_API_KEY", "paired-integration-provider-key")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "paired-integration-server-password")
    plan = None
    images_built = False

    try:
        _build_calculator_images(tmp_path / "image", image_tags)
        images_built = True
        canary = tmp_path / "preflight-canary"
        canary.mkdir()
        canary.joinpath("calculator.sh").write_text(_calculator_source())
        canary.joinpath("compile.sh").write_text("#!/bin/sh\ncp calculator.sh executable\nchmod +x executable\n")
        preflight_candidate(package_workspace(canary, tmp_path / "canary-run", instance_ids[0]), instance_ids[0])
        plan = plan_campaign(
            load_config(config_path),
            instances,
            output=campaign_root,
            limit=2,
            seed=42,
        )

        assert len(plan.jobs) == 4
        assert {(job.instance_id, job.arm) for job in plan.jobs} == {
            (instance_id, arm) for instance_id in instance_ids for arm in ("procontract", "vanilla")
        }
        records = {arm: load_install_record(campaign_root / f"installs/{arm}/install.json") for arm in repos}
        assert records["procontract"].revision != records["vanilla"].revision
        assert records["procontract"].binary_sha256 != records["vanilla"].binary_sha256
        assert records["procontract"].version != records["vanilla"].version
        assert all(record.supplied_binary is not None and record.worktree is None for record in records.values())

        breaker = CircuitBreaker.from_campaign_config(plan.config)
        first = run_campaign(
            plan.jobs,
            workers=2,
            breaker=breaker,
            state_path=campaign_root / "breaker.json",
            execute=lambda candidate, stop: run_job(plan, candidate, breaker=breaker, stop_token=stop),
        )

        assert first.completed == 4
        assert first.failed == first.running == 0
        before_resume = {}
        for candidate in plan.jobs:
            paths = JobPaths(campaign_root / candidate.arm / candidate.instance_id)
            state = StateStore(paths.state).read()
            submission = paths.submission
            assert state.phase == "done"
            assert {"packaged", "preflighted", "done"} <= set(state.phase_timestamps or {})
            assert submission.is_file()
            with tarfile.open(submission, "r:gz") as archive:
                assert archive.getnames() == ["calculator.sh", "compile.sh"]
            if candidate.arm == "vanilla":
                assert paths.root.joinpath("vanilla-preflight.json").is_file()
            else:
                result = json.loads(paths.root.joinpath("procontract-result.json").read_text())
                assert result["completion"] == "discharged"
                assert result["quiet"] is True
                assert paths.root.joinpath("procontract-preflight.json").is_file()
            fake_state_path = paths.root / "workspace-state/opencode-state/data/fake-opencode.json"
            fake_state = json.loads(fake_state_path.read_text())
            assert fake_state["sessions"] == fake_state["prompts"] == fake_state["model_calls"] == 1
            if candidate.arm == "vanilla":
                assert fake_state["contracts"] == fake_state["exports"] == fake_state["attestations"] == 0
                assert fake_state["starts"] == 1
            else:
                assert fake_state["contracts"] == fake_state["exports"] == fake_state["attestations"] == 1
                assert fake_state["quiet_polls"] >= 2
                assert fake_state["contract"]["status"] == "discharged"
                assert fake_state["starts"] == 2
            before_resume[(candidate.instance_id, candidate.arm)] = fake_state

        resumed = plan_campaign(
            load_config(config_path),
            instances,
            output=campaign_root,
            limit=2,
            seed=42,
            resume=True,
        )
        second = run_campaign(
            resumed.jobs,
            workers=2,
            execute=lambda candidate, stop: run_job(
                resumed,
                candidate,
                breaker=CircuitBreaker.from_campaign_config(resumed.config),
                resume=True,
                stop_token=stop,
            ),
        )

        assert second.completed == 4
        assert second.failed == second.running == 0
        for candidate in resumed.jobs:
            fake_state_path = (
                campaign_root
                / candidate.arm
                / candidate.instance_id
                / "workspace-state/opencode-state/data/fake-opencode.json"
            )
            assert json.loads(fake_state_path.read_text()) == before_resume[(candidate.instance_id, candidate.arm)]

        locked_job = JobPaths(campaign_root / "vanilla" / instance_ids[0])
        with JobLease(locked_job, resume=True, redo=False, retry_infra=False):
            with pytest.raises(JobLockedError):
                with JobLease(locked_job, resume=True, redo=False, retry_infra=False):
                    pass

        request = WorkspaceRequest(
            job=locked_job,
            campaign_root=campaign_root.parent,
            campaign=campaign_name,
            instance_id=instance_ids[0],
            arm="vanilla",
            image=f"{image_names[0]}:task_cleanroom",
            install=records["vanilla"],
            candidate_root=locked_job.root / "candidates",
            state_root=locked_job.root / "workspace-state",
            log_dir=locked_job.root / "logs",
            credential_env=("OPENAI_API_KEY", "OPENCODE_SERVER_PASSWORD"),
        )
        with WorkspaceLease(request):
            with pytest.raises(WorkspaceError, match="already held"):
                with WorkspaceLease(request):
                    pass

    finally:
        try:
            containers = _run_docker(
                ("docker", "ps", "-aq", "--filter", f"label=programbench.campaign={campaign_name}"),
                30,
            ).split()
            if containers:
                _run_docker(("docker", "rm", "-f", *containers), 60)
            assert (
                _run_docker(
                    ("docker", "ps", "-aq", "--filter", f"label=programbench.campaign={campaign_name}"),
                    30,
                ).strip()
                == ""
            )
        finally:
            if images_built:
                try:
                    _run_docker(
                        (
                            "docker",
                            "run",
                            "--rm",
                            "--user",
                            "root",
                            "-v",
                            f"{tmp_path}:/cleanup",
                            "--entrypoint",
                            "chmod",
                            image_tags[0],
                            "-R",
                            "a+rwX",
                            "/cleanup",
                        ),
                        60,
                    )
                finally:
                    _run_docker(("docker", "rmi", "-f", *image_tags), 120)
                for tag in image_tags:
                    with pytest.raises(DockerCommandError):
                        _run_docker(("docker", "image", "inspect", tag), 30)
            for repo, _ in repos.values():
                subprocess.run(("git", "-C", str(repo), "worktree", "prune"), check=True, timeout=30)
            if plan is not None:
                assert all(getattr(record, "worktree", None) is None for record in plan.installs.values())


def _fake_repo(root: Path, arm: str, version: str) -> tuple[Path, Path]:
    root.mkdir()
    binary = root / "opencode"
    binary.write_text(_fake_opencode_source(arm, version))
    binary.chmod(0o755)
    subprocess.run(("git", "init", "-q", str(root)), check=True, timeout=30)
    subprocess.run(("git", "-C", str(root), "add", "opencode"), check=True, timeout=30)
    subprocess.run(
        (
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-qm",
            arm,
        ),
        check=True,
        timeout=30,
    )
    return root, binary


def _campaign_config(repos: dict[str, tuple[Path, Path]]) -> str:
    return textwrap.dedent(
        f"""\
        model: openai/gpt-5.6-luna
        variant: max
        limits:
          wall_time_seconds: 21600
          provider_turns: 1000
          procontract_actions: 4000
          total_cost_soft: 100
          total_cost_hard: 150
        cache:
          warmup_turns: 10
          minimum_read_ratio: 0.8
          stop_after_consecutive_failures: 2
        arms:
          procontract:
            repo: {repos["procontract"][0]}
            revision: HEAD
            mode: procontract
            binary: {repos["procontract"][1]}
          vanilla:
            repo: {repos["vanilla"][0]}
            revision: HEAD
            mode: vanilla
            binary: {repos["vanilla"][1]}
        excluded: []
        """
    )


def _build_calculator_images(context: Path, tags: tuple[str, ...]) -> None:
    fixture = Path(__file__).parents[1] / "src/programbench/data/tasks/testorg__calculator.abc1234"
    context.mkdir()
    context.joinpath("reference").write_text(_calculator_source())
    context.joinpath("Dockerfile").write_text(
        fixture.joinpath("Dockerfile").read_text()
        + "\nRUN agent_id=10000; "
        + 'while getent passwd "$agent_id" >/dev/null || getent group "$agent_id" >/dev/null; '
        + 'do agent_id=$((agent_id + 1)); done; groupadd --gid "$agent_id" agent && '
        + 'useradd --uid "$agent_id" --gid "$agent_id" --create-home agent\n'
        + "COPY reference /workspace/executable\n"
        + "RUN chmod 755 /workspace/executable && chown -R agent:agent /workspace\n"
    )
    subprocess.run(
        ("docker", "build", *(value for tag in tags for value in ("-t", tag)), str(context)),
        check=True,
        timeout=600,
    )


def _calculator_source() -> str:
    return textwrap.dedent(
        """\
        #!/bin/sh
        case "$2" in
          +) echo $(($1 + $3)) ;;
          -) echo $(($1 - $3)) ;;
          '*') echo $(($1 * $3)) ;;
          *) exit 2 ;;
        esac
        """
    )


def _fake_opencode_source(arm: str, version: str) -> str:
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        import base64
        import hashlib
        import json
        import os
        import shutil
        import subprocess
        import sys
        import threading
        import time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from pathlib import Path
        from urllib.parse import urlparse

        ARM = {arm!r}
        VERSION = {version!r}
        TASK = "Implement the documented program in this cleanroom. Produce source, a relocatable compile.sh, and ./executable. Use the reference only for black-box experiments."
        CALCULATOR = {_calculator_source()!r}
        COMPILE = "#!/bin/sh\\ncp calculator.sh executable\\nchmod +x executable\\n"
        LOCK = threading.Lock()


        def state_path():
            return Path(os.environ.get("XDG_DATA_HOME", "/tmp")) / "fake-opencode.json"


        def load():
            path = state_path()
            if path.exists():
                return json.loads(path.read_text())
            return {{
                "api_calls": 0,
                "attestations": 0,
                "contract": None,
                "contracts": 0,
                "execution": None,
                "exports": 0,
                "messages": [],
                "model_calls": 0,
                "prompts": 0,
                "quiet_polls": 0,
                "session": None,
                "sessions": 0,
                "starts": 0,
            }}


        def save(value):
            path = state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            pending = path.with_suffix(".pending")
            pending.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")))
            pending.replace(path)


        def mutate(operation):
            with LOCK:
                value = load()
                result = operation(value)
                save(value)
                return result


        def hash_json(value):
            return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


        def write_solution(root, git=False):
            if root.exists() and git:
                shutil.rmtree(root)
            root.mkdir(parents=True, exist_ok=True)
            root.joinpath("calculator.sh").write_text(CALCULATOR)
            root.joinpath("compile.sh").write_text(COMPILE)
            root.joinpath("executable").write_text(CALCULATOR)
            for name in ("calculator.sh", "compile.sh", "executable"):
                root.joinpath(name).chmod(0o755)
            if not git:
                return None
            subprocess.run(("git", "init", "-q", str(root)), check=True)
            subprocess.run(("git", "-C", str(root), "add", "calculator.sh", "compile.sh", "executable"), check=True)
            environment = {{
                **os.environ,
                "GIT_AUTHOR_NAME": "fixture",
                "GIT_AUTHOR_EMAIL": "fixture@example.com",
                "GIT_COMMITTER_NAME": "fixture",
                "GIT_COMMITTER_EMAIL": "fixture@example.com",
                "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
                "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
            }}
            subprocess.run(("git", "-C", str(root), "commit", "-qm", "snapshot"), check=True, env=environment)
            return subprocess.run(
                ("git", "-C", str(root), "rev-parse", "HEAD^{{tree}}"),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()


        def assistant(message_id):
            return {{
                "id": "msg_assistant_" + message_id[-12:],
                "type": "assistant",
                "time": {{"created": 1, "completed": 2}},
                "agent": "build",
                "model": {{"providerID": "openai", "id": "gpt-5.6-luna", "variant": "max"}},
                "content": [{{"type": "text", "text": "calculator complete"}}],
                "finish": "stop",
                "cost": 0.01,
                "tokens": {{"input": 10, "output": 3, "reasoning": 0, "cache": {{"read": 0, "write": 0}}}},
            }}


        def contract_values(payload):
            write_solution(Path("/candidate"))
            subject_hash = write_solution(state_path().parent / "snapshot", git=True)
            spec = {{
                "trigger": {{"type": "immediate"}},
                "goal": payload["goal"],
                "policy": payload["policy"],
                "brief": payload["brief"],
                "requires": [],
                "authority": ["filesystem.read", "filesystem.write", "process.execute"],
                "budget": {{
                    "turns": payload["budget"]["turns"],
                    "actions": payload["budget"]["actions"],
                    "deadline": payload["budget"]["deadline"],
                }},
                "evidence": {{
                    "type": "principal",
                    "claim": payload["evidence"]["claim"],
                    "replay": payload["evidence"]["replay"],
                }},
                "resolution": {{"maxAttempts": 1, "retryDelay": 0}},
            }}
            contract = {{
                "id": payload["id"],
                "scope": payload["scope"],
                "spec": spec,
                "issuer": "local-owner",
                "revision": 1,
                "status": "verification",
                "specHash": hash_json(spec),
                "handoff": {{
                    "summary": "calculator ready",
                    "uncertainties": [],
                    "subjectHash": subject_hash,
                    "replay": {{
                        "policyHash": hash_json(payload["evidence"]["replay"]),
                        "subjectHash": subject_hash,
                        "evidenceHash": "native-replay-evidence",
                        "passed": True,
                        "summary": "Replay verification passed",
                    }},
                    "time": int(time.time() * 1000),
                }},
            }}
            execution = {{
                "contractID": payload["id"],
                "revision": 1,
                "location": payload["location"],
                "model": payload["model"],
                "sessionID": "ses_contract_" + payload["id"][-12:],
                "promptID": "msg_contract_" + payload["id"][-12:],
                "dispatched": True,
                "attempts": 1,
                "nextActionAt": int(time.time() * 1000),
                "turnsUsed": 1,
                "actionsUsed": 1,
                "leaseOwner": "scheduler",
                "leaseExpiresAt": int(time.time() * 1000) + 60000,
            }}
            return contract, execution


        if sys.argv[1:] == ["--version"]:
            print(VERSION)
            raise SystemExit(0)
        if sys.argv[1:] == ["models"]:
            print("openai/gpt-5.6-luna")
            raise SystemExit(0)
        if sys.argv[1:3] == ["contract", "export"]:
            contract_id, destination = sys.argv[3:5]
            subject_hash = write_solution(Path(destination), git=True)
            def exported(value):
                if value["contract"]["id"] != contract_id or value["contract"]["handoff"]["subjectHash"] != subject_hash:
                    raise RuntimeError("export identity mismatch")
                value["exports"] += 1
            mutate(exported)
            print(json.dumps({{"contractID": contract_id, "subjectHash": subject_hash, "directory": destination}}))
            raise SystemExit(0)
        if not sys.argv[1:] or sys.argv[1] != "serve":
            raise SystemExit(2)

        hostname = sys.argv[sys.argv.index("--hostname") + 1]
        port = int(sys.argv[sys.argv.index("--port") + 1])
        mutate(lambda value: value.__setitem__("starts", value["starts"] + 1))


        class Handler(BaseHTTPRequestHandler):
            def body(self):
                return json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))) or b"{{}}")

            def respond(self, value, status=200):
                encoded = b"" if status == 204 else json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
                self.send_response(status)
                if encoded:
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                if encoded:
                    self.wfile.write(encoded)

            def count(self):
                mutate(lambda value: value.__setitem__("api_calls", value["api_calls"] + 1))

            def authorized(self):
                expected = "Basic " + base64.b64encode(
                    (os.environ.get("OPENCODE_SERVER_USERNAME", "opencode") + ":" + os.environ["OPENCODE_SERVER_PASSWORD"]).encode()
                ).decode()
                return self.headers.get("authorization") == expected

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path in ("/health", "/global/health"):
                    return self.respond({{"healthy": True}})
                self.count()
                value = load()
                if parsed.path.startswith("/api/contract") and not self.authorized():
                    return self.respond({{"error": "unauthorized"}}, 401)
                if parsed.path == "/api/session/active":
                    return self.respond({{"data": {{}}}})
                if parsed.path == "/api/contract/quiet":
                    mutate(lambda current: current.__setitem__("quiet_polls", current["quiet_polls"] + 1))
                    value = load()
                    contract = value["contract"]
                    quiet = contract["status"] == "discharged"
                    return self.respond({{
                        "scope": contract["scope"],
                        "quiet": quiet,
                        "frontier": value["attestations"],
                        "ledgerHash": "ledger",
                        "stateHash": "state",
                        "outstanding": [] if quiet else [contract["id"]],
                    }})
                if parsed.path.startswith("/api/contract/") and parsed.path.endswith("/execution"):
                    return self.respond(value["execution"])
                if parsed.path.startswith("/api/contract/"):
                    return self.respond({{"data": value["contract"]}})
                if "/message/" in parsed.path:
                    prompt = value["messages"][0] if value["messages"] else None
                    if prompt is None:
                        return self.respond({{"error": "missing"}}, 404)
                    return self.respond({{"data": prompt}})
                if parsed.path.endswith("/message"):
                    return self.respond({{"data": value["messages"], "cursor": {{}}}})
                if parsed.path.startswith("/api/session/"):
                    if value["session"] is None:
                        return self.respond({{"error": "missing"}}, 404)
                    return self.respond({{"data": value["session"]}})
                return self.respond({{"error": "missing"}}, 404)

            def do_PATCH(self):
                self.count()
                return self.respond({{}})

            def do_POST(self):
                parsed = urlparse(self.path)
                self.count()
                if parsed.path.startswith("/api/contract") and not self.authorized():
                    return self.respond({{"error": "unauthorized"}}, 401)
                if parsed.path == "/api/session":
                    body = self.body()
                    def create(value):
                        value["session"] = body
                        value["sessions"] += 1
                    mutate(create)
                    return self.respond({{"data": body}})
                if parsed.path.endswith("/prompt"):
                    body = self.body()
                    write_solution(Path("/candidate"))
                    session_id = parsed.path.split("/")[3]
                    def prompt(value):
                        value["prompts"] += 1
                        value["model_calls"] += 1
                        value["messages"] = [
                            {{"id": body["id"], "type": "user", "text": body["prompt"]["text"]}},
                            assistant(body["id"]),
                        ]
                    mutate(prompt)
                    return self.respond({{"data": {{
                        "admittedSeq": 1,
                        "promotedSeq": 1,
                        "id": body["id"],
                        "sessionID": session_id,
                        "prompt": body["prompt"],
                        "delivery": body["delivery"],
                        "timeCreated": time.time(),
                    }}}})
                if parsed.path == "/api/contract":
                    payload = self.body()
                    contract, execution = contract_values(payload)
                    def issue(value):
                        value["contract"] = contract
                        value["contracts"] += 1
                        value["execution"] = execution
                        value["messages"] = [assistant(execution["promptID"])]
                        value["model_calls"] += 1
                        value["prompts"] += 1
                        value["session"] = {{"id": execution["sessionID"]}}
                        value["sessions"] += 1
                    mutate(issue)
                    return self.respond({{
                        "data": contract,
                        "execution": execution,
                        "receipt": {{"frontier": 1, "hash": "issued"}},
                    }})
                if parsed.path.endswith("/attestation"):
                    body = self.body()
                    def attest(value):
                        value["attestations"] += 1
                        value["contract"] = {{
                            **value["contract"],
                            "status": "discharged",
                            "attestationID": "pca_" + body["evidenceHash"][:16],
                        }}
                    mutate(attest)
                    return self.respond({{"frontier": 2, "hash": "attested"}})
                if parsed.path.endswith("/interrupt"):
                    return self.respond(None, 204)
                return self.respond({{"error": "missing"}}, 404)

            def log_message(self, *_args):
                return


        ThreadingHTTPServer((hostname, port), Handler).serve_forever()
        """
    )
