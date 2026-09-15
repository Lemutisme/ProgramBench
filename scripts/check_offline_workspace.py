"""No-model admission check against the real offline workspace implementation."""

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

from scripts.campaign_runner import _campaign_name, plan_campaign
from scripts.campaign_state import JobPaths
from scripts.campaign_workspace import WorkspaceLease, WorkspaceRequest, ensure_workspace
from scripts.run_opencode import load_config
from programbench.utils.load_data import load_all_instances


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
out['interfaces']=os.listdir('/sys/class/net')
out['reference_help_exit']=subprocess.run(['/candidate/reference','-h'],capture_output=True,timeout=3).returncode
print(json.dumps(out))
'''


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--model-probe',action='store_true')
    parser.add_argument('--permission-probe',action='store_true')
    parser.add_argument('--contract-probe',action='store_true')
    parser.add_argument('--tool-probe',action='store_true')
    parser.add_argument('--baseline',action='store_true')
    parser.add_argument('--workflow-probe',action='store_true')
    parser.add_argument('--config',type=Path,default=Path(__file__).with_name('opencode_programbench_pipeline_luna.yaml'))
    parser.add_argument('--restart-checks',type=int,default=0)
    args=parser.parse_args()
    if args.contract_probe and (args.baseline or args.model_probe or args.tool_probe
            or os.environ.get('OPENAI_API_KEY') != 'no-model-admission-check'
            or os.environ.get('OPENAI_BASE_URL') != 'http://127.0.0.1:9/v1'):
        parser.error('--contract-probe requires dummy credentials and unreachable loopback upstream, without model probes')
    os.umask(0o077)
    os.environ['OPENCODE_SERVER_PASSWORD']=secrets.token_hex(32)
    config=load_config(args.config)
    arm='vanilla' if args.baseline else 'procontract'
    plan=plan_campaign(config,load_all_instances(),output=args.output,limit=1,
                       instance_filter=r'^abishekvashok__cmatrix\.5c082c6$',arm_selection=arm,dry_run=True)
    # Use the exact frozen install record from the real planning path.
    from scripts.campaign_install import InstallRecord
    install=InstallRecord.from_dict(plan.installs[arm]) if isinstance(plan.installs[arm],dict) else plan.installs[arm]
    job=JobPaths(args.output/arm/plan.selected_ids[0])
    request=WorkspaceRequest(job=job,campaign_root=args.output.parent,campaign=_campaign_name(args.output),
             instance_id=plan.selected_ids[0],arm=arm,image=plan.jobs[0].image,install=install,
             candidate_root=job.root/'candidates',state_root=job.root/'workspace-state',log_dir=job.root/'logs',
             credential_env=tuple(k for k in ('OPENAI_API_KEY','OPENAI_BASE_URL','OPENCODE_SERVER_PASSWORD') if os.environ.get(k)),
             isolated=True,health_timeout=45,provider_model=config.model.split('/',1)[1],provider_effort=config.variant,
             deadline_only=config.limits.deadline_only)
    with WorkspaceLease(request) as lease:
        workspace=ensure_workspace(request,lease)
        try:
            result=subprocess.run(['docker','exec',workspace.container_id,'python3','-I','-S','-c',PROBE],capture_output=True,text=True,timeout=40)
            if result.returncode:raise RuntimeError('offline probes failed: '+result.stderr[:1000])
            value=json.loads(result.stdout)
            assert 'UNEXPECTED' not in result.stdout
            assert value['child_gateway_status']==403 and value['credential_placeholder_only']
            assert value['interfaces']==['lo']
            assert all(value[key].get('failed') or value[key].get('unavailable') for key in ('https','git','cargo'))
            value['container_id']=workspace.container_id
            value['network_mode']=workspace.manifest.network_mode
            value['binary_sha256']=workspace.manifest.binary_sha256
            (args.output/'NETWORK_CHECK.json').write_text(json.dumps(value,indent=2)+'\n')
            print(json.dumps(value),flush=True)
            for restart in range(args.restart_checks):
                subprocess.run(['docker','exec',workspace.container_id,'python3','-I','-S','-c',
                                "from pathlib import Path; Path('/candidate/reference').unlink(missing_ok=True)"],check=True)
                workspace.stop()
                workspace=workspace.restart()
                print(json.dumps({'restart':restart+1,'status':'healthy','reference_restored':True}),flush=True)
            if args.contract_probe:
                from scripts.campaign_offline import DockerHttpClient
                from scripts.campaign_procontract import (
                    HttpProContractApi, _validate_issue_response, build_issue_payload, protocol_for_revision,
                )
                selected=next(item for item in config.arms if item.name=='procontract')
                protocol=protocol_for_revision(install.revision, selected.execution_policy,
                                               runtime_protocol=selected.runtime_protocol,
                                               policy_artifact=selected.policy_artifact)
                payload=build_issue_payload(workspace,now=time.time(),model=config.model,
                                            variant=config.variant,protocol=protocol)
                api=HttpProContractApi(workspace.base_url,expected_port=workspace.port,
                                       http_client=DockerHttpClient(workspace.container_id))
                response=_validate_issue_response(api.issue(payload),payload)
                admitted={'protocol':protocol if isinstance(protocol,str) else 'policy-split-v1',
                          **({'policy_artifact_sha256':selected.policy_artifact.sha256} if selected.policy_artifact else {}),
                          'contract_id':response['data']['id'],
                          'spec_hash':response['data']['specHash'],'authority':response['data']['spec']['authority'],
                          'execution_policy_matches':response['execution'].get('executionPolicy')==payload.get('executionPolicy'),
                          'real_provider_calls_enabled':False}
                assert admitted['execution_policy_matches']
                (args.output/'CONTRACT_CHECK.json').write_text(json.dumps(admitted,indent=2)+'\n')
                print(json.dumps({'contract_probe':'passed',**admitted}),flush=True)
            if args.workflow_probe:
                from scripts.campaign_workflow import check_and_continue
                setup="from pathlib import Path; p=Path('/candidate'); (p/'compile.sh').write_text('#!/bin/sh\\nexit 7\\n'); (p/'compile.sh').chmod(0o755); (p/'validate.sh').write_text('#!/bin/sh\\nexit 0\\n'); (p/'validate.sh').chmod(0o755)"
                subprocess.run(['docker','exec',workspace.container_id,'python3','-c',setup],check=True)
                feedback=[]
                class Recorder:
                    def feedback(self,sid,mid,text):
                        feedback.append({'session_id':sid,'message_id':mid,'text':text})
                workspace,continued=check_and_continue(workspace,Recorder(),'ses_workflow_probe',1,time.time()+90)
                assert continued and len(feedback)==1 and feedback[0]['session_id']=='ses_workflow_probe'
                assert 'failed' in feedback[0]['text'].lower()
                (args.output/'WORKFLOW_CHECK.json').write_text(json.dumps({'continued':continued,'feedback':feedback},indent=2)+'\n')
                print(json.dumps({'workflow_probe':'passed','same_session':True,'native_contract':False}),flush=True)
            if args.model_probe or args.permission_probe or args.tool_probe:
                from scripts.campaign_offline import DockerHttpClient
                from scripts.campaign_vanilla_http import HttpOpenCodeApi
                api=HttpOpenCodeApi(workspace.base_url,expected_port=workspace.port,http_client=DockerHttpClient(workspace.container_id))
                sid='ses_'+secrets.token_hex(16)
                mid='msg_'+secrets.token_hex(16)
                assert api.get_session(sid) is None
                api.create_session(sid,model=config.model,variant=config.variant,directory='/candidate',agent=None)
                if args.permission_probe:
                    decisions=[]
                    for action,resources,expected in [('external_directory',['/tmp/*'],'allow'),('edit',['/tmp/programbench-headless-probe.txt'],'allow'),('question',['*'],'deny')]:
                        response=api._request('POST',f'/api/session/{sid}/permission',
                                              {'action':action,'resources':resources,'save':resources})
                        effect=json.loads(response.body)['data']['effect']
                        decisions.append({'action':action,'resources':resources,'effect':effect})
                        assert effect==expected, f'V2 permission mismatch: {action}={effect}'
                    (args.output/'PERMISSION_CHECK.json').write_text(json.dumps(decisions,indent=2)+'\n')
                    print(json.dumps({'permission_probe':'passed','decisions':decisions}),flush=True)
                if not (args.model_probe or args.tool_probe):
                    return
                prompt='Reply with exactly OFFLINE_PROVIDER_OK. Do not use tools. This is only a transport check, not a programming task.'
                if args.tool_probe:
                    prompt='This is a tool transport check, not a programming task. Use the write tool (not bash) to create /tmp/programbench-headless-probe.txt containing exactly HEADLESS_WRITE_OK. Then use the read tool to read that absolute path. Finally reply with exactly OFFLINE_PROVIDER_OK.'
                api._request('POST',f'/api/session/{sid}/prompt',{'id':mid,'prompt':{'text':prompt},'delivery':'steer','resume':True})
                deadline=time.monotonic()+180
                while time.monotonic()<deadline:
                    messages=api.messages(sid)
                    assistants=[m for m in messages['data'] if m.get('type')=='assistant']
                    if assistants and assistants[-1].get('finish') is not None and api.status(sid)=='idle':
                        (args.output/'MODEL_CHECK.json').write_text(json.dumps(messages,indent=2)+'\n')
                        answer=''.join(p.get('text','') for m in assistants for p in m.get('content',[]) if p.get('type')=='text')
                        assert 'OFFLINE_PROVIDER_OK' in answer, 'model transport did not return the probe token'
                        if args.tool_probe:
                            tools=[p for m in assistants for p in m.get('content',[]) if p.get('type')=='tool']
                            assert any(p.get('name')=='write' and p['state']['status']=='completed' for p in tools)
                            assert any(p.get('name')=='read' and p['state']['status']=='completed' for p in tools)
                            actual=subprocess.run(['docker','exec',workspace.container_id,'python3','-I','-S','-c',"from pathlib import Path; assert Path('/tmp/programbench-headless-probe.txt').read_text().strip()=='HEADLESS_WRITE_OK'"],capture_output=True,timeout=10)
                            assert actual.returncode==0, 'tool probe file content mismatch'
                        print(json.dumps({'model_probe':'passed','model':config.model,'variant':config.variant,'assistant_messages':len(assistants)}),flush=True)
                        break
                    time.sleep(1)
                else:
                    raise TimeoutError('real model transport probe timed out')
        finally:
            workspace.stop()


if __name__=='__main__':main()
