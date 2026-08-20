from __future__ import annotations

import hashlib
import json
import os
import subprocess
import textwrap
import time
import uuid
from pathlib import Path

import pytest

from scripts.campaign_install import InstallRecord
from scripts.campaign_procontract import (
    HttpProContractApi,
    ProContractHttpError,
    restart_delivery_workspace,
)
from scripts.campaign_state import JobLease, JobPaths
from scripts.campaign_workspace import (
    DockerCommandError,
    WorkspaceLease,
    WorkspaceRequest,
    _run_docker,
    ensure_workspace,
)


CONTRACT_ID = "pct_integration"
SESSION_ID = "ses_integration"
SCOPE = "scope-integration"
PASSWORD = "integration-contract-secret"


FAKE_OPENCODE = textwrap.dedent(
    f"""\
    #!/usr/bin/env python3
    import argparse
    import base64
    import json
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    from urllib.parse import urlparse

    CONTRACT_ID = {CONTRACT_ID!r}
    SESSION_ID = {SESSION_ID!r}
    SCOPE = {SCOPE!r}
    STATE = Path(os.environ["XDG_DATA_HOME"]) / "procontract-fixture.json"
    LOCK = threading.Lock()


    def load():
        if STATE.exists():
            return json.loads(STATE.read_text())
        return {{
            "starts": 0,
            "attestation_requests": 0,
            "requests": [],
            "contract": {{"id": CONTRACT_ID, "status": "verification"}},
            "execution": {{"contractID": CONTRACT_ID, "sessionID": SESSION_ID}},
            "quiet": False,
        }}


    def save(state):
        STATE.parent.mkdir(parents=True, exist_ok=True)
        pending = STATE.with_suffix(".pending")
        pending.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")))
        pending.replace(STATE)


    class Handler(BaseHTTPRequestHandler):
        def authorized(self):
            expected = "Basic " + base64.b64encode(
                ("opencode:" + os.environ["OPENCODE_SERVER_PASSWORD"]).encode()
            ).decode()
            if self.headers.get("authorization") == expected:
                return True
            self.send_error(401)
            return False

        def respond(self, value):
            body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in {{"/health", "/global/health"}}:
                return self.respond({{"healthy": True}})
            if not self.authorized():
                return
            with LOCK:
                state = load()
                state["requests"].append("GET " + parsed.path)
                save(state)
            if parsed.path == "/api/contract/quiet":
                return self.respond(
                    {{
                        "scope": SCOPE,
                        "quiet": state["quiet"],
                        "frontier": state["attestation_requests"],
                        "ledgerHash": "ledger",
                        "stateHash": "state",
                        "outstanding": [] if state["quiet"] else [CONTRACT_ID],
                    }}
                )
            if parsed.path == "/api/contract/" + CONTRACT_ID:
                return self.respond({{"data": state["contract"]}})
            if parsed.path == "/api/contract/" + CONTRACT_ID + "/execution":
                return self.respond(state["execution"])
            if parsed.path == "/api/session/active":
                return self.respond({{"data": {{}}}})
            if parsed.path == "/api/session/" + SESSION_ID + "/message":
                return self.respond({{"data": [], "cursor": {{}}}})
            self.send_error(404)

        def do_POST(self):
            parsed = urlparse(self.path)
            if not self.authorized():
                return
            if parsed.path != "/api/contract/" + CONTRACT_ID + "/attestation":
                return self.send_error(404)
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            if body != {{"evidenceHash": "principal-evidence"}}:
                return self.send_error(400)
            with LOCK:
                state = load()
                state["requests"].append("POST " + parsed.path)
                state["attestation_requests"] += 1
                state["contract"]["status"] = "discharged"
                state["quiet"] = True
                save(state)
            self.respond({{"frontier": 1, "hash": "attestation"}})

        def log_message(self, _format, *_args):
            return


    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    if args.command != "serve":
        raise SystemExit(2)
    with LOCK:
        state = load()
        state["starts"] += 1
        save(state)
    ThreadingHTTPServer((args.hostname, args.port), Handler).serve_forever()
    """
)


