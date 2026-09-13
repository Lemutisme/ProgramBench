import http.client
import http.server
import json
import os
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.campaign_freeze import CredentialLeakError, _verify_no_credentials
from scripts.campaign_procontract import _scan_credential_roots
from scripts.campaign_provider_gateway import Gateway, process_identity
from scripts.campaign_usage import CircuitBreaker


@pytest.mark.parametrize('terminal', ['response.completed', 'response.incomplete', 'response.failed'])
def test_terminal_usage_is_durable_before_eof_and_survives_shutdown(tmp_path, terminal):
    release = threading.Event()
    class Upstream(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(('data: ' + json.dumps({'type': terminal, 'response': {'usage': {'output_tokens': 128000}}}) + '\n\n').encode())
            self.wfile.flush()
            release.wait(5)
    upstream = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    server = Gateway(tmp_path/'channel', tmp_path/'control', 'test-model', 'max',
                     upstream=f'http://127.0.0.1:{upstream.server_port}/v1', api_key='test-only-secret')
    (tmp_path/'control/runtime.json').write_text(json.dumps(process_identity(os.getpid())))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    connection = http.client.HTTPConnection('programbench-provider.invalid')
    connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.sock.settimeout(5)
    connection.sock.connect(f'/proc/{os.getpid()}/fd/{server.directory_fd}/provider.sock')
    try:
        connection.request('POST', '/v1/responses', body=json.dumps({'model':'test-model','stream':True,'reasoning':{'effort':'max'},'input':'local test'}))
        response = connection.getresponse()
        assert terminal.encode() in response.readline()
        with server.lock:
            row = server.db.execute('SELECT finished,terminal_at,usage,outcome,sent,headers_at,first_byte FROM request WHERE peer_pid IS NOT NULL').fetchone()
        assert row[0] is None and row[1] is not None
        assert json.loads(row[2]) == {'output_tokens':128000}
        assert row[3] == terminal and all(row[4:])
        pending = server.record(os.getpid(), json.dumps({'model':'test-model','reasoning':{'effort':'max'}}).encode())
        server.checkpoint(pending, sent=time.time())
        server.checkpoint_shutdown()
        with server.lock:
            rows = server.db.execute('SELECT outcome,usage,close_reason FROM request ORDER BY id').fetchall()
        assert rows[0] == (terminal, row[2], 'gateway_shutdown')
        assert rows[1] == ('gateway_stopped_unknown', None, 'gateway_shutdown')
    finally:
        release.set()
        response.read()
        connection.close()
        server.shutdown(); server.server_close(); os.close(server.directory_fd)
        upstream.shutdown(); upstream.server_close()


def test_only_publication_surfaces_are_scanned(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCAL_SCAN_SECRET', 'local-test-secret')
    roots = {name: tmp_path/name for name in ['candidate','subject','export','git_dir']}
    for root in roots.values(): root.mkdir()
    private = roots['candidate']/'private'
    private.mkdir(mode=0o700)
    (private/'probe').write_text('local-test-secret')
    (private/'probe').chmod(0o600)
    paths = SimpleNamespace(**roots)
    _scan_credential_roots(paths, ('LOCAL_SCAN_SECRET',))
    assert (private/'probe').read_text() == 'local-test-secret'
    for name in ['subject','export','git_dir']:
        leak = roots[name]/'published'
        leak.write_text('local-test-secret')
        with pytest.raises(CredentialLeakError): _scan_credential_roots(paths, ('LOCAL_SCAN_SECRET',))
        leak.unlink()


def test_systemic_fault_pauses_and_remains_paused_after_backoff_and_reload():
    breaker = CircuitBreaker(clock=lambda: 1000)
    assert not breaker.observe_system_failure('one', 'tool_stream', now=1000)
    assert not breaker.observe_system_failure('one', 'tool_stream', now=1001)
    assert not breaker.observe_system_failure('two', 'tool_stream', now=1002)
    assert breaker.observe_system_failure('three', 'tool_stream', now=1003)
    assert breaker.stop_active and not breaker.admission_allowed
    restored = CircuitBreaker.from_snapshot(breaker.snapshot(), clock=lambda: 2000)
    assert restored.pause_reason == 'repeated execution protocol failure'
    assert not restored.admission_allowed
    restored.acknowledge('system')
    assert restored.admission_allowed


def test_gateway_connection_failure_is_not_reported_as_upstream_http_rejection(tmp_path):
    class Upstream(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
    upstream = http.server.ThreadingHTTPServer(('127.0.0.1',0),Upstream)
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    server = Gateway(tmp_path/'channel',tmp_path/'control','test-model','max',
                     upstream=f'http://127.0.0.1:{upstream.server_port}/v1',api_key='test-only-secret')
    (tmp_path/'control/runtime.json').write_text(json.dumps(process_identity(os.getpid())))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    connection = http.client.HTTPConnection('programbench-provider.invalid')
    connection.sock = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    connection.sock.settimeout(5)
    connection.sock.connect(f'/proc/{os.getpid()}/fd/{server.directory_fd}/provider.sock')
    try:
        connection.request('POST','/v1/responses',body=json.dumps({'model':'test-model','stream':True,'reasoning':{'effort':'max'},'input':'local test'}))
        response = connection.getresponse()
        assert response.status == 502
        assert response.getheader('X-ProgramBench-Transport-Error') == 'upstream_connection_failed'
        assert b'provider transport failed' in response.read()
    finally:
        connection.close()
        server.shutdown(); server.server_close(); os.close(server.directory_fd)
        upstream.shutdown(); upstream.server_close()
    row=server.db.execute('SELECT status,outcome,usage,error_name FROM request WHERE peer_pid IS NOT NULL').fetchone()
    assert row == (None, 'transport_failed', None, 'RemoteDisconnected')


def test_frozen_tooling_is_readable_across_uid_with_private_controller_umask(tmp_path, monkeypatch):
    from scripts.campaign_offline import prepare_tooling
    source=tmp_path/'portable-rg';source.write_bytes(b'local executable fixture')
    cache=tmp_path/'cache';cache.mkdir()
    monkeypatch.setenv('PROGRAMBENCH_RIPGREP',str(source))
    previous=os.umask(0o077)
    try: prepare_tooling(SimpleNamespace(cache_dir=cache))
    finally: os.umask(previous)
    target=cache/'opencode/bin/rg'
    assert target.read_bytes()==source.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o555
    assert target.parent.stat().st_mode & 0o777 == 0o755
    assert target.parent.parent.stat().st_mode & 0o777 == 0o755
    before=target.stat().st_ino
    prepare_tooling(SimpleNamespace(cache_dir=cache))
    assert target.stat().st_ino==before
    source.write_bytes(b'changed fixture')
    with pytest.raises(ValueError,match='frozen ripgrep changed'):
        prepare_tooling(SimpleNamespace(cache_dir=cache))
