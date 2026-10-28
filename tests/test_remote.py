import copy
import json
from pathlib import Path
from types import SimpleNamespace
import uuid
import pytest
import logforge_remote as remote

@pytest.fixture
def plan(tmp_path):
    value={'schema_version':'1.0','kind':'remote-agents','deployment_id':str(uuid.uuid4()),
        'central':{'url':'https://central.example/unicron','mtls_url':'https://central.example:8443'},
        'targets':[{'agent_name':'checkout-prod','host':'server.example','username':'ubuntu'}]}
    path=tmp_path/'agents.logforge'
    path.write_text(json.dumps(value))
    return path,value

@pytest.mark.parametrize('field,value',[('host','-oProxyCommand=bad'),('host','host;whoami'),('username','-root'),('port',True),('private_key','secret')])
def test_rejects_unsafe_plan_values(plan,field,value):
    path,data=plan
    data['targets'][0][field]=value
    path.write_text(json.dumps(data))
    with pytest.raises(remote.RemoteError): remote.load_plan(path)

def test_rejects_duplicate_agents_and_unreachable_loopback_central(plan):
    path,data=plan
    data['targets'].append(copy.deepcopy(data['targets'][0]))
    path.write_text(json.dumps(data))
    with pytest.raises(remote.RemoteError,match='unique'): remote.load_plan(path)
    data['targets'].pop()
    data['central']['url']='https://127.0.0.1/unicron'
    path.write_text(json.dumps(data))
    with pytest.raises(remote.RemoteError,match='loopback'): remote.load_plan(path)

def test_ssh_keeps_enrollment_off_command_line_and_requires_host_trust(monkeypatch):
    captured={}
    def invoke(command,**kwargs):
        captured.update(command=command,**kwargs)
        return SimpleNamespace(returncode=0,stdout='{"ok":true,"result":{"existing":false}}')
    monkeypatch.setattr(remote.subprocess,'run',invoke)
    remote.ssh({'host':'server.example'},{'enrollment_token':'sensitive-enrollment'})
    assert 'sensitive-enrollment' not in str(captured['command'])
    assert 'sensitive-enrollment' in captured['input']
    assert 'StrictHostKeyChecking=yes' in captured['command']
    assert 'BatchMode=yes' in captured['command']

def test_partial_failure_report_continues_other_targets_and_is_readable(plan,monkeypatch):
    path,data=plan
    data['targets'].append({'agent_name':'second','host':'second.example'})
    path.write_text(json.dumps(data))
    monkeypatch.setattr(remote,'central_client',lambda _: (lambda *args,**kwargs:{},'public ca','fingerprint'))
    def transport(target,payload):
        if target['agent_name']=='checkout-prod': raise remote.RemoteError('Trusted host key is missing.')
        return {'existing':False,'central_reachable':True}
    monkeypatch.setattr(remote,'ssh',transport)
    result=remote.run_plan(path,'preview')
    report=json.loads(Path(result['report']).read_text())
    assert result['status']=='failed' and len(report['events'])==2
    assert not report['events'][0]['passed'] and report['events'][1]['passed']
    assert 'enrollment_token' not in json.dumps(report)
    import logforge_mcp as bridge
    read=bridge.inspect_run('logforge_run_read',{'project_path':str(path.parent),'run_id':report['run_id']})
    assert read['matching_event_count']==2

def test_existing_owned_agent_does_not_generate_new_enrollment(plan,monkeypatch):
    path,_=plan
    monkeypatch.setattr(remote,'central_client',lambda _: (lambda *a,**k:pytest.fail('no enrollment call expected'),'public ca','fingerprint'))
    monkeypatch.setattr(remote,'ssh',lambda *a,**k:{'existing':True,'action':'reused'})
    monkeypatch.setattr(remote,'wait_online',lambda *a,**k:{'status':'online','container_count':2})
    assert remote.run_plan(path,'apply')['status']=='passed'

def test_duplicate_central_identity_is_not_adopted(plan,monkeypatch):
    path,_=plan
    monkeypatch.setattr(remote,'central_client',lambda _: (lambda *a,**k:pytest.fail('no enrollment call expected'),'public ca','fingerprint'))
    monkeypatch.setattr(remote,'ssh',lambda *a,**k:{'existing':False})
    monkeypatch.setattr(remote,'agents',lambda _: [{'agent_name':'checkout-prod'}])
    result=remote.run_plan(path,'apply')
    assert result['status']=='failed'
    assert 'already exists' in Path(result['report']).read_text()

def test_stop_does_not_require_central_login(plan,monkeypatch):
    path,_=plan
    monkeypatch.setattr(remote,'central_client',lambda _:pytest.fail('stop must work without Central authentication'))
    monkeypatch.setattr(remote,'offline_central_ca',lambda _:(None,'cached public CA','fingerprint'))
    monkeypatch.setattr(remote,'ssh',lambda *a,**k:{'running':False,'volumes':'preserved'})
    assert remote.run_plan(path,'stop')['status']=='passed'

def test_unexpected_target_failure_still_writes_partial_report(plan,monkeypatch):
    path,_=plan
    monkeypatch.setattr(remote,'central_client',lambda _:(None,'public CA','fingerprint'))
    def bad_result(*args): raise KeyError('untrusted remote payload')
    monkeypatch.setattr(remote,'ssh',bad_result)
    result=remote.run_plan(path,'preview')
    assert result['status']=='failed'
    assert 'untrusted remote payload' not in Path(result['report']).read_text()

def test_remote_error_cannot_echo_enrollment_token(monkeypatch):
    monkeypatch.setattr(remote.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0,stdout=json.dumps({'ok':False,'error':'failed secret-enrollment'})))
    with pytest.raises(remote.RemoteError) as failure:
        remote.ssh({'host':'example.com'},{'enrollment_token':'secret-enrollment'})
    assert 'secret-enrollment' not in str(failure.value)

def test_central_preflight_failure_is_recorded_without_contacting_targets(plan,monkeypatch):
    path,_=plan
    def unavailable(_): raise remote.RemoteError('Central access is unavailable.')
    monkeypatch.setattr(remote,'central_client',unavailable)
    monkeypatch.setattr(remote,'ssh',lambda *a,**k:pytest.fail('preflight failed'))
    result=remote.run_plan(path,'apply')
    assert result['status']=='failed'
    assert 'Central access is unavailable.' in Path(result['report']).read_text()
