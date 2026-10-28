"""Preview, deploy, verify, or stop LogForge agents on explicitly selected Linux SSH hosts."""
import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import logforge_mcp as bridge
import logforge_setup as setup

class RemoteError(Exception):
    pass

def url(value, path=None):
    parsed=urllib.parse.urlsplit(value)
    try:
        parsed.port
    except ValueError:
        raise RemoteError('Invalid HTTPS endpoint port.') from None
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RemoteError('Use a trusted HTTPS endpoint without credentials, query, or fragment.')
    if path is not None and parsed.path.rstrip('/')!=path:
        raise RemoteError('Unexpected Central endpoint path.')
    return value.rstrip('/')

def load_plan(path):
    path=Path(path).resolve()
    plan=json.loads(path.read_text(encoding='utf-8'))
    def fields(value,allowed):
        if not isinstance(value,dict) or set(value)-set(allowed.split()):
            raise RemoteError('The deployment plan contains unsupported fields or an invalid object; use the remote-agents schema.')
    fields(plan,'schema_version kind deployment_id name central targets')
    if plan.get('kind')!='remote-agents' or plan.get('schema_version')!='1.0':
        raise RemoteError('Expected a remote-agents plan, schema_version 1.0.')
    plan['deployment_id']=str(uuid.UUID(plan['deployment_id']))
    central=plan['central']
    fields(central,'url mtls_url admin_config ca_file')
    central['url']=url(central['url'],'/unicron')
    central['mtls_url']=url(central['mtls_url'],'')
    for endpoint in (central['url'],central['mtls_url']):
        hostname=urllib.parse.urlsplit(endpoint).hostname
        if hostname=='localhost':
            raise RemoteError('Remote agents require a reachable Central address; localhost refers to the remote target.')
        try:
            if ipaddress.ip_address(hostname).is_loopback:
                raise RemoteError('A loopback Central address cannot serve remote agents.')
        except ValueError:
            pass
    targets=plan.get('targets',[])
    if not isinstance(targets,list) or not 1<=len(targets)<=32:
        raise RemoteError('Select between 1 and 32 explicit targets.')
    names=set()
    for target in targets:
        fields(target,'agent_name host username port identity_file known_hosts_file extra_hosts')
        name=target.get('agent_name','')
        if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,62}',name) or name in names:
            raise RemoteError('Agent names must be unique, lowercase, and 1–63 safe characters.')
        names.add(name)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}',target.get('host','')):
            raise RemoteError('Invalid SSH hostname or alias.')
        if target.get('username') and not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}',target['username']):
            raise RemoteError('Invalid SSH username.')
        if type(target.get('port',22)) is not int or not 1<=target.get('port',22)<=65535:
            raise RemoteError('Invalid SSH port.')
        if 'private_key' in target or 'password' in target:
            raise RemoteError('Plans must contain credential file paths, never private keys or passwords.')
        for field in ('identity_file','known_hosts_file'):
            if target.get(field):
                p=Path(target[field]).expanduser()
                target[field]=str((p if p.is_absolute() else path.parent/p).resolve())
                if not Path(target[field]).is_file():
                    raise RemoteError('An SSH credential or known-hosts file is missing.')
        for hostname,address in target.get('extra_hosts',{}).items():
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}',hostname):
                raise RemoteError('Invalid extra_hosts name.')
            ipaddress.ip_address(address)
    for field in ('admin_config','ca_file'):
        if central.get(field):
            p=Path(central[field]).expanduser()
            central[field]=str((p if p.is_absolute() else path.parent/p).resolve())
    return path,plan

