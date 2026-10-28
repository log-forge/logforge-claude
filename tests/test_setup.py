import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
import logforge_mcp as bridge
import logforge_setup as setup

@pytest.fixture(autouse=True)
def isolated_setup(tmp_path, monkeypatch):
    monkeypatch.setenv('LOGFORGE_SETUP_HOME', str(tmp_path / 'managed'))
    monkeypatch.setenv('LOGFORGE_MCP_CONFIG', str(tmp_path / 'connection.json'))
    monkeypatch.delenv('LOGFORGE_MCP_URL', raising=False)
    monkeypatch.delenv('LOGFORGE_MCP_API_KEY', raising=False)
    monkeypatch.delenv('DOCKER_CONTEXT', raising=False)
    monkeypatch.delenv('DOCKER_HOST', raising=False)

def test_explicit_remote_context_cannot_be_hidden_by_local_docker_host(monkeypatch):
    monkeypatch.setattr(setup.shutil, 'which', lambda _: '/usr/bin/docker')
    monkeypatch.setenv('DOCKER_CONTEXT', 'production')
    monkeypatch.setenv('DOCKER_HOST', 'unix:///var/run/docker.sock')
    monkeypatch.setattr(setup, 'run', lambda args, **kwargs: SimpleNamespace(returncode=0, stdout='linux' if args[1] == 'info' else 'ssh://production.example'))
    with pytest.raises(setup.SetupError, match='remote Docker'):
        setup.start()
    assert not setup.home().exists()

def test_online_agent_without_inventory_is_not_verified(monkeypatch):
    state = setup.new_state()
    ticks = iter([0, 0, 5])
    monkeypatch.setattr(setup.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(setup.time, 'sleep', lambda _: None)
    with pytest.raises(setup.SetupError, match='delivered inventory'):
        setup.wait_agent(state, lambda _: {'agents': [{'agent_name': setup.names(state)['agent'], 'status': 'online', 'container_count': 0}]}, 1)

def test_missing_docker_is_actionable_and_does_not_create_state(monkeypatch):
    monkeypatch.setattr(setup.shutil, 'which', lambda _: None)
    result = setup.status()
    assert result['docker'] == 'unavailable'
    assert 'not installed' in result['next_step']
    assert 'without Docker' in result['local_diagnostics']
    assert not setup.home().exists()

def test_remote_docker_is_not_automatically_modified(monkeypatch):
    monkeypatch.setattr(setup.shutil, 'which', lambda _: '/usr/bin/docker')
    monkeypatch.setattr(setup, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='linux\n'))
    monkeypatch.setenv('DOCKER_HOST', 'ssh://production.example')
    with pytest.raises(setup.SetupError, match='remote Docker'):
        setup.start()
    assert not setup.home().exists()

def test_unavailable_engine_does_not_create_state(monkeypatch):
    monkeypatch.setattr(setup.shutil, 'which', lambda _: '/usr/bin/docker')
    monkeypatch.setattr(setup, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout=''))
    with pytest.raises(setup.SetupError, match='engine is unavailable'):
        setup.start()
    assert not setup.home().exists()

def test_existing_connection_is_never_replaced(monkeypatch):
    bridge.save_config({'url': 'https://existing.example/mcp', 'api_key': 'private-test-key'})
    original = bridge.config_path().read_bytes()
    monkeypatch.setattr(setup, 'docker_ready', lambda: None)
    monkeypatch.setattr(setup, 'ensure_central', lambda _: pytest.fail('must not provision'))
    with pytest.raises(setup.SetupError, match='not replaced'):
        setup.start()
    assert bridge.config_path().read_bytes() == original
    assert not (setup.home() / 'state.json').exists()

def test_state_secrets_are_protected_and_launch_file_is_removed():
    with setup.setup_lock():
        state = setup.new_state()
        setup.private_write(setup.home() / 'state.json', state)
        env = setup.environment(state)
        assert len(env['CENTRAL_ADMIN_PASSWORD']) >= 32
        assert '!' in env['CENTRAL_ADMIN_PASSWORD']
        assert any(c.isupper() for c in env['CENTRAL_ADMIN_PASSWORD'])
        assert any(c.islower() for c in env['CENTRAL_ADMIN_PASSWORD'])
        assert any(c.isdigit() for c in env['CENTRAL_ADMIN_PASSWORD'])
        if os.name == 'nt':
            assert env['CENTRAL_ADMIN_PASSWORD'] not in (setup.home() / 'state.json').read_text()
        else:
            assert (setup.home() / 'state.json').stat().st_mode & 0o777 == 0o600
        with setup.env_file(env) as filename:
            assert Path(filename).exists()
        assert not Path(filename).exists()
        with pytest.raises(setup.SetupError, match='Another'):
            with setup.setup_lock():
                pass

def test_stop_refuses_an_unowned_container(monkeypatch):
    state = setup.new_state()
    setup.private_write(setup.home() / 'state.json', state)
    monkeypatch.setattr(setup, 'docker_ready', lambda: None)
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv)
        assert argv[1:3] == ['container', 'inspect']
        return SimpleNamespace(returncode=0, stdout=json.dumps([{'Config': {'Labels': {}}, 'State': {'Running': True}}]))
    monkeypatch.setattr(setup, 'run', fake_run)
    with pytest.raises(setup.SetupError, match='another owner'):
        setup.stop()
    assert len(calls) == 1

def test_monitor_refuses_another_host_before_authentication(monkeypatch):
    setup.private_write(setup.home() / 'state.json', setup.new_state())
    monkeypatch.setattr(setup, 'docker_ready', lambda: None)
    monkeypatch.setattr(setup, 'admin_client', lambda _: pytest.fail('must not authenticate for another host'))
    with pytest.raises(setup.SetupError, match='other hosts were not modified'):
        setup.monitor('unrelated-host:production-database')
