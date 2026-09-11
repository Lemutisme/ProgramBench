"""Trusted model-only Unix gateway for network-none candidate containers.

The actual credential exists only here. Linux SO_PEERCRED pins callers to the
admitted OpenCode process, not its shell children. No general proxy, redirects,
remote file/image inputs, provider-hosted tools, or alternate models are allowed.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import http.server
import json
import os
import signal
import socket
import socketserver
import sqlite3
import stat
import struct
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


MAX_REQUEST_BYTES = 32 * 1024 * 1024
MODEL_ORIGIN = "http://programbench-provider.invalid"
MODEL_PATH = "/v1/responses"


def process_identity(pid: int) -> dict[str, object]:
    raw = Path(f"/proc/{pid}/stat").read_text()
    return {"pid": pid, "start_ticks": raw.rpartition(") ")[2].split()[19]}


def validate_payload(value: object, model: str, effort: str) -> dict:
    if not isinstance(value, dict) or value.get("model") != model:
        raise ValueError("model_not_admitted")
    if value.get("stream") is not True or value.get("background") not in (None, False):
        raise ValueError("only_foreground_streaming_allowed")
    if value.get("conversation") is not None or value.get("previous_response_id") is not None:
        raise ValueError("external_conversation_state_not_allowed")
    if not isinstance(value.get("reasoning"), dict) or value["reasoning"].get("effort") != effort:
        raise ValueError("reasoning_not_admitted")
    for tool in value.get("tools", []):
        if not isinstance(tool, dict) or tool.get("type") not in ("function", "custom"):
            raise ValueError("provider_hosted_tool_not_allowed")
    pending = [value.get("input", [])]
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(item)
        if isinstance(item, dict):
            if item.get("type") in ("input_file", "computer_screenshot"):
                raise ValueError("remote_or_multimodal_input_not_allowed")
            if "file_url" in item or "file_id" in item:
                raise ValueError("remote_input_not_allowed")
            if "image_url" in item and (not isinstance(item["image_url"], str) or not item["image_url"].startswith("data:image/")):
                raise ValueError("remote_image_not_allowed")
            pending.extend(item.values())
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


class Gateway(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, channel: Path, control: Path, model: str, effort: str, *, upstream: str, api_key: str):
        self.channel = channel
        self.control = control
        self.model = model
        self.effort = effort
        self.api_key = api_key
        self.upstream = urlsplit(upstream.rstrip("/"))
        if self.upstream.scheme not in ("https", "http") or self.upstream.query or self.upstream.fragment or self.upstream.username:
            raise ValueError("unsupported_upstream_origin")
        if self.upstream.scheme == "http" and self.upstream.hostname not in ("127.0.0.1", "localhost"):
            raise ValueError("plaintext_upstream_not_allowed")
        self.lock = threading.Lock()
        self.control.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.channel.mkdir(parents=True, exist_ok=True, mode=0o755)
        if self.channel.is_symlink() or self.control.is_symlink():
            raise ValueError("symlink_gateway_root")
        self.channel.chmod(0o755)
        self.db = sqlite3.connect(control / "requests.db", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS request (id INTEGER PRIMARY KEY, started REAL, finished REAL, peer_pid INTEGER, body_sha256 TEXT, status INTEGER, outcome TEXT, usage TEXT)")
        if 'metadata' not in {row[1] for row in self.db.execute('PRAGMA table_info(request)')}:
            self.db.execute('ALTER TABLE request ADD COLUMN metadata TEXT')
        self.db.commit()
        socket_path = channel / "provider.sock"
        if socket_path.exists() or socket_path.is_symlink():
            raise ValueError("gateway_socket_already_exists")
        # Linux pathname Unix sockets can bind via a directory fd even when the
        # durable experiment path itself exceeds sockaddr_un's 108-byte limit.
        self.directory_fd = os.open(channel, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        super().__init__(f"/proc/self/fd/{self.directory_fd}/provider.sock", Handler)
        socket_path.chmod(0o666)

    def allowed_peer(self, connection: socket.socket) -> int | None:
        pid, _uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        try:
            recorded = json.loads((self.control / "runtime.json").read_text())
            if recorded != process_identity(pid):
                return None
        except (OSError, ValueError, IndexError):
            return None
        return pid

    def record(self, pid: int, body: bytes) -> int:
        with self.lock:
            value = json.loads(body)
            metadata = {'model': value['model'], 'reasoning': value['reasoning'],
                        'tool_names': [tool.get('name') for tool in value.get('tools', [])]}
            prior = self.db.execute("SELECT min(started),count(*) FROM request WHERE peer_pid IS NOT NULL").fetchone()
            if prior[0] and time.time() - prior[0] > 21600:
                raise ValueError("wall_time_exhausted")
            # Existing Session code owns the 1000 provider-turn limit. This is a
            # separate ceiling on wire attempts, including its bounded retries.
            if prior[1] >= 3000:
                raise ValueError("transport_attempt_ceiling")
            reserved = self.db.execute(
                "SELECT count(*) FROM request WHERE peer_pid IS NOT NULL AND (status=200 OR finished IS NULL)"
            ).fetchone()[0]
            if reserved >= 1000:
                raise ValueError("provider_turn_budget_exhausted")
            cursor = self.db.execute("INSERT INTO request(started,peer_pid,body_sha256,outcome,metadata) VALUES (?,?,?,?,?)",
                                     (time.time(), pid, hashlib.sha256(body).hexdigest(), "admitted", json.dumps(metadata)))
            self.db.commit()
            return cursor.lastrowid

    def finish(self, request_id: int, status: int, outcome: str, usage: object):
        with self.lock:
            self.db.execute("UPDATE request SET finished=?,status=?,outcome=?,usage=? WHERE id=?",
                            (time.time(), status, outcome, json.dumps(usage), request_id))
            self.db.commit()

    def deny(self, reason: str, status: int = 403):
        with self.lock:
            self.db.execute("INSERT INTO request(started,status,outcome) VALUES (?,?,?)", (time.time(), status, reason))
            self.db.commit()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, *_args):
        pass  # Never write headers, prompts, paths, credentials, or provider bodies to stderr.

    def refuse(self, reason: str, status: int = 403):
        self.server.deny(reason, status)
        body = json.dumps({"error": {"message": reason, "type": "offline_gateway_denied"}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_POST(self):
        peer = self.server.allowed_peer(self.connection)
        if peer is None:
            return self.refuse("caller_not_admitted_runtime")
        if self.path != MODEL_PATH or self.headers.get("Host") != "programbench-provider.invalid":
            return self.refuse("endpoint_not_admitted")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit() or self.headers.get("Transfer-Encoding"):
            return self.refuse("invalid_request_framing")
        length = int(lengths[0])
        if not 0 < length <= MAX_REQUEST_BYTES:
            return self.refuse("request_size_limit")
        self.connection.settimeout(30)
        body = self.rfile.read(length)
        if len(body) != length:
            return self.refuse("incomplete_request")
        try:
            validate_payload(json.loads(body, object_pairs_hook=unique_object), self.server.model, self.server.effort)
            request_id = self.server.record(peer, body)
        except (ValueError, TypeError, AttributeError) as error:
            if str(error) in ('provider_turn_budget_exhausted', 'wall_time_exhausted', 'transport_attempt_ceiling'):
                return self.refuse(str(error), 409)
            return self.refuse("request_not_admitted")
        upstream = self.server.upstream
        cls = http.client.HTTPSConnection if upstream.scheme == "https" else http.client.HTTPConnection
        connection = cls(upstream.hostname, upstream.port, timeout=900)
        status = 502
        usage = None
        outcome = "transport_failed"
        began = False
        try:
            connection.request("POST", upstream.path + "/responses", body=body,
                               headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.server.api_key,
                                        "Accept-Encoding": "identity"})
            response = connection.getresponse()
            status = response.status
            # Do not follow redirects or relay arbitrary upstream error bodies.
            if status != 200:
                outcome = "upstream_http_" + str(status)
                message = json.dumps({"error": {"message": outcome}}).encode()
                self.send_response(status if status >= 400 else 502)
                self.send_header("Content-Length", str(len(message)))
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                self.end_headers()
                began = True
                self.wfile.write(message)
                return
            if not (response.getheader("Content-Type") or "").startswith("text/event-stream"):
                raise ValueError("unexpected_upstream_content_type")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            began = True
            self.connection.settimeout(900)
            pending = bytearray()
            while chunk := response.read1(64 * 1024):
                self.wfile.write(chunk)
                self.wfile.flush()
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, pending = pending.partition(b"\n")
                    if line.startswith(b"data: "):
                        try:
                            event = json.loads(line[6:])
                            if event.get("type") in ("response.completed", "response.incomplete", "response.failed"):
                                usage = event.get("response", {}).get("usage")
                                outcome = event["type"]
                        except (ValueError, AttributeError):
                            pass
                if len(pending) > MAX_REQUEST_BYTES:
                    raise ValueError("upstream_frame_limit")
        except (OSError, ValueError, http.client.HTTPException):
            if not began:
                self.send_error(502, "provider transport failed")
        finally:
            self.close_connection = True
            connection.close()
            self.server.finish(request_id, status, outcome, usage)

    def do_GET(self):
        self.refuse("method_not_admitted")

    do_CONNECT = do_GET
    do_PUT = do_GET
    do_DELETE = do_GET
    do_PATCH = do_GET
    do_OPTIONS = do_GET


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", type=Path, required=True)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    server = Gateway(args.channel, args.control, args.model, args.effort,
                     upstream=os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1",
                     api_key=os.environ["OPENAI_API_KEY"])
    (args.control / "gateway.json").write_text(json.dumps(process_identity(os.getpid())))
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    server.serve_forever(poll_interval=0.1)


if __name__ == "__main__":
    main()