def external_client(config_path):
    config=json.loads(Path(config_path).read_text())
    base=url(config['url'],'/unicron')
    password=bridge.protect_secret(config['password_dpapi'],decrypt=True) if 'password_dpapi' in config else config['password']
    jar=http.cookiejar.CookieJar()
    opener=urllib.request.build_opener(bridge.NoRedirect(),urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=config.get('ca_file'))))
    def request(path,data=None):
        endpoint=base.removesuffix('/unicron')+path
        req=urllib.request.Request(endpoint,data=json.dumps(data).encode() if data is not None else None,
            headers={'Content-Type':'application/json','Origin':base.removesuffix('/unicron')})
        try:
            with opener.open(req,timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise RemoteError('Central returned HTTP '+str(error.code)+'; its access and entitlement checks remain in force.') from None
        except (OSError,ValueError):
            raise RemoteError('Central could not be reached with trusted TLS.') from None
    request('/unicron/auth/api/auth/sign-in/username',{'username':config['username'],'password':password})
    return request

def central_client(plan):
    central=plan['central']
    if central.get('admin_config'):
        request=external_client(central['admin_config'])
        if not central.get('ca_file'):
            raise RemoteError('Provide the Central root CA file so remote bootstrap can verify its fingerprint.')
        ca=Path(central['ca_file'])
    else:
        state=setup.read_state()
        if not state:
            raise RemoteError('Configure Central administrator access or select an existing managed setup profile.')
        request,_,ca=setup.admin_client(state)
    pem=ca.read_text()
    fingerprint=hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    return request,pem,fingerprint

def offline_central_ca(plan):
    """Stopping an owned agent must not require an online Central login."""
    central=plan['central']
    ca=Path(central['ca_file']) if central.get('ca_file') else setup.home()/'root_ca.crt'
    pem=ca.read_text()
    return None,pem,hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()

def ssh(target,payload):
    command=['ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=15']
    if target.get('identity_file'):
        command+=['-o','IdentitiesOnly=yes','-i',target['identity_file']]
    if target.get('known_hosts_file'):
        command+=['-o','UserKnownHostsFile='+target['known_hosts_file']]
    command+=['-p',str(target.get('port',22))]
    if target.get('username'):
        command+=['-l',target['username']]
    bootstrap="import json,sys; payload=json.load(sys.stdin); source=payload.pop('worker'); exec(compile(source,'logforge_remote_worker.py','exec')); main(payload)"
    command += ['--',target['host'],'python3 -c '+shlex.quote(bootstrap)]
    payload={**payload,'worker':Path(__file__).with_name('logforge_remote_worker.py').read_text()}
    try:
        result=subprocess.run(command,input=json.dumps(payload),capture_output=True,text=True,encoding='utf-8',timeout=660)
    except (OSError,subprocess.TimeoutExpired):
        raise RemoteError('SSH could not complete. Check access, Python 3, Docker, and target readiness.') from None
    if result.returncode:
        raise RemoteError('SSH/remote Python failed. Check the trusted host key, credentials, Python 3, and Docker permissions. Raw output was withheld.')
    try:
        response=json.loads(result.stdout)
    except ValueError:
        raise RemoteError('Remote worker did not return a valid result; raw output was withheld.') from None
    if not isinstance(response,dict):
        raise RemoteError('Remote worker returned an invalid result; raw output was withheld.')
    if response.get('ok') is not True:
        message=str(response.get('error','Remote operation failed.'))
        if payload.get('enrollment_token'):
            message=message.replace(payload['enrollment_token'],'[REDACTED]')
        raise RemoteError(message[:500])
    allowed={'container','existing','running','central_reachable','action','volumes'}
    if not isinstance(response.get('result'),dict):
        raise RemoteError('Remote worker returned an invalid result; raw output was withheld.')
    return {key:value for key,value in response['result'].items() if key in allowed}

def agents(request):
    found=[]
    for offset in range(0,10000,1000):
        page=request('/unicron/api/agent/list?limit=1000&offset='+str(offset))
        found.extend(page.get('agents',[]))
        if len(found)>=page.get('total',len(found)):
            return found
    raise RemoteError('Agent inventory exceeds the bounded deployment lookup.')

def wait_online(request,name,timeout):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        for agent in agents(request):
            if agent.get('agent_name')==name:
                if agent.get('status')=='blocked':
                    raise RemoteError('Central blocked this agent. Resolve its normal admission/entitlement requirements.')
                if agent.get('status')=='online' and agent.get('container_count',0)>0:
                    return {'status':'online','container_count':agent['container_count']}
        time.sleep(2)
    raise RemoteError('Agent has not become online with inventory; its container and data were preserved for diagnosis.')

def run_plan(path,operation,timeout=180):
    path,plan=load_plan(path)
    central_error=None
    request=pem=fingerprint=None
    try:
        request,pem,fingerprint=offline_central_ca(plan) if operation=='stop' else central_client(plan)
    except (RemoteError,setup.SetupError,bridge.BridgeError) as error:
        central_error=str(error)
    except (OSError,ValueError,KeyError,TypeError):
        central_error='Central preflight failed. Check the protected administrator configuration and trusted CA file.'
    started=datetime.now(timezone.utc).isoformat()
    run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]
    events=[]
    for target in plan['targets']:
        name=target['agent_name']
        data={'target':name,'operation':operation}
        passed=False
        try:
            if central_error:
                raise RemoteError(central_error)
            spec={'agent_name':name,'central_url':plan['central']['url'],'mtls_url':plan['central']['mtls_url'],
                'ca_fingerprint':fingerprint,'image':setup.AGENT_IMAGE,'extra_hosts':target.get('extra_hosts',{})}
            payload={'operation':'stop' if operation=='stop' else 'inspect','deployment_id':plan['deployment_id'],'spec':spec,'ca_pem':pem}
            data['preflight']=ssh(target,payload)
            if operation=='apply':
                if not data['preflight']['existing']:
                    if any(a.get('agent_name')==name for a in agents(request)):
                        raise RemoteError('An agent with this name already exists in Central but is not owned on this target. Choose a unique name.')
                    enrollment=request('/unicron/api/agent/enroll',{'agent_name':name,'install_target':'remote','central_url':spec['central_url'],'queue_mode':'memory','memory_queue_mb':32})
                    if enrollment.get('ok') is not True or not enrollment.get('token'):
                        raise RemoteError('Central did not authorize enrollment.')
                    payload['enrollment_token']=enrollment['token']
                payload['operation']='apply'
                data['deployment']=ssh(target,payload)
                payload.pop('enrollment_token',None)
                data['verification']=wait_online(request,name,timeout)
            passed=True
        except (RemoteError,setup.SetupError,bridge.BridgeError) as error:
            data['error']=str(error)
        except (OSError,ValueError,KeyError,TypeError):
            data['error']='Target operation could not complete; inspect local configuration and target readiness. Raw data was withheld.'
        events.append({'event_id':uuid.uuid4().hex,'run_id':run_id,'name':'remote.'+operation+'.'+name,'kind':'check',
            'tags':['remote-deployment',name],'source':None,'passed':passed,'data':data})
    failures=[e['name'] for e in events if not e['passed']]
    report={'schema_version':'1.0','kind':'run','run_id':run_id,'project':plan.get('name','Remote LogForge agents'),
        'plan':path.name,'started_at':started,'finished_at':datetime.now(timezone.utc).isoformat(),'status':'failed' if failures else 'passed',
        'selected_tags':['remote-deployment'],'events':events,'expectations':[],'commands':[],
        'summary':{'outputs':0,'checks':len(events),'failed_checks':failures,'filtered':0,'unreached':0,'dropped':0,'rejected':0,'diagnostic_errors':0},
        'coverage_note':'Remote deployment verifies trusted SSH/TLS, owned resource handling, and online inventory. Application log retention and feature checks must be exercised separately.'}
    destination=path.parent/'.logforge/runs'/(run_id+'.logforge')
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    from logforge_usage import record_run, kick
    record_run(report)
    kick()
    return {'status':report['status'],'report':str(destination),'summary':report['summary']}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('preview','apply','stop'):
        child=sub.add_parser(name)
        child.add_argument('plan',type=Path)
        child.add_argument('--timeout',type=int,default=180)
    config=sub.add_parser('configure-central')
    config.add_argument('--url',required=True)
    config.add_argument('--username',required=True)
    config.add_argument('--ca-file',type=Path)
    config.add_argument('--output',type=Path,default=bridge.config_path().parent/'remote-admin.json')
    args=parser.parse_args()
    try:
        if args.command=='configure-central':
            value={'url':url(args.url,'/unicron'),'username':args.username}
            if args.ca_file:
                value['ca_file']=str(args.ca_file.resolve())
            password=getpass.getpass('LogForge administrator password (hidden): ')
            value['password_dpapi' if os.name=='nt' else 'password']=bridge.protect_secret(password) if os.name=='nt' else password
            setup.private_write(args.output,value)
            print(json.dumps({'configured':str(args.output),'credentials':'protected outside the plugin'}))
            return 0
        result=run_plan(args.plan,args.command,args.timeout)
        print(json.dumps(result,indent=2))
        return 0 if result['status']=='passed' else 1
    except (RemoteError,setup.SetupError,bridge.BridgeError) as error:
        print(json.dumps({'ok':False,'error':str(error)}))
        return 1
    except (OSError,ValueError,KeyError,TypeError):
        print(json.dumps({'ok':False,'error':'Invalid configuration or unavailable local files. No secret-bearing exception details were printed.'}))
        return 1

if __name__=='__main__':
    raise SystemExit(main())
