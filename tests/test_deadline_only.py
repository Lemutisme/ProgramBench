import http.client
import http.server
import json
import os
import socket
import threading
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from scripts.campaign_procontract import (
    ProContractCompletion,
    _safety_completion,
    _expected_spec,
    _load_or_create_issue,
    _result,
    _valid_spec_schema,
)
from scripts.campaign_provider_gateway import Gateway, process_identity
from scripts.campaign_usage import CircuitBreaker, UsageParseError, summarize_usage
from scripts.campaign_vanilla import _observe_terminal_usage, _summarize_usage
from scripts.run_opencode import ConfigError, Limits


def issue_file(root, *, started=None):
    started = time.time() if started is None else started
    path = root / "procontract-issue.json"
    path.write_text(json.dumps({"startedAt": started, "payload": {"budget": {"deadline": int(started * 1000) + 21600000}}}))
    return path


def gateway(root, *, issue=None):
    return Gateway(root / "channel", root / "control", "test-model", "max",
                   upstream="http://127.0.0.1:1/v1", api_key="fixture-only", issue=issue)


def close_gateway(server):
    server.server_close()
    os.close(server.directory_fd)
    server.db.close()
    (server.channel / "provider.sock").unlink()


def test_deadline_only_limits_do_not_use_finite_sentinels():
    limits = Limits(21600, None, None, None, None)
    assert limits.deadline_only
    assert not Limits(21600, 1000, 4000, None, None).deadline_only
    for invalid in (0, -1, True, float("inf")):
        with pytest.raises(ConfigError):
            Limits(21600, invalid, None, None, None)


def test_frozen_issue_recovery_preserves_absolute_deadline_and_omitted_counts(tmp_path):
    root = tmp_path / "state"
    control = root / "provider-control"
    control.mkdir(parents=True)
    (control / "profile.json").write_text(json.dumps({"budget": "6h-only"}))
    workspace = SimpleNamespace(manifest=SimpleNamespace(
        campaign="deadline", job_id="fixture", state_root=root, network_mode="none", credential_env=(),
    ))
    path = tmp_path / "procontract-issue.json"
    first = _load_or_create_issue(path, workspace, "openai/gpt-5.6-luna", "max", lambda: 100)
    restored = _load_or_create_issue(path, workspace, "openai/gpt-5.6-luna", "max", lambda: 9000)
    assert restored == first
    assert restored["payload"]["budget"] == {"deadline": 21700000}
    assert _valid_spec_schema(_expected_spec(restored["payload"]))
    breaker = CircuitBreaker(provider_turn_limit=None)
    limits = Limits(21600, None, None, None, None)
    assert _safety_completion(breaker, limits, 3001, 1000001, lambda: 21699, 21700) is None
    assert _safety_completion(breaker, limits, 3001, 1000001, lambda: 21700, 21700) == ProContractCompletion.WALL_TIME
    assert _safety_completion(breaker, Limits(21600, 1000, 4000, None, None), 1000, 0, lambda: 100, 21700) == ProContractCompletion.PROVIDER_TURNS


def test_gateway_accepts_more_than_old_wire_cap_and_rejects_deadline_regrant(tmp_path):
    issue = issue_file(tmp_path)
    server = gateway(tmp_path, issue=issue)
    body = json.dumps({"model": "test-model", "reasoning": {"effort": "max"}}).encode()
    try:
        for _ in range(3001):
            request = server.record(os.getpid(), body)
            server.finish(request, 200, "response.completed", {"output_tokens": 1})
        assert server.db.execute("SELECT count(*) FROM request").fetchone()[0] == 3001
        original = server.deadline()
    finally:
        close_gateway(server)
    restored = gateway(tmp_path, issue=issue)
    try:
        assert restored.deadline() == original
        assert restored.record(os.getpid(), body) == 3002
        issue_file(tmp_path, started=time.time() + 100)
        with pytest.raises(ValueError, match="contract_budget_conflict"):
            restored.record(os.getpid(), body)
    finally:
        close_gateway(restored)


