"""Connect an explicitly selected local LogForge instance to the hosted HTTPS MCP using outbound requests."""
import argparse
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser

import logforge_mcp as bridge
import logforge_setup as setup
import logforge_usage as usage

DEFAULT_URL='https://usage.logforge.dev'
READ_TOOLS=frozenset({'logforge_describe_stack','logforge_inventory_search','logforge_logs_query','logforge_metrics_query',
    'logforge_metrics_query_range','logforge_alerts_list','logforge_alerts_get','logforge_graph_get','logforge_alert_history_search',
    'logforge_file_list','logforge_file_read'})

def config_path():
    return Path(os.environ.get('LOGFORGE_CLOUD_CONFIG',str(bridge.config_path().parent/'cloud.json'))).expanduser().resolve()

def load():
    data=json.loads(config_path().read_text(encoding='utf-8'))
    if 'relay_token_dpapi' in data:data['relay_token']=bridge.protect_secret(data['relay_token_dpapi'],decrypt=True)
    return data

def save(data):
    data=dict(data)
    if os.name=='nt' and 'relay_token' in data:data['relay_token_dpapi']=bridge.protect_secret(data.pop('relay_token'))
    config_path().parent.mkdir(parents=True,exist_ok=True)
    if os.name!='nt':config_path().parent.chmod(0o700)
    setup.private_write(config_path(),data)

def base_url(value):
    bridge.validate_url(value)
    parsed=urllib.parse.urlsplit(value)
    if parsed.path not in ('','/') or parsed.query or parsed.fragment:raise bridge.BridgeError('Use the gateway HTTPS origin, without a path.')
    return value.rstrip('/')

def request(config,path,body,authenticated=False):
    headers={'Content-Type':'application/json'}
    if authenticated:headers['Authorization']='Bearer '+config['relay_token']
    opener=urllib.request.build_opener(bridge.NoRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    req=urllib.request.Request(base_url(config['url'])+path,data=json.dumps(body).encode(),headers=headers)
    try:
        with opener.open(req,timeout=35) as response:
            raw=response.read(2*1024*1024+1)
            if len(raw)>2*1024*1024:raise bridge.BridgeError('Gateway response exceeded the limit.')
            return json.loads(raw)
    except urllib.error.HTTPError as error:
        if error.code==401:raise bridge.BridgeError('Relay access expired or was revoked. Connect again through your LogForge account.') from None
        raise bridge.BridgeError('Gateway returned HTTP '+str(error.code)+'. No response body or credential was printed.') from None
    except (OSError,ValueError):raise bridge.BridgeError('Cannot reach the gateway using trusted TLS.') from None

def connect(url,label,open_browser=True,timeout=600):
    if config_path().exists():raise bridge.BridgeError('A relay is already configured. Preserve it, use a separate LOGFORGE_CLOUD_CONFIG, or revoke/disconnect it first.')
    config={'url':base_url(url),'label':label,'projects':{},'mcp_config':str(bridge.config_path().resolve())}
    pair=request(config,'/gateway/relay/start',{'label':label})
    print(json.dumps({'verification_url':pair['verification_url'],'message':'Sign in and approve this connection. Keep credentials out of chat.'}),flush=True)
    if open_browser:webbrowser.open(pair['verification_url'])
    deadline=time.monotonic()+min(timeout,600)
    while time.monotonic()<deadline:
        result=request(config,'/gateway/relay/pair-status',{'id':pair['id'],'secret':pair['secret']})
        if not result.get('pending'):
            config.update(connection_id=result['connection_id'],relay_token=result['token'],created_at=time.time())
            save(config);return {'connected':True,'connection_id':config['connection_id'],'config':str(config_path())}
        time.sleep(3)
    raise bridge.BridgeError('Connection approval timed out. Start connect again when ready.')

def share_project(path,label=None):
    root=Path(path).expanduser().resolve()
    if not root.is_dir():raise bridge.BridgeError('Select an existing project directory.')
    data=load()
    for identifier,project in data['projects'].items():
        if project['path']==str(root):return {'project_id':identifier,'label':project['label']}
    identifier=str(uuid.uuid4());data['projects'][identifier]={'path':str(root),'label':label or root.name}
    save(data);return {'project_id':identifier,'label':data['projects'][identifier]['label']}

def handle(config,gateway,job):
    name=job.get('name');args=job.get('arguments',{})
    if not isinstance(args,dict):raise bridge.BridgeError('Invalid relay arguments.')
    if name=='logforge_projects_list':
        value={'projects':[{'project_id':key,'label':p['label']} for key,p in config.get('projects',{}).items()]}
        return {'content':[{'type':'text','text':json.dumps(value)}],'structuredContent':value}
    if name in ('logforge_runs_list','logforge_run_read'):
        if set(args)-{'project_id','run_id','tag','limit'}:raise bridge.BridgeError('Unsupported report arguments.')
        project=config.get('projects',{}).get(args.get('project_id'))
        if not project:raise bridge.BridgeError('This project was not explicitly shared by this relay.')
        params={key:value for key,value in args.items() if key!='project_id'}
        params['project_path']=project['path']
        value=bridge.inspect_run(name,params)
        return {'content':[{'type':'text','text':json.dumps(value)}],'structuredContent':value}
    if name not in READ_TOOLS:raise bridge.BridgeError('This relay only exposes approved read-only LogForge tools.')
    response=gateway.call({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}})
    if 'error' in response:raise bridge.BridgeError('The connected instance rejected the requested read operation.')
    return response['result']

