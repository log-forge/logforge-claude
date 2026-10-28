from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
import logforge_usage as usage
import logforge_mcp as bridge


@pytest.fixture(autouse=True)
def isolated_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("LOGFORGE_USAGE_STATE", str(tmp_path / "usage.sqlite3"))
    monkeypatch.delenv("LOGFORGE_USAGE_DISABLED", raising=False)


@pytest.fixture
def receiver():
    requests = []
    mode = {"status": 204}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"]))), self.headers["Authorization"]))
            self.send_response(mode["status"])
            if mode["status"] == 307:
                self.send_header("Location", "/credential-leak")
            self.end_headers()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1/plugin-reports", requests, mode
    server.shutdown()
    server.server_close()
    thread.join()


def test_opt_out_before_first_record_means_no_identity_or_network(receiver):
    _, requests, _ = receiver
    assert usage.status()["enabled"] is True
    assert not usage.state_path().exists()  # status does not generate a reporting identity.
    usage.disable()
    assert not usage.record({"bridge.started": 1})
    assert usage.flush(force=True)["status"] == "disabled"
    with usage.database() as conn:
        assert usage.settings(conn) == {"enabled": False, "preference": "disabled", "notice_version": 2}
    assert not requests


def test_fresh_default_on_first_valid_record_uses_isolated_claude_labels():
    assert usage.status()["preference"] == "default"
    assert usage.record({"run.passed": 1}, tool="logforge_run_read", duration_ms=3)
    current = usage.status(preview=True)
    assert current["enabled"] and current["preference"] == "default"
    payload = current["reports"][0]
    assert payload["product"] == "logforge-claude-plugin"
    assert payload["distribution"] == "claude-directory"
    assert payload["plugin_version"] == usage.VERSION
    assert payload["tools"]["logforge_run_read"]["calls"] == 1


def test_saved_disable_survives_upgrade_and_future_records(monkeypatch):
    usage.disable()
    monkeypatch.setattr(usage, "VERSION", "99.0.0")
    assert not usage.record({"run.passed": 1})
    assert usage.status()["enabled"] is False
    assert usage.status(preview=True)["reports"] == []
    # Also preserve a disable saved by 0.1.0 before preference metadata existed.
    with usage.database(create=True) as conn:
        usage.save_settings(conn, {"enabled": False, "consent_version": 1})
    assert not usage.record({"run.passed": 1})
    assert usage.status()["enabled"] is False


def test_cli_disable_persists_in_a_fresh_process():
    disabled = subprocess.run([sys.executable, usage.__file__, "disable"], capture_output=True, text=True, check=True)
    assert json.loads(disabled.stdout)["enabled"] is False
    code = ("import sys;sys.path.insert(0,sys.argv[1]);import logforge_usage as u;"
            "assert not u.record({'run.passed':1});assert not u.status()['enabled'];"
            "assert u.flush(force=True)['status']=='disabled';print('saved opt-out retained')")
    checked = subprocess.run([sys.executable, "-c", code, str(Path(usage.__file__).parent)],
                             capture_output=True, text=True, check=True)
    assert checked.stdout.strip() == "saved opt-out retained"


def test_environment_opt_out_before_first_use_has_no_identity(monkeypatch):
    monkeypatch.setenv("LOGFORGE_USAGE_DISABLED", "1")
    assert not usage.status()["enabled"]
    assert not usage.record({"run.passed": 1})
    assert usage.flush(force=True)["status"] == "disabled"
    assert not usage.state_path().exists()


@pytest.mark.parametrize("arguments", [
    {"events": {"prompt": "private prompt"}}, {"events": ["run.passed"]},
    {"events": {"run.passed": True}}, {"events": {"run.passed": -1}},
    {"events": {"run.passed": 1}, "tool": {"token": "secret"}},
    {"events": {"run.passed": 1}, "duration_ms": float("nan")},
    {"events": {"run.passed": 1}, "duration_ms": float("inf")},
    {"events": {"run.passed": 1}, "error": "private error"},
])
def test_malformed_or_sensitive_records_rejected_before_identity_creation(arguments):
    assert not usage.record(**arguments)
    assert not usage.state_path().exists()


def test_retry_revisions_opt_out_and_destination_isolation(receiver):
    endpoint, requests, mode = receiver
    usage.enable(endpoint, "development")
    assert usage.status()["first_activation"] is None
    usage.record({"run.passed": 1}, tool="logforge_run_read", duration_ms=100)
    identity = usage.status()["instance_id"]
    mode["status"] = 503
    assert usage.flush(force=True)["status"] == "retry_pending"
    mode["status"] = 204
    assert usage.flush(force=True)["sent"] == 1
    assert requests[0][1] == requests[1][1]
    assert usage.flush(force=True)["sent"] == 0
    usage.record(tool="logforge_run_read", error=True, duration_ms=10_000)
    assert usage.flush(force=True)["sent"] == 1
    stats = requests[-1][1]["tools"]["logforge_run_read"]
    assert stats["calls"] == 2 and stats["errors"] == 1
    assert stats["latency"]["under_1s"] == 1 and stats["latency"]["10s_or_more"] == 1
    assert requests[-1][1]["revision"] == 2
    usage.record({"run.failed": 1})
    usage.disable()
    assert usage.status()["pending_reports"] == 0
    assert not usage.record({"run.passed": 1})
    assert usage.flush(force=True)["status"] == "disabled"
    usage.enable(endpoint, "development")
    assert usage.status()["instance_id"] != identity
    usage.record({"bridge.started": 1})
    before = usage.status()["instance_id"]
    usage.enable(endpoint + "/new", "development")
    assert usage.status()["instance_id"] != before
    assert usage.status()["pending_reports"] == 0