def test_gateway_historical_caps_and_expired_contract_remain_enforced(tmp_path):
    body = json.dumps({"model": "test-model", "reasoning": {"effort": "max"}}).encode()
    server = gateway(tmp_path)
    try:
        for _ in range(1000):
            request = server.record(os.getpid(), body)
            server.finish(request, 200, "response.completed", {})
        with pytest.raises(ValueError, match="provider_turn_budget_exhausted"):
            server.record(os.getpid(), body)
    finally:
        close_gateway(server)
    issue = issue_file(tmp_path, started=time.time() - 21601)
    server = gateway(tmp_path, issue=issue)
    try:
        with pytest.raises(ValueError, match="wall_time_exhausted"):
            server.record(os.getpid(), body)
    finally:
        close_gateway(server)


def test_large_native_history_retains_unknown_usage_through_breaker_reload():
    messages = {"data": [{"id": f"msg_{index}", "type": "assistant", "content": [],
                          "tokens": {"input": 1, "output": 2, "reasoning": 3, "cache": {"read": 10, "write": 0}},
                          "cost": None if index == 3000 else 0.01} for index in range(3001)]}
    summary = _summarize_usage(messages)
    assert summary.provider_turns == 3001 and summary.cost == Decimal("30.00")
    assert summary.usage_incomplete and summary.turns[-1].usage_incomplete
    breaker = CircuitBreaker(provider_turn_limit=None)
    breaker.observe("job", 1, summary, terminal=False)
    restored = CircuitBreaker.from_snapshot(json.dumps(breaker.snapshot()))
    assert restored.provider_turn_limit is None and restored.campaign_usage == summary
    assert restored.campaign_usage.usage_incomplete
    _observe_terminal_usage(restored, "job", 2, summary)
    assert restored.snapshot()["jobs"]["job"]["turn_limit_violation"] is False
    assert not restored.stop_active
    with pytest.raises(UsageParseError):
        CircuitBreaker().observe("job", 1, summary, terminal=False)
    flat = {"data": [{"type": "assistant", "content": "", "tokens": item["tokens"], "cost": item["cost"]} for item in messages["data"]]}
    assert summarize_usage(flat, allow_large=True) == summary
    result = _result(
        {"id": "pct_fixture"}, {"revision": 1, "specHash": "hash", "status": "discharged"},
        {"sessionID": "ses_fixture", "promptID": "msg_fixture", "turnsUsed": 3001},
        {"startedAt": 0, "acceptanceHash": "hash"}, summary, 4001, lambda: 10,
        ProContractCompletion.DISCHARGED, quiet={"quiet": True, "outstanding": []},
        subject_hash=None, replay_evidence_hash=None, report_hash=None, package_hash=None,
        executable_hash=None, attestation_id=None, workspace_frozen=True, stopped=True,
    )
    assert result.to_dict()["usage_incomplete"] is True
    assert result.cost == Decimal("30.00")


def test_gateway_interrupts_inflight_request_at_original_contract_deadline(tmp_path):
    released = threading.Event()
    class Upstream(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b": heartbeat\n\n")
            self.wfile.flush()
            released.wait(5)

    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    issue = issue_file(tmp_path, started=time.time() - 21599)
    server = Gateway(tmp_path / "channel", tmp_path / "control", "test-model", "max",
                     upstream=f"http://127.0.0.1:{upstream.server_port}/v1", api_key="fixture-only", issue=issue)
    (server.control / "runtime.json").write_text(json.dumps(process_identity(os.getpid())))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = http.client.HTTPConnection("programbench-provider.invalid")
    client.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.sock.settimeout(4)
    client.sock.connect(f"/proc/{os.getpid()}/fd/{server.directory_fd}/provider.sock")
    started = time.monotonic()
    try:
        client.request("POST", "/v1/responses", body=json.dumps({"model": "test-model", "stream": True,
                       "reasoning": {"effort": "max"}, "input": "fixture"}))
        response = client.getresponse()
        assert response.status == 200
        assert response.read() == b": heartbeat\n\n"
        assert time.monotonic() - started < 3
        for _ in range(100):
            with server.lock:
                row = server.db.execute("SELECT finished,usage,close_reason FROM request WHERE peer_pid IS NOT NULL").fetchone()
            if row[0] is not None:
                break
            time.sleep(0.01)
        assert row[0] is not None and row[1] is None and row[2] == "contract_deadline"
    finally:
        released.set()
        client.close()
        server.shutdown()
        close_gateway(server)
        upstream.shutdown()
        upstream.server_close()