def fixture_install(tmp_path: Path, binary: Path) -> InstallRecord:
    return InstallRecord(
        arm="procontract",
        repo=tmp_path,
        common_dir=tmp_path,
        repo_identity="fixture",
        repo_url=None,
        requested_revision="HEAD",
        revision="a" * 40,
        source_dirty=False,
        supplied_binary=binary,
        worktree=None,
        binary=binary,
        binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        version="fixture",
        install_command=(),
        build_command=(),
        smoke_command=(),
        catalog_command=(),
        platform="linux",
        full_model_catalog_output_sha256=None,
        full_model_catalog_unavailable_reason="not requested",
        full_model_catalog_status="unavailable",
        owned_paths=(),
        publication_phase="manifest_published",
    )


@pytest.mark.slow
def test_real_delivery_restart_rebinds_procontract_api_and_preserves_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    try:
        _run_docker(("docker", "info", "--format", "{{.ServerVersion}}"), 30)
    except DockerCommandError as error:
        pytest.skip(f"Docker daemon unavailable: {error}")

    try:
        base_image = json.loads(
            _run_docker(("docker", "image", "inspect", "--format", "{{json .Id}}", "python:3.11"), 30)
        )
    except DockerCommandError:
        _run_docker(("docker", "pull", "python:3.11"), 300)
        base_image = json.loads(
            _run_docker(("docker", "image", "inspect", "--format", "{{json .Id}}", "python:3.11"), 30)
        )

    tag = f"programbench-procontract-fixture-{uuid.uuid4().hex}:task_cleanroom"
    campaign = f"integration-{uuid.uuid4().hex}"
    context = tmp_path / "image"
    context.mkdir()
    context.joinpath("Dockerfile").write_text(
        f"FROM {base_image}\n"
        "RUN useradd --create-home agent && mkdir -p /workspace && "
        "printf '#!/bin/sh\\necho reference executable\\n' > /workspace/executable && "
        "printf 'ProContract restart fixture\\n' > /workspace/README.md && "
        "chmod 755 /workspace/executable && chmod 644 /workspace/README.md && "
        "chown -R agent:agent /workspace\n"
    )
    binary = tmp_path / "install" / "opencode"
    binary.parent.mkdir()
    binary.write_text(FAKE_OPENCODE)
    binary.chmod(0o755)
    campaign_root = tmp_path / "campaign-output"
    job_paths = JobPaths(campaign_root / campaign / "procontract" / "owner__fixture.integration")
    job_paths.root.mkdir(parents=True)
    events: list[str] = []
    commands: list[tuple[str, ...]] = []

    def docker(command: tuple[str, ...], timeout: float) -> str:
        commands.append(command)
        if len(command) > 1 and command[1] == "stop":
            events.append("stop")
        if len(command) > 1 and command[1] == "start" and "stop" in events:
            events.append("restart")
        return _run_docker(command, timeout)

    request = WorkspaceRequest(
        job=job_paths,
        campaign_root=campaign_root,
        campaign=campaign,
        instance_id="owner__fixture.integration",
        arm="procontract",
        image=tag,
        install=fixture_install(tmp_path, binary),
        candidate_root=job_paths.root / "candidates",
        state_root=job_paths.root / "workspace-state",
        log_dir=job_paths.root / "logs",
        credential_env=("OPENCODE_SERVER_PASSWORD",),
        docker_runner=docker,
        health_timeout=20,
    )
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", PASSWORD)
    image_built = False
    try:
        _run_docker(("docker", "build", "--quiet", "-t", tag, str(context)), 300)
        image_built = True

        with JobLease(job_paths, resume=True, redo=False, retry_infra=False) as job, WorkspaceLease(request) as lease:
            workspace = ensure_workspace(request, lease)
            original_container = workspace.container_id
            old_url = workspace.base_url

            class OrderedHttpProContractApi(HttpProContractApi):
                def rebind(self, base_url: str) -> None:
                    super().rebind(base_url)
                    events.append("rebind")

                def get_contract(self, contract_id: str):
                    contract = super().get_contract(contract_id)
                    events.append("get")
                    return contract

                def attest(self, contract_id: str, evidence_hash: str):
                    response = super().attest(contract_id, evidence_hash)
                    events.append("attest")
                    return response

            api = OrderedHttpProContractApi(old_url, expected_port=workspace.port, password=PASSWORD)
            assert api.get_contract(CONTRACT_ID) == {"id": CONTRACT_ID, "status": "verification"}
            assert job._lock_context is not None and lease._handle_active
            assert workspace.port != 4096
            assert workspace.manifest.mounts[-1].destination == "/opencode"
            assert workspace.manifest.mounts[-1].read_only
            assert workspace.candidate.joinpath("README.md").read_text() == "ProContract restart fixture\n"
            events.clear()

            workspace.stop_for_freeze()
            with pytest.raises(ProContractHttpError):
                api.get_contract(CONTRACT_ID)

            restarted = restart_delivery_workspace(workspace, api)
            assert restarted.container_id == original_container
            assert restarted.manifest.phase == "running"
            assert restarted.base_url != old_url
            assert api.base_url == restarted.base_url
            stale = HttpProContractApi(
                old_url,
                expected_port=workspace.port,
                password=PASSWORD,
                request_timeout=0.5,
            )
            with pytest.raises(ProContractHttpError):
                stale.get_contract(CONTRACT_ID)
            assert api.get_contract(CONTRACT_ID) == {"id": CONTRACT_ID, "status": "verification"}
            assert api.get_execution(CONTRACT_ID) == {"contractID": CONTRACT_ID, "sessionID": SESSION_ID}
            assert api.status(SESSION_ID) == "idle"
            assert api.messages(SESSION_ID) == {"data": []}
            assert api.quiet(SCOPE) == {
                "scope": SCOPE,
                "quiet": False,
                "frontier": 0,
                "ledgerHash": "ledger",
                "stateHash": "state",
                "outstanding": [CONTRACT_ID],
            }
            assert api.attest(CONTRACT_ID, "principal-evidence") == {"frontier": 1, "hash": "attestation"}
            assert api.quiet(SCOPE)["quiet"] is True

            state = json.loads(restarted.state_dir.joinpath("data/procontract-fixture.json").read_text())
            assert state["starts"] == 2
            assert state["attestation_requests"] == 1
            assert state["contract"] == {"id": CONTRACT_ID, "status": "discharged"}
            assert state["execution"] == {"contractID": CONTRACT_ID, "sessionID": SESSION_ID}
            assert state["quiet"] is True
            assert state["requests"] == [
                f"GET /api/contract/{CONTRACT_ID}",
                f"GET /api/contract/{CONTRACT_ID}",
                f"GET /api/contract/{CONTRACT_ID}/execution",
                "GET /api/session/active",
                f"GET /api/session/{SESSION_ID}/message",
                "GET /api/contract/quiet",
                f"POST /api/contract/{CONTRACT_ID}/attestation",
                "GET /api/contract/quiet",
            ]
            assert events == ["stop", "restart", "rebind", "get", "attest"]
            assert len([command for command in commands if len(command) > 1 and command[1] == "create"]) == 1
            assert len([command for command in commands if len(command) > 1 and command[1] == "start"]) == 2
            restarted.stop_for_freeze()
    finally:
        if image_built:
            try:
                containers = _run_docker(
                    ("docker", "ps", "-aq", "--filter", f"label=programbench.campaign={campaign}"), 30
                ).split()
                if containers:
                    _run_docker(("docker", "rm", "-f", *containers), 60)
            finally:
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
                            tag,
                            "-R",
                            "a+rwX",
                            "/cleanup",
                        ),
                        60,
                    )
                finally:
                    _run_docker(("docker", "rmi", "-f", tag), 60)

    assert _run_docker(("docker", "ps", "-aq", "--filter", f"label=programbench.campaign={campaign}"), 30).strip() == ""
    with pytest.raises(DockerCommandError):
        _run_docker(("docker", "image", "inspect", tag), 30)


