from __future__ import annotations

import hashlib
import json
import textwrap
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
