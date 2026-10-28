"""Linux SSH worker. Invoked by logforge_remote.py with an in-memory JSON payload."""
import fcntl
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import urllib.parse
import urllib.request

LABEL = 'dev.logforge.claude.remote-owner'
SPEC_LABEL = 'dev.logforge.claude.remote-spec'

class RemoteError(Exception):
    pass

@contextmanager
def operation_lock(owner):
    directory=Path.home()/'.local/state/logforge-remote'/owner
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    directory.chmod(0o700)
    with open(directory/'operation.lock','a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            raise RemoteError('Another deployment is running for this target.') from None
        yield directory

def docker(args, check=True):
    env = {k:v for k,v in os.environ.items() if k not in ('DOCKER_HOST','DOCKER_CONTEXT')}
    try:
        result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=600, env=env)
    except (OSError, subprocess.TimeoutExpired):
        raise RemoteError('Docker is missing or did not finish; install/start Docker and retry.') from None
    if check and result.returncode:
        raise RemoteError('Docker operation failed. Inspect the target locally; secret-bearing output was withheld.')
    return result

def inspect(kind, name, owner):
    result = docker([kind, 'inspect', name], check=False)
    if result.returncode:
        # The engine must still be reachable before treating a resource as absent.
        docker(['info','--format','{{.OSType}}'])
        return None
    value = json.loads(result.stdout)[0]
    labels = value.get('Config',{}).get('Labels') if kind == 'container' else value.get('Labels')
    if (labels or {}).get(LABEL) != owner:
        raise RemoteError('A target resource belongs to another owner. It was not changed.')
    return value

def execute(payload):
    spec = payload['spec']
    name = 'logforge-agent-' + spec['agent_name']
    volume = name + '-data'
    owner = payload['deployment_id']
    digest = hashlib.sha256(json.dumps(spec,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if docker(['info','--format','{{.OSType}}']).stdout.strip() != 'linux':
        raise RemoteError('Remote deployment requires a Linux Docker engine.')
    context = docker(['context','show']).stdout.strip()
    endpoint = docker(['context','inspect',context,'--format','{{.Endpoints.docker.Host}}']).stdout.strip()
    if not endpoint.startswith('unix://'):
        raise RemoteError('The SSH target must use its own local Docker socket, not another remote context.')
    existing = inspect('container', name, owner)
    inspect('volume', volume, owner)
    if existing and existing['Config']['Labels'].get(SPEC_LABEL) != digest:
        raise RemoteError('This agent has a different deployment specification. Explicit migration is required; its data was preserved.')
    result = {'container':name,'existing':existing is not None,'running':bool(existing and existing['State']['Running'])}
    if payload['operation'] == 'stop':
        with operation_lock(owner):
            current=inspect('container',name,owner)
            if current and current['Config']['Labels'].get(SPEC_LABEL)!=digest:
                raise RemoteError('Concurrent deployment changed this agent specification.')
            if current and current['State']['Running']:
                docker(['stop',name])
        return {**result,'running':False,'volumes':'preserved'}
    context = ssl.create_default_context(cadata=payload['ca_pem'])
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):
            return None
    try:
        opener = urllib.request.build_opener(NoRedirect(),urllib.request.HTTPSHandler(context=context))
        with opener.open(spec['central_url'] + '/api/pki/ca/root', timeout=15) as response:
            if response.status != 200:
                raise RemoteError('Central CA endpoint was not reachable.')
        parsed = urllib.parse.urlsplit(spec['mtls_url'])
        with socket.create_connection((parsed.hostname,parsed.port or 443),timeout=10):
            pass
    except (OSError, ValueError):
        raise RemoteError('The target cannot reach Central with trusted TLS or its agent port. Resolve routing, DNS, and certificates first.') from None
    result['central_reachable'] = True
    if payload['operation'] == 'inspect':
        return result
    if payload['operation'] != 'apply':
        raise RemoteError('Unsupported remote operation.')
    with operation_lock(owner) as directory:
        # Recheck after acquiring the lock; no container/volume deletion is performed.
        current = inspect('container',name,owner)
        if current:
            if current['Config']['Labels'].get(SPEC_LABEL) != digest:
                raise RemoteError('Concurrent deployment changed this agent specification.')
            if not current['State']['Running']:
                docker(['start',name])
            return {**result,'running':True,'action':'reused'}
        token = payload.get('enrollment_token')
        if not token:
            raise RemoteError('Enrollment is required for a new agent.')
        if inspect('volume',volume,owner) is None:
            docker(['volume','create','--label',LABEL+'='+owner,volume])
        env = {'AGENT_NAME':spec['agent_name'],'HOST_ID':spec['agent_name'],'HERALD_ID':spec['agent_name'],'HERALD_NAME':spec['agent_name'],
            'CENTRAL_URL':spec['central_url'],'CENTRAL_MTLS_URL':spec['mtls_url'],
            'CENTRAL_WS_URL':spec['mtls_url'].replace('https://','wss://',1)+'/unicron/api/agent/ws',
            'ENROLL_TOKEN':token,'CA_FINGERPRINT':spec['ca_fingerprint'],
            'HERALD_CA_ROOT':'/agent-data/certs/root_ca.crt','HERALD_CERT':'/agent-data/certs/agent.crt','HERALD_KEY':'/agent-data/certs/agent.key',
            'TELEMETRY_MODE':'hybrid','TELEMETRY_QUEUE_MODE':'memory','TELEMETRY_MEMORY_QUEUE_MB':'32',
            'FLB_MEM_BUF_LIMIT':'32MB','FLB_TAIL_DB_PATH':'/dev/shm/flb_monitored.db','ENVIRONMENT':'production'}
        fd, filename = tempfile.mkstemp(dir=directory,prefix='enrollment-',suffix='.env')
        try:
            with os.fdopen(fd,'w') as handle:
                for key,value in env.items():
                    if '\n' in value or '\r' in value:
                        raise RemoteError('Invalid enrollment value.')
                    handle.write(key+'='+value+'\n')
            args = ['run','-d','--name',name,'--restart','unless-stopped','--label',LABEL+'='+owner,'--label',SPEC_LABEL+'='+digest,
                '--env-file',filename,'-v','/var/run/docker.sock:/var/run/docker.sock','-v','/var/lib/docker/containers:/var/lib/docker/containers:ro',
                '-v','/:/host/root:ro','-v',volume+':/agent-data']
            for hostname, address in spec.get('extra_hosts',{}).items():
                args += ['--add-host',hostname+':'+address]
            docker(args+[spec['image']])
        finally:
            Path(filename).unlink(missing_ok=True)
        return {**result,'running':True,'action':'created'}

def main(payload):
    try:
        print(json.dumps({'ok':True,'result':execute(payload)}))
    except (RemoteError,OSError,ValueError,KeyError) as error:
        message = str(error) if isinstance(error,RemoteError) else 'Remote operation failed; inspect the target locally.'
        print(json.dumps({'ok':False,'error':message}))