def state_path():return config_path().with_suffix('.state.json')
def stop_path():return config_path().with_suffix('.stop')

def running():
    try:
        value=json.loads(state_path().read_text())
        return value if value.get('running') and time.time()-value['heartbeat']<90 else None
    except (OSError,ValueError,KeyError):return None

def start():
    if running():return {'running':True,'already_running':True}
    load()
    stop_path().unlink(missing_ok=True)
    options={'stdin':subprocess.DEVNULL,'stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL,'close_fds':True}
    if os.name=='nt':options['creationflags']=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    else:options['start_new_session']=True
    child=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'serve'],**options)
    for _ in range(40):
        if running():return {'running':True,'survives_chat_exit':True,'starts_after_reboot':False}
        if child.poll() is not None:break
        time.sleep(.25)
    raise bridge.BridgeError('Relay could not start. Check the selected local connection with logforge_mcp.py doctor.')

def stop():
    stop_path().touch()
    for _ in range(180):
        if not running():return {'running':False,'configuration_preserved':True}
        time.sleep(.25)
    return {'running':True,'stop_requested':True,'message':'Waiting for the current network request to finish.'}

def serve():
    # OS-owned advisory lock prevents duplicate relays and releases automatically after a crash.
    lock=config_path().with_suffix('.lock').open('a+b')
    if not lock.tell():lock.write(b'0');lock.flush()
    lock.seek(0)
    if os.name=='nt':
        import msvcrt
        msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:serve_locked()
    finally:
        setup.private_write(state_path(),{'running':False,'heartbeat':time.time()})
        lock.close()

def serve_locked():
    data=load()
    # Pin the connection chosen during pairing. A cloud request can never override this path.
    os.environ['LOGFORGE_MCP_CONFIG']=data['mcp_config']
    os.environ.pop('LOGFORGE_MCP_URL',None);os.environ.pop('LOGFORGE_MCP_API_KEY',None)
    gateway=bridge.runtime_gateway()
    gateway.call({'jsonrpc':'2.0','id':0,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'logforge-outbound-relay','version':'0.5.0'}}})
    print(json.dumps({'status':'relay running','connection_id':data['connection_id']}),flush=True)
    usage.record({'bridge.started':1})
    delay=1
    while not stop_path().exists():
        setup.private_write(state_path(),{'running':True,'heartbeat':time.time()})
        try:
            data=load()  # Project sharing and credential changes take effect without a restart.
            job=request(data,'/gateway/relay/poll',{},True)
            if job.get('idle'):delay=1;continue
            started=time.monotonic()
            try:result=handle(data,gateway,job)
            except (bridge.BridgeError,OSError,ValueError,KeyError,TypeError):result={'isError':True,'content':[{'type':'text','text':'The local instance could not complete this read. Check its connection and explicitly shared project configuration.'}]}
            if len(json.dumps(result).encode())>1900000:result={'isError':True,'content':[{'type':'text','text':'Result exceeds the relay limit. Narrow the query or reduce the event limit.'}]}
            request(data,'/gateway/relay/result',{'request_id':job['request_id'],'result':result},True)
            usage.record_rpc({'method':'tools/call','params':{'name':job['name']}},{'result':result},(time.monotonic()-started)*1000);usage.kick()
            delay=1
        except bridge.BridgeError:
            print('Relay temporarily unavailable; retrying. Check account connection status if this persists.',file=sys.stderr,flush=True)
            for _ in range(delay*4):
                if stop_path().exists():return
                time.sleep(.25)
            delay=min(delay*2,30)

def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    link=sub.add_parser('connect');link.add_argument('--url',default=DEFAULT_URL);link.add_argument('--name',required=True);link.add_argument('--no-browser',action='store_true')
    share=sub.add_parser('share-project');share.add_argument('path');share.add_argument('--name')
    unshare=sub.add_parser('unshare-project');unshare.add_argument('project_id')
    sub.add_parser('status');sub.add_parser('serve');sub.add_parser('start');sub.add_parser('stop')
    args=parser.parse_args()
    try:
        if args.command=='connect':value=connect(args.url,args.name,not args.no_browser)
        elif args.command=='share-project':value=share_project(args.path,args.name)
        elif args.command=='unshare-project':
            data=load();data['projects'].pop(args.project_id,None);save(data);value={'unshared':True}
        elif args.command=='start':value=start()
        elif args.command=='stop':value=stop()
        elif args.command=='status':
            data=load();value={key:data[key] for key in ('url','label','connection_id')};value['shared_projects']=len(data.get('projects',{}))
            value['running']=bool(running());value['starts_after_reboot']=False
        else:serve();return 0
        print(json.dumps(value,indent=2));return 0
    except (bridge.BridgeError,OSError,ValueError,KeyError):
        print('Cloud connection could not complete. Check the local configuration, account approval, and network access.',file=sys.stderr);return 1
if __name__=='__main__':raise SystemExit(main())