@pytest.mark.slow
@pytest.mark.parametrize("expected_completion", ["discharged", "pending_revision"])
def test_pinned_real_binary_completes_native_procontract_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expected_completion: str
) -> None:
    from scripts.run_opencode import (
        CircuitBreaker,
        HttpProContractApi,
        JobLease,
        JobState,
        Limits,
        StateStore,
        WorkspaceLease,
        run_procontract,
    )

    binary_value = os.environ.get("PROGRAMBENCH_PROCONTRACT_BINARY")
    if not binary_value:
        pytest.skip("PROGRAMBENCH_PROCONTRACT_BINARY is not set; pinned real ProContract test is opt-in")
    binary = Path(binary_value)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        pytest.fail("PROGRAMBENCH_PROCONTRACT_BINARY must name an executable regular file")
    actual_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
    actual_version = subprocess.run(
        [str(binary), "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    fixture_path = tmp_path / "procontract-binary.json"
    fixture_path.write_text(
        json.dumps(
            {
                "sha256": os.environ.get("PROGRAMBENCH_PROCONTRACT_SHA256", actual_sha256),
                "version": os.environ.get("PROGRAMBENCH_PROCONTRACT_VERSION", actual_version),
            },
            sort_keys=True,
        )
    )
    fixture_metadata = json.loads(fixture_path.read_text())
    assert actual_sha256 == fixture_metadata["sha256"]
    assert actual_version == fixture_metadata["version"]
    try:
        _run_docker(("docker", "info", "--format", "{{.ServerVersion}}"), 30)
    except DockerCommandError as error:
        pytest.skip(f"Docker daemon unavailable: {error}")

    tag = f"programbench-real-procontract-{uuid.uuid4().hex}:task_cleanroom"
    campaign = f"real-contract-{uuid.uuid4().hex}"
    instance_id = f"owner__real.{uuid.uuid4().hex}"
    preflight_image = f"programbench/{instance_id.replace('__', '_1776_')}:task_cleanroom_v6"
    context = tmp_path / "image"
    context.mkdir()
    image_built = False
    preflight_tagged = False
    provider_container: str | None = None
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", PASSWORD)
    monkeypatch.setenv("OPENAI_API_KEY", "deterministic-provider-key")
    try:
        try:
            base_image = json.loads(
                _run_docker(("docker", "image", "inspect", "--format", "{{json .Id}}", "python:3.11"), 30)
            )
        except DockerCommandError:
            _run_docker(("docker", "pull", "python:3.11"), 300)
            base_image = json.loads(
                _run_docker(("docker", "image", "inspect", "--format", "{{json .Id}}", "python:3.11"), 30)
            )
        context.joinpath("Dockerfile").write_text(
            f"FROM {base_image}\n"
            f"RUN useradd --uid {os.getuid()} --create-home agent && mkdir -p /workspace && "
            "printf '#!/bin/sh\\necho reference\\n' > /workspace/executable && "
            "printf 'Real ProContract lifecycle fixture\\n' > /workspace/README.md && "
            "chmod 755 /workspace/executable && chmod 644 /workspace/README.md\n"
        )
        _run_docker(("docker", "build", "--quiet", "-t", tag, str(context)), 300)
        image_built = True
        _run_docker(("docker", "tag", tag, preflight_image), 30)
        preflight_tagged = True
        provider_script = tmp_path / "provider.py"
        provider_source = textwrap.dedent(
            """\
                import json
                from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

                responses = 0

                class Handler(BaseHTTPRequestHandler):
                    def do_POST(self):
                        global responses
                        body = self.rfile.read(int(self.headers.get("content-length", "0")))
                        with open("/record/provider-hits.jsonl", "ab") as output:
                            output.write(body + b"\\n")
                        if self.path.endswith("/responses"):
                            responses += 1
                            if responses == 1:
                                args = json.dumps({"summary":"Candidate and replay are ready.","uncertainties":[]}, separators=(",", ":"))
                                events = [
                                    {"type":"response.created","sequence_number":1,"response":{"id":"resp_programbench","created_at":1,"model":"gpt-5.6-luna","service_tier":None}},
                                    {"type":"response.output_item.added","sequence_number":2,"output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"contract_report_ready","arguments":"","status":"in_progress"}},
                                    {"type":"response.function_call_arguments.delta","sequence_number":3,"output_index":0,"item_id":"fc_1","delta":args},
                                    {"type":"response.function_call_arguments.done","sequence_number":4,"output_index":0,"item_id":"fc_1","arguments":args},
                                    {"type":"response.output_item.done","sequence_number":5,"output_index":0,"item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"contract_report_ready","arguments":args,"status":"completed"}},
                                    {"type":"response.completed","sequence_number":6,"response":{"incomplete_details":None,"service_tier":None,"usage":{"input_tokens":10,"input_tokens_details":{"cached_tokens":0},"output_tokens":3,"output_tokens_details":{"reasoning_tokens":0}}}},
                                ]
                            else:
                                events = [
                                    {"type":"response.created","sequence_number":1,"response":{"id":"resp_programbench_next","created_at":1,"model":"gpt-5.6-luna","service_tier":None}},
                                    {"type":"response.output_item.added","sequence_number":2,"output_index":0,"item":{"type":"message","id":"msg_provider_next"}},
                                    {"type":"response.output_text.delta","sequence_number":3,"item_id":"msg_provider_next","delta":"Settlement recorded.","logprobs":None},
                                    {"type":"response.output_item.done","sequence_number":4,"output_index":0,"item":{"type":"message","id":"msg_provider_next"}},
                                    {"type":"response.completed","sequence_number":5,"response":{"incomplete_details":None,"service_tier":None,"usage":{"input_tokens":10,"input_tokens_details":{"cached_tokens":0},"output_tokens":3,"output_tokens_details":{"reasoning_tokens":0}}}},
                                ]
                            payload = "".join("data: " + json.dumps(event) + "\\n\\n" for event in events) + "data: [DONE]\\n\\n"
                        elif self.path.endswith("/chat/completions"):
                            chunks = [
                                {"id":"chatcmpl-programbench","object":"chat.completion.chunk","choices":[{"delta":{"role":"assistant"}}]},
                                {"id":"chatcmpl-programbench","object":"chat.completion.chunk","choices":[{"delta":{"content":"Delivery is ready."}}]},
                                {"id":"chatcmpl-programbench","object":"chat.completion.chunk","choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":3,"total_tokens":13}},
                            ]
                            payload = "".join("data: " + json.dumps(chunk) + "\\n\\n" for chunk in chunks) + "data: [DONE]\\n\\n"
                        else:
                            self.send_error(404)
                            return
                        encoded = payload.encode()
                        self.send_response(200)
                        self.send_header("content-type", "text/event-stream")
                        self.send_header("content-length", str(len(encoded)))
                        self.end_headers()
                        self.wfile.write(encoded)
                    def log_message(self, _format, *_args):
                        return

                ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
            """
        )
        if expected_completion == "pending_revision":
            provider_source = provider_source.replace("contract_report_ready", "contract_propose_revision").replace(
                '{"summary":"Candidate and replay are ready.","uncertainties":[]}',
                '{"goal":"Revised fixture goal","brief":"Await principal revision decision.","reason":"Need revised scope."}',
            )
        provider_script.write_text(provider_source)
        provider_name = f"programbench-provider-{uuid.uuid4().hex}"
        provider_container = _run_docker(
            (
                "docker",
                "run",
                "-d",
                "--name",
                provider_name,
                "--network",
                "bridge",
                "-v",
                f"{provider_script}:/provider.py:ro",
                "-v",
                f"{tmp_path}:/record:rw",
                "python:3.11",
                "python",
                "/provider.py",
            ),
            60,
        ).strip()
        provider_host = _run_docker(
            (
                "docker",
                "inspect",
                "--format",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                provider_container,
            ),
            30,
        ).strip()
        campaign_root = tmp_path / "campaign-output"
        paths = JobPaths(campaign_root / campaign / "procontract" / instance_id)
        paths.root.mkdir(parents=True)
        request = WorkspaceRequest(
            job=paths,
            campaign_root=campaign_root,
            campaign=campaign,
            instance_id=instance_id,
            arm="procontract",
            image=tag,
            install=fixture_install(tmp_path, binary),
            candidate_root=paths.root / "candidates",
            state_root=paths.root / "workspace-state",
            log_dir=paths.root / "logs",
            credential_env=("OPENAI_API_KEY", "OPENCODE_SERVER_PASSWORD"),
            health_timeout=30,
        )
        started = time.monotonic()

        def sleeper(seconds: float) -> None:
            if time.monotonic() - started > 90:
                raise AssertionError("real ProContract lifecycle exceeded 90 seconds")
            time.sleep(min(seconds, 0.1))

        with JobLease(paths, resume=True, redo=False, retry_infra=False) as job, WorkspaceLease(request) as lease:
            workspace = ensure_workspace(request, lease)
            workspace.stop_for_freeze()
            seed = tmp_path / "seed"
            seed.mkdir()
            seed.joinpath("compile.sh").write_text(
                "#!/bin/sh\nprintf '#!/bin/sh\\necho real-procontract-fixture\\n' > ./executable\nchmod +x ./executable\n"
            )
            seed.joinpath("executable").write_text("#!/bin/sh\necho real-procontract-fixture\n")
            seed.joinpath("opencode.json").write_text(
                json.dumps(
                    {
                        "formatter": False,
                        "lsp": False,
                        "model": "openai/gpt-5.6-luna",
                        "small_model": "openai/gpt-5.6-luna",
                        "provider": {
                            "openai": {
                                "name": "Deterministic OpenAI",
                                "id": "openai",
                                "env": ["OPENAI_API_KEY"],
                                "npm": "@ai-sdk/openai-compatible",
                                "models": {
                                    "gpt-5.6-luna": {
                                        "id": "gpt-5.6-luna",
                                        "name": "Deterministic GPT",
                                        "attachment": False,
                                        "reasoning": False,
                                        "temperature": False,
                                        "tool_call": True,
                                        "release_date": "2026-01-01",
                                        "limit": {"context": 100000, "output": 10000},
                                        "cost": {"input": 1, "output": 1},
                                        "options": {},
                                        "variants": {"max": {}},
                                    }
                                },
                                "options": {
                                    "baseURL": f"http://{provider_host}:8080/v1",
                                },
                            }
                        },
                    }
                )
            )
            _run_docker(
                (
                    "docker",
                    "run",
                    "--rm",
                    "--user",
                    "root",
                    "--network",
                    "none",
                    "-v",
                    f"{seed}:/seed:ro",
                    "-v",
                    f"{workspace.candidate}:/candidate:rw",
                    "-v",
                    f"{workspace.state_dir}:/state:rw",
                    tag,
                    "/bin/sh",
                    "-ec",
                    "cp /seed/compile.sh /seed/executable /candidate/; chmod 755 /candidate/compile.sh /candidate/executable; "
                    "chown -R root:root /candidate; "
                    "git -C /candidate init -q; git -C /candidate config user.name fixture; "
                    "git -C /candidate config user.email fixture@example.com; git -C /candidate add -A; "
                    "git -C /candidate commit -qm snapshot; "
                    "mkdir -p /state/config/opencode; cp /seed/opencode.json /state/config/opencode/opencode.json; "
                    "chown -R agent:agent /candidate /state",
                ),
                60,
            )
            workspace = workspace.restart()
            configured = _run_docker(
                (
                    "docker",
                    "exec",
                    "--user",
                    workspace.manifest.container_user,
                    "--workdir",
                    "/candidate",
                    "--env",
                    f"HOME={workspace.manifest.home}",
                    "--env",
                    f"XDG_CACHE_HOME={workspace.manifest.xdg_cache_home}",
                    "--env",
                    f"XDG_CONFIG_HOME={workspace.manifest.xdg_config_home}",
                    "--env",
                    f"XDG_DATA_HOME={workspace.manifest.xdg_data_home}",
                    workspace.container_id,
                    "/opencode",
                    "debug",
                    "config",
                ),
                30,
            )
            assert provider_host in configured
            _run_docker(
                (
                    "docker",
                    "exec",
                    workspace.container_id,
                    "python",
                    "-c",
                    "import urllib.request; "
                    f"urllib.request.urlopen(urllib.request.Request('http://{provider_host}:8080/v1/chat/completions', "
                    "data=b'{}', headers={'content-type':'application/json'}), timeout=5).read()",
                ),
                15,
            )
            store = StateStore(paths.state)
            store.write(JobState("planned"))
            api = HttpProContractApi(
                workspace.base_url,
                expected_port=workspace.port,
                password=PASSWORD,
            )
            breaker = CircuitBreaker()
            result = run_procontract(
                job,
                store,
                workspace,
                model="openai/gpt-5.6-luna",
                variant="max",
                limits=Limits(21600, 1000, 4000, 100, 150),
                breaker=breaker,
                api=api,
                sleeper=sleeper,
                poll_interval=0.1,
            )

            assert result.completion.value == expected_completion
            if expected_completion == "discharged":
                assert result.quiet is True
                assert result.report_hash is not None
                assert result.attestation_id is not None
            else:
                assert result.pending_revision is not None
                assert result.attestation_id is None
                assert not paths.root.joinpath("procontract-attestation-request.json").exists()
                native = json.loads(paths.root.joinpath("procontract-native-result.json").read_text())
                assert native["contract"]["pendingRevision"] == result.pending_revision
                assert native["execution"]["contractID"] == result.contract_id
                progress = json.loads(paths.root.joinpath("procontract-progress.json").read_text())["current"]
                assistant = next(message for message in progress["messages"]["data"] if message["type"] == "assistant")
                assert assistant["finish"] == "tool-calls"
                assert all(part["state"]["status"] in {"completed", "error"} for part in assistant["content"])
                assert result.usage_incomplete is False
                assert result.input_tokens == breaker.campaign_usage.input == 10
                assert result.output_tokens == breaker.campaign_usage.output == 3
                assert result.cost == breaker.campaign_usage.cost > 0
                issued = json.loads(paths.root.joinpath("procontract-issue-response.json").read_text())["execution"]
                assert (
                    native["execution"]["dispatched"] is False
                    or native["execution"]["promptID"] != issued["promptID"]
                    or result.interrupt_attempted
                )
            assert tmp_path.joinpath("provider-hits.jsonl").is_file()
            assert workspace.manifest.binary_sha256 == fixture_metadata["sha256"]
            if workspace.lease._handle_active:
                workspace.stop_for_freeze()
    finally:
        if provider_container is not None:
            _run_docker(("docker", "rm", "-f", provider_container), 60)
        if image_built:
            containers = _run_docker(
                ("docker", "ps", "-aq", "--filter", f"label=programbench.campaign={campaign}"), 30
            ).split()
            if containers:
                _run_docker(("docker", "rm", "-f", *containers), 60)
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
                        tag,
                        "-R",
                        "a+rwX",
                        "/cleanup",
                    ),
                    60,
                )
            finally:
                _run_docker(
                    ("docker", "rmi", "-f", tag, *((preflight_image,) if preflight_tagged else ())),
                    60,
                )
