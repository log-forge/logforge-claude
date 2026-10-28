"""Provision an owned, loopback-only LogForge appliance; never adopt or delete user resources."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import logforge_mcp as bridge

APPLIANCE_IMAGE = 'logforge/unicron@sha256:80ce7b65e1950d026f16789f1feb117c3d20894de83a3d5cb0729ffbc3c7a93a'
AGENT_IMAGE = 'logforge/unicron-agent@sha256:328adfef3bb1efc02a26390591b2c68d282d4c0b6c1b5034ffda1d5aa3fea197'
LABEL = 'dev.logforge.claude.owner'
INSTALL_DOCS = {
    'Windows': 'https://docs.docker.com/desktop/setup/install/windows-install/',
    'Darwin': 'https://docs.docker.com/desktop/setup/install/mac-install/',
    'Linux': 'https://docs.docker.com/engine/install/',
}

class SetupError(Exception):
    pass

def home():
    return Path(os.environ.get('LOGFORGE_SETUP_HOME', str(bridge.config_path().parent / 'managed'))).expanduser().resolve()

def run(args, *, check=True, timeout=120):
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise SetupError('Docker could not complete the operation; check installation and engine availability.') from None
    if check and result.returncode:
        # Docker error output can contain an environment value; do not export it.
        raise SetupError('Docker operation failed: ' + ' '.join(args[:2]) + '. Existing data is preserved; run status and inspect Docker locally.')
    return result

def docker_ready():
    if not shutil.which('docker'):
        raise SetupError('Docker is not installed. Install it using ' + INSTALL_DOCS.get(platform.system(), INSTALL_DOCS['Linux']) + '. Local LogForge.out diagnostics remain available without Docker.')
    result = run(['docker', 'info', '--format', '{{.OSType}}'], check=False, timeout=20)
    if result.returncode:
        raise SetupError('Docker is installed but its engine is unavailable. Start Docker Desktop or the Docker Engine service, then retry.')
    if result.stdout.strip() != 'linux':
        raise SetupError('The LogForge appliance requires Docker Linux containers. Switch Docker to its Linux engine.')
    explicit_context = os.environ.get('DOCKER_CONTEXT')
    endpoint = None if explicit_context else os.environ.get('DOCKER_HOST')
    if not endpoint:
        context = explicit_context or run(['docker', 'context', 'show']).stdout.strip()
        endpoint = run(['docker', 'context', 'inspect', context, '--format', '{{.Endpoints.docker.Host}}']).stdout.strip()
    if not endpoint.startswith(('unix://', 'npipe://')):
        raise SetupError('Automatic provisioning requires a local Docker socket/context. A remote Docker context was detected; use an explicitly reviewed remote deployment instead.')

def private_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        path.parent.chmod(0o700)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(value, handle, indent=2)
    temporary.replace(path)
    if os.name != 'nt':
        path.chmod(0o600)

@contextmanager
def setup_lock():
    home().mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        home().chmod(0o700)
    with open(home() / 'setup.lock', 'a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SetupError('Another LogForge setup operation is running.') from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

def read_state():
    path = home() / 'state.json'
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding='utf-8'))
    uuid.UUID(state['owner'])
    if state.get('schema_version') != 1:
        raise SetupError('Unsupported managed-instance state. No Docker resources were changed.')
    return state

def names(state):
    base = 'logforge-claude-' + uuid.UUID(state['owner']).hex[:12]
    return {'central': base, 'network': base, 'volume': base + '-data', 'agent': base + '-agent', 'agent_volume': base + '-agent-data'}

def owned_object(kind, name, state):
    result = run(['docker', kind, 'inspect', name], check=False)
    if result.returncode:
        return None
    info = json.loads(result.stdout)[0]
    labels = info.get('Config', {}).get('Labels') if kind == 'container' else info.get('Labels')
    if (labels or {}).get(LABEL) != state['owner']:
        raise SetupError('A resource with the expected name belongs to another owner: ' + name + '. It was not modified.')
    return info

def free_ports():
    sockets = []
    try:
        for _ in range(3):
            sock = socket.socket(); sock.bind(('127.0.0.1', 0)); sockets.append(sock)
        return dict(zip(('http', 'https', 'mtls'), [s.getsockname()[1] for s in sockets]))
    finally:
        for sock in sockets:
            sock.close()

def environment(state):
    if 'environment_dpapi' in state:
        return json.loads(bridge.protect_secret(state['environment_dpapi'], decrypt=True))
    return state['environment']

def new_state():
    env = {key: secrets.token_hex(32) for key in ('POSTGRES_PASSWORD', 'CENTRAL_AUTH_SECRET', 'INTERNAL_API_SECRET',
        'DEPLOYMENT_ENCRYPTION_KEY', 'CSRF_COOKIE_SECRET', 'CSRF_SECRET', 'STEP_CA_PASSWORD', 'STEP_CA_PROVISIONER_PASSWORD', 'STEP_CA_RA_PASSWORD')}
    env.update(UNICRON_CENTRAL_FQDN='localhost', CENTRAL_ADMIN_USERNAME='admin',
        CENTRAL_ADMIN_PASSWORD='Lf!9aA' + secrets.token_urlsafe(32), CENTRAL_ADMIN_RECOVERY_OVERRIDE='false', REMOTE_AGENT_IMAGE=AGENT_IMAGE)
    state = {'schema_version': 1, 'owner': str(uuid.uuid4()), 'ports': free_ports()}
    if os.name == 'nt':
        state['environment_dpapi'] = bridge.protect_secret(json.dumps(env))
    else:
        state['environment'] = env
    return state

@contextmanager
def env_file(env):
    fd, filename = tempfile.mkstemp(prefix='launch-', suffix='.env', dir=home())
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            for key, value in env.items():
                if '\n' in str(value) or '\r' in str(value):
                    raise SetupError('Invalid environment value')
                handle.write(key + '=' + str(value) + '\n')
        yield filename
    finally:
        Path(filename).unlink(missing_ok=True)

def create_storage(kind, name, state):
    if owned_object(kind, name, state) is None:
        run(['docker', kind, 'create', '--label', LABEL + '=' + state['owner'], name])

def ensure_central(state):
    resources = names(state)
    info = owned_object('container', resources['central'], state)
    if info:
        if not info['State']['Running']:
            run(['docker', 'start', resources['central']])
        return
    create_storage('network', resources['network'], state)
    create_storage('volume', resources['volume'], state)
    state['ports'] = free_ports()
    private_write(home() / 'state.json', state)
    args = ['docker', 'run', '-d', '--name', resources['central'], '--label', LABEL + '=' + state['owner'],
        '--restart', 'unless-stopped', '--read-only', '--tmpfs', '/tmp:rw,nosuid,nodev,mode=1777,size=256m',
        '--tmpfs', '/run:rw,nosuid,nodev,mode=755,size=64m', '--tmpfs', '/run/pyinstaller:rw,nosuid,nodev,exec,mode=1777,size=256m',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--network', resources['network'], '--network-alias', 'unicron.central']
    for capability in ('CHOWN', 'DAC_OVERRIDE', 'FOWNER', 'KILL', 'SETGID', 'SETUID', 'NET_BIND_SERVICE'):
        args += ['--cap-add', capability]
    for host in ('unicron-stepca', 'unicron-stepca-ra', 'unicron.central', 'localhost'):
        args += ['--add-host', host + ':127.0.0.1']
    for role, internal in (('http', 80), ('https', 443), ('mtls', 8443)):
        args += ['-p', f'127.0.0.1:{state["ports"][role]}:{internal}']
    env = environment(state)
    env.update(UNICRON_PUBLIC_CENTRAL_PORT=str(state['ports']['https']), UNICRON_PUBLIC_CENTRAL_MTLS_PORT=str(state['ports']['mtls']),
        UNICRON_APPLIANCE_CONTAINER_NAME=resources['central'], LOCAL_AGENT_DOCKER_NETWORK=resources['network'],
        LOCAL_AGENT_CENTRAL_URL='https://unicron.central/unicron', UNICRON_SELF_UPDATE_ENABLED='false', TMPDIR='/run/pyinstaller')
    with env_file(env) as filename:
        run(args + ['--env-file', filename, '-v', resources['volume'] + ':/var/lib/unicron', APPLIANCE_IMAGE], timeout=600)

def wait_ready(state, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = owned_object('container', names(state)['central'], state)
        if info and info['State'].get('Health', {}).get('Status') == 'healthy':
            return
        if info and info['State'].get('Status') in ('exited', 'dead'):
            raise SetupError('The managed LogForge container stopped during startup. Its data is preserved; inspect its Docker logs locally.')
        time.sleep(2)
    raise SetupError('LogForge is still starting or unhealthy. Retry start after checking Docker; no volumes were deleted.')

def admin_client(state):
    ca = home() / 'root_ca.crt'
    owned_object('container', names(state)['central'], state)
    run(['docker', 'cp', names(state)['central'] + ':/var/lib/unicron/pki/trust/root_ca.crt', str(ca)])
    base = f'https://localhost:{state["ports"]["https"]}'
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(bridge.NoRedirect(), urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(ca))))
    def request(path, data=None):
        req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
            headers={'Content-Type': 'application/json', 'Origin': base})
        try:
            with opener.open(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise SetupError('Managed LogForge setup API ' + path + ' returned HTTP ' + str(error.code) + '. No credentials or response body were printed.') from None
        except (urllib.error.URLError, ValueError, TimeoutError):
            raise SetupError('Managed LogForge setup API is unavailable with trusted TLS. Retry when the appliance is healthy.') from None
    env = environment(state)
    request('/unicron/auth/api/auth/sign-in/username', {'username': env['CENTRAL_ADMIN_USERNAME'], 'password': env['CENTRAL_ADMIN_PASSWORD']})
    return request, base, ca

def ensure_agent(state, request, ca):
    resources = names(state)
    info = owned_object('container', resources['agent'], state)
    if info:
        if not info['State']['Running']:
            run(['docker', 'start', resources['agent']])
        return
    create_storage('volume', resources['agent_volume'], state)
    result = request('/unicron/api/agent/enroll', {'agent_name': resources['agent'], 'install_target': 'local', 'queue_mode': 'memory', 'memory_queue_mb': 32})
    if result.get('ok') is not True or not isinstance(result.get('token'), str):
        raise SetupError('Managed agent enrollment was not accepted.')
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(ca.read_text())).hexdigest()
    env = {'AGENT_NAME': resources['agent'], 'HOST_ID': resources['agent'], 'HERALD_NAME': resources['agent'], 'HERALD_ID': resources['agent'],
        'CENTRAL_URL': 'https://unicron.central/unicron', 'CENTRAL_MTLS_URL': 'https://unicron.central:8443',
        'CENTRAL_WS_URL': 'wss://unicron.central:8443/unicron/api/agent/ws', 'ENROLL_TOKEN': result['token'], 'CA_FINGERPRINT': fingerprint,
        'HERALD_CA_ROOT': '/agent-data/certs/root_ca.crt', 'HERALD_CERT': '/agent-data/certs/agent.crt', 'HERALD_KEY': '/agent-data/certs/agent.key',
        'TELEMETRY_MODE': 'hybrid', 'TELEMETRY_QUEUE_MODE': 'memory', 'TELEMETRY_MEMORY_QUEUE_MB': '32', 'FLB_MEM_BUF_LIMIT': '32MB',
        'FLB_TAIL_DB_PATH': '/dev/shm/flb_monitored.db', 'ENVIRONMENT': 'development'}
    # Construct the command ourselves. Never execute the server's shell command or its reset/deletion preamble.
    with env_file(env) as filename:
        run(['docker', 'run', '-d', '--name', resources['agent'], '--label', LABEL + '=' + state['owner'], '--restart', 'unless-stopped',
            '--network', resources['network'], '--env-file', filename, '-v', '/var/run/docker.sock:/var/run/docker.sock',
            '-v', '/var/lib/docker/containers:/var/lib/docker/containers:ro', '-v', '/:/host/root:ro',
            '-v', resources['agent_volume'] + ':/agent-data', AGENT_IMAGE], timeout=600)

def wait_agent(state, request, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        agents = request('/unicron/api/agent/list').get('agents', [])
        for agent in agents:
            if agent.get('agent_name') == names(state)['agent'] and agent.get('status') == 'online' and agent.get('container_count', 0) > 0:
                return {'agent': agent['agent_name'], 'status': 'online', 'inventory_received': True}
        time.sleep(2)
    raise SetupError('The managed Docker agent has not connected and delivered inventory. Check its logs locally; a running container alone is not verified monitoring.')

def status():
    result = {'local_diagnostics': 'available without Docker or a configured appliance', 'platform': platform.system(),
        'docker_install_docs': INSTALL_DOCS.get(platform.system(), INSTALL_DOCS['Linux']),
        'configuration_present': bridge.config_path().exists() or bool(os.getenv('LOGFORGE_MCP_URL') and os.getenv('LOGFORGE_MCP_API_KEY'))}
    try:
        docker_ready(); result['docker'] = 'ready'
    except SetupError as error:
        result.update(docker='unavailable', next_step=str(error)); return result
    state = read_state()
    if not state:
        result['managed_instance'] = 'not provisioned'; return result
    resources = names(state)
    info = owned_object('container', resources['central'], state)
    agent = owned_object('container', resources['agent'], state)
    result.update(managed_instance=info['State']['Status'] if info else 'missing', container=resources['central'],
        url=f'https://localhost:{state["ports"]["https"]}/unicron', docker_agent_container=agent['State']['Status'] if agent else 'not enabled')
    return result

def start(monitor_docker=False, timeout=240):
    docker_ready()
    if os.getenv('LOGFORGE_MCP_URL') or os.getenv('LOGFORGE_MCP_API_KEY'):
        raise SetupError('An explicit environment connection is configured. Preserve it or select a separate setup profile before provisioning.')
    with setup_lock():
        state = read_state()
        if bridge.config_path().exists():
            expected = f'https://localhost:{state["ports"]["https"]}/unicron/api/mcp' if state else None
            if bridge.load_config()['url'] != expected:
                raise SetupError('An existing LogForge connection is configured; it was not replaced. Use that connection or explicitly select a separate profile.')
        if state is None:
            state = new_state(); private_write(home() / 'state.json', state)
        ensure_central(state); wait_ready(state, timeout)
        request, base, ca = admin_client(state)
        if not bridge.config_path().exists():
            created = request('/unicron/auth/api/v1/api-keys', {'name': 'Claude managed read-only', 'scopes': ['read']})
            config = {'url': base + '/unicron/api/mcp', 'api_key': created['data']['key'], 'ca_file': str(ca)}
            bridge.doctor(config); bridge.save_config(config)
            state['key_id'] = created['data']['apiKey']['id']; private_write(home() / 'state.json', state)
        monitoring = None
        if monitor_docker or owned_object('container', names(state)['agent'], state) is not None:
            ensure_agent(state, request, ca)
            monitoring = wait_agent(state, request, timeout)
        checked = bridge.doctor(bridge.load_config())
        return {**status(), 'connection': checked, 'monitoring_verification': monitoring, 'reconnect_mcp': True, 'publisher_usage': 'unchanged; separate consent required'}

def monitor(container_key, enabled=True):
    docker_ready()
    with setup_lock():
        state = read_state()
        if not state or not container_key.startswith(names(state)['agent'] + ':'):
            raise SetupError('Select an inventory key belonging to this managed agent; other hosts were not modified.')
        config = bridge.load_config()
        if config['url'] != f'https://localhost:{state["ports"]["https"]}/unicron/api/mcp':
            raise SetupError('The active connection does not belong to this managed appliance; it was not modified.')
        gateway = bridge.Gateway(config)
        response = gateway.call({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'logforge_inventory_search', 'arguments': {'container_key': container_key, 'limit': 1}}})
        result = response.get('result', {})
        data = result.get('structuredContent') or json.loads(result['content'][0]['text'])
        matches = [item for item in data.get('containers', []) if item.get('container_key') == container_key and item.get('herald_id') == names(state)['agent']]
        if result.get('isError') or not matches:
            raise SetupError('The selected container was not found in this managed agent inventory.')
        request, _, _ = admin_client(state)
        return request('/unicron/api/containers/' + urllib.parse.quote(container_key, safe='') + '/monitoring?host_id=' + urllib.parse.quote(names(state)['agent'], safe=''), {'enabled': enabled})

def stop():
    docker_ready()
    with setup_lock():
        state = read_state()
        if not state:
            return {'stopped': [], 'volumes': 'preserved'}
        stopped = []
        for role in ('agent', 'central'):
            name = names(state)[role]
            info = owned_object('container', name, state)
            if info and info['State']['Running']:
                run(['docker', 'stop', name]); stopped.append(name)
        return {'stopped': stopped, 'volumes': 'preserved'}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status'); sub.add_parser('stop')
    monitor_parser = sub.add_parser('monitor', help='Enable or disable monitoring for one explicitly selected workload on the managed agent.')
    monitor_parser.add_argument('--container-key', required=True)
    monitor_parser.add_argument('--disable', action='store_true')
    start_parser = sub.add_parser('start')
    start_parser.add_argument('--monitor-docker', action='store_true', help='Enroll the managed agent with local Docker socket access; requires authorization to monitor this Docker host.')
    start_parser.add_argument('--timeout', type=int, default=240)
    args = parser.parse_args()
    try:
        if args.command == 'status':
            result = status()
        elif args.command == 'stop':
            result = stop()
        elif args.command == 'monitor':
            result = monitor(args.container_key, not args.disable)
        else:
            result = start(args.monitor_docker, args.timeout)
        print(json.dumps(result, indent=2)); return 0
    except (SetupError, bridge.BridgeError, OSError, ValueError, KeyError) as error:
        print(json.dumps({'ok': False, 'error': str(error)})); return 1

if __name__ == '__main__':
    raise SystemExit(main())