def test_allowlist_redaction_and_redirect_refusal(receiver):
    endpoint, requests, mode = receiver
    usage.enable(endpoint, "development")
    assert not usage.record({"private-feature-name": 1})
    assert not usage.record({"outputs": True})
    usage.record(tool="private-tool-name", duration_ms=12)
    usage.record_run({"status": "passed", "summary": {"outputs": 1, "checks": 1, "failed_checks": [], "filtered": 4, "unreached": 0},
                      "events": [{"source": {"file": "/private-project/private-file.ts", "snippet": "private code"}, "tags": ["private-tag"], "data": {"token": "private-token"}}]})
    preview = json.dumps(usage.status(preview=True))
    assert "private-" not in preview and "private code" not in preview
    assert '"language.typescript": 1' in preview
    assert '"other"' in preview
    mode["status"] = 307
    assert usage.flush(force=True)["status"] == "retry_pending"
    assert len(requests) == 1 and requests[0][0] == "/v1/plugin-reports"
    assert requests[0][2].startswith("Bearer ")
    assert requests[0][2][7:] not in preview


def test_concurrent_writers_and_upgrade_preserve_totals(receiver, monkeypatch):
    usage.enable(receiver[0], "development")
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(lambda _: usage.record(tool="logforge_logs_query", duration_ms=1), range(80)))
    original = usage.status(preview=True)
    assert original["reports"][0]["tools"]["logforge_logs_query"]["calls"] == 80
    next_version = str(int(usage.VERSION.split('.')[0]) + 1) + '.0.0'
    monkeypatch.setattr(usage, "VERSION", next_version)
    assert usage.record(tool="logforge_logs_query", duration_ms=2)
    upgraded = usage.status(preview=True)
    assert upgraded["instance_id"] == original["instance_id"]
    assert len(upgraded["reports"]) == 2
    assert sum(r["tools"]["logforge_logs_query"]["calls"] for r in upgraded["reports"]) == 81


@pytest.mark.parametrize("attempt", range(3))
def test_concurrent_default_first_use_preserves_identity_and_counts(attempt):
    assert not usage.state_path().exists()
    first_use = threading.Barrier(16, timeout=10)

    def record_first_use(_):
        first_use.wait()
        return usage.record({"outputs": 1}, tool="logforge_logs_query", duration_ms=1)

    with ThreadPoolExecutor(max_workers=16) as pool:
        accepted = list(pool.map(record_first_use, range(16)))
    assert all(accepted), f"Only {sum(accepted)}/16 first-use records accepted"
    state = usage.status(preview=True)
    assert len(state["reports"]) == 1
    payload = state["reports"][0]
    assert payload["instance_id"] == state["instance_id"]
    assert payload["events"]["outputs"] == 16
    assert payload["tools"]["logforge_logs_query"]["calls"] == 16
    assert payload["revision"] == 16


def test_bad_telemetry_state_cannot_break_mcp(monkeypatch):
    usage.state_path().write_text("not sqlite")
    assert not usage.record(tool="logforge_logs_query")
    assert usage.flush(force=True)["status"] == "retry_pending"
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO('[1,2]\n{"jsonrpc":"2.0","id":4,"method":"ping"}\n'))
    out = io.StringIO()
    monkeypatch.setattr(bridge.sys, "stdout", out)
    bridge.serve_requests(None)
    responses = list(map(json.loads, out.getvalue().splitlines()))
    assert responses[0]["error"]["code"] == -32600
    assert responses[1] == {"jsonrpc": "2.0", "id": 4, "result": {}}


def test_rpc_counts_both_error_forms_without_arguments(receiver):
    usage.enable(receiver[0], "development")
    request = {"method": "tools/call", "params": {"name": "logforge_file_read", "arguments": {"path": "secret-file"}}}
    usage.record_rpc(request, {"error": {"message": "secret-error"}}, 1)
    usage.record_rpc(request, {"result": {"isError": True, "content": "secret-log"}}, 2)
    usage.record_rpc(request, {"result": {}}, 3)
    usage.record_rpc({"method": "initialize"}, {"result": {}}, 4)
    payload = usage.status(preview=True)["reports"][0]
    assert payload["tools"]["logforge_file_read"]["errors"] == 2
    assert payload["tools"]["logforge_file_read"]["calls"] == 3
    assert payload["events"]["connection.succeeded"] == 1
    assert "secret" not in json.dumps(payload)
