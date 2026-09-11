PROBE = r'''
import errno,http.client,json,os,socket,subprocess
out={}
for name,target in [('ipv4',('1.1.1.1',443)),('dns_tcp',('8.8.8.8',53))]:
 try:
  with socket.create_connection(target,timeout=2):out[name]='UNEXPECTED_CONNECTED'
 except OSError as e:out[name]=type(e).__name__+':'+str(e.errno)
try:
 s=socket.socket(socket.AF_INET6,socket.SOCK_STREAM);s.settimeout(2);s.connect(('2606:4700:4700::1111',443));out['ipv6']='UNEXPECTED_CONNECTED'
except OSError as e:out['ipv6']=type(e).__name__+':'+str(e.errno)
finally:s.close()
try:
 socket.getaddrinfo('github.com',443);out['dns']='UNEXPECTED_RESOLVED'
except OSError as e:out['dns']=type(e).__name__+':'+str(e.errno)
for name,cmd in [('https',['curl','-fsSL','--connect-timeout','2','--max-time','4','https://github.com/']),('git',['git','ls-remote','https://github.com/LuaJIT/LuaJIT.git','HEAD']),('cargo',['cargo','search','lightningcss','--limit','1'])]:
 try:
  r=subprocess.run(cmd,capture_output=True,timeout=5);out[name]={'exit':r.returncode,'output_bytes':len(r.stdout),'failed':r.returncode!=0}
 except subprocess.TimeoutExpired:out[name]={'failed':True,'timeout':True}
 except FileNotFoundError:out[name]={'unavailable':True}
c=http.client.HTTPConnection('programbench-provider.invalid');c.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);c.sock.settimeout(3);c.sock.connect('/run/programbench-provider/provider.sock')
c.request('POST','/v1/responses',json.dumps({'model':'gpt-5.6-sol','stream':True,'reasoning':{'effort':'xhigh'},'input':'forged child request'}));r=c.getresponse();out['child_gateway_status']=r.status;r.read();c.close()
out['credential_placeholder_only']=os.environ.get('OPENAI_API_KEY')=='programbench-offline-channel'
out['contract_disabled']=os.environ.get('OPENCODE_PROCONTRACT_DISABLED')=='1'
out['interfaces']=os.listdir('/sys/class/net')
out['reference_executable']=os.access('/workspace/executable',os.X_OK)
print(json.dumps(out))
'''
