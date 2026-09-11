import http.client
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from scripts.campaign_provider_gateway import Gateway, process_identity, validate_payload


def test_payload_rejects_remote_tools_urls_and_model_changes():
    base={"model":"test-model","stream":True,"reasoning":{"effort":"xhigh"},"input":"hello"}
    assert validate_payload(base,"test-model","xhigh") == base
    for patch in ({"model":"other-model"},{"stream":False},{"background":True},
                  {"tools":[{"type":"web_search"}]},{"tools":[{"type":"mcp","server_url":"https://github.com"}]},
                  {"input":[{"type":"input_image","image_url":"https://github.com/a.png"}]},
                  {"input":[{"type":"input_file","file_url":"https://github.com/a.txt"}]},
                  {"reasoning":"bad"}):
        with pytest.raises(ValueError):validate_payload(base|patch,"test-model","xhigh")
    assert validate_payload(base|{"input":[{"type":"input_image","image_url":"data:image/png;base64,AA=="}]},"test-model","xhigh")


def test_gateway_streams_only_for_admitted_process_and_keeps_secret_on_host(tmp_path):
    received=[]
    class Upstream(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=self.rfile.read(int(self.headers['Content-Length']))
            received.append((self.path,self.headers.get('Authorization'),json.loads(body)))
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            self.wfile.write(b'event: response.completed\ndata: {"type":"response.completed","response":{"usage":{"input_tokens":2,"output_tokens":1}}}\n\n')
    upstream=http.server.ThreadingHTTPServer(('127.0.0.1',0),Upstream)
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    server=Gateway(tmp_path/'channel',tmp_path/'control','test-model','xhigh',
                   upstream=f'http://127.0.0.1:{upstream.server_port}/v1',api_key='trusted-test-secret')
    (tmp_path/'control/runtime.json').write_text(json.dumps(process_identity(os.getpid())))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    payload={"model":"test-model","stream":True,"reasoning":{"effort":"xhigh"},"input":"hello"}
    address=f'/proc/{os.getpid()}/fd/{server.directory_fd}/provider.sock'
    def request(method,path,body):
        connection=http.client.HTTPConnection('programbench-provider.invalid')
        connection.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        connection.sock.connect(address)
        connection.request(method,path,body=json.dumps(body),headers={'Content-Type':'application/json','Authorization':'Bearer dummy'})
        response=connection.getresponse(); data=response.read();connection.close()
        return response.status,data
    try:
        status,data=request('POST','/v1/responses',payload)
        assert status==200 and b'response.completed' in data and b'trusted-test-secret' not in data
        assert received[0][0]=='/v1/responses' and received[0][1]=='Bearer trusted-test-secret'
        assert request('GET','/v1/responses',payload)[0]==403
        assert request('POST','https://github.com/',payload)[0]==403
        assert request('POST','/v1/responses',payload|{'tools':[{'type':'web_search'}]})[0]==403
        child="""import http.client,socket,json,sys
c=http.client.HTTPConnection('programbench-provider.invalid');c.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);c.sock.connect(sys.argv[1]);c.request('POST','/v1/responses',body=sys.argv[2]);r=c.getresponse();print(r.status);r.read()
"""
        denied=subprocess.run([sys.executable,'-c',child,address,json.dumps(payload)],capture_output=True,text=True,check=True)
        assert denied.stdout.strip()=='403'
        assert len(received)==1
        assert server.db.execute("SELECT count(*) FROM request WHERE outcome='response.completed'").fetchone()[0]==1
    finally:
        server.shutdown();server.server_close();os.close(server.directory_fd);server.db.close()
        upstream.shutdown();upstream.server_close()
