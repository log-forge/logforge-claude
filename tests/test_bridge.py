import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
import logforge_mcp as bridge

@pytest.fixture
def server():
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, message, dict(self.headers)))
            if self.path == "/redirect":
                self.send_response(307); self.send_header("Location", "/secret-destination"); self.end_headers(); return
            if self.path == "/unauthorized":
                self.send_response(401); self.end_headers(); self.wfile.write(b"token-that-must-not-leak"); return
            if self.path == "/malformed":
                self.send_response(200); self.end_headers(); self.wfile.write(b"<html>wrong endpoint</html>"); return
            if "id" not in message:
                self.send_response(202); self.end_headers(); return
            result = {"tools": [{"name": "logforge_describe_stack", "inputSchema": {"type": "object"}}]} if message["method"] == "tools/list" else {"ok": True}
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(bridge.rpc_result(message["id"], result)).encode())
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
    yield f"http://127.0.0.1:{http.server_port}", received
    http.shutdown(); http.server_close(); thread.join()

def test_auth_forwarding_notifications_and_tool_merge(server):
    url, received = server
    gateway = bridge.Gateway({"url": url, "api_key": "test-credential"})
    notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert bridge.dispatch(gateway, notification) is None
    reply = bridge.dispatch(gateway, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    names = {t["name"] for t in reply["result"]["tools"]}
    assert names == {"logforge_describe_stack", "logforge_runs_list", "logforge_run_read"}
    assert received[0][2]["Authorization"] == "Bearer test-credential"

def test_redirects_never_forward_credentials(server):
    url, received = server
    gateway = bridge.Gateway({"url": url + "/redirect", "api_key": "private"})
    with pytest.raises(bridge.BridgeError, match="redirected"):
        gateway.call({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert len(received) == 1

@pytest.mark.parametrize("path,expected", [("/unauthorized", "rejected"), ("/malformed", "invalid JSON")])
def test_upstream_errors_do_not_expose_raw_bodies(server, path, expected):
    url, _ = server
    gateway = bridge.Gateway({"url": url + path, "api_key": "private"})
    with pytest.raises(bridge.BridgeError, match=expected) as error:
        gateway.call({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "token-that-must-not-leak" not in str(error.value)

def test_url_and_environment_pairing(monkeypatch):
    for url in ("http://example.com/mcp", "https://key@example.com/mcp", "https://example.com/mcp?key=secret"):
        with pytest.raises(bridge.BridgeError): bridge.validate_url(url)
    monkeypatch.setenv("LOGFORGE_MCP_URL", "https://example.com/mcp")
    monkeypatch.delenv("LOGFORGE_MCP_API_KEY", raising=False)
    with pytest.raises(bridge.BridgeError, match="together"): bridge.load_config()

def test_credential_storage_roundtrip(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    bridge.save_config({"url": "https://example.com/mcp", "api_key": "a-private-test-secret"}, path)
    monkeypatch.setenv("LOGFORGE_MCP_CONFIG", str(path))
    monkeypatch.delenv("LOGFORGE_MCP_URL", raising=False)
    monkeypatch.delenv("LOGFORGE_MCP_API_KEY", raising=False)
    assert bridge.load_config()["api_key"] == "a-private-test-secret"
    if os.name == "nt": assert "a-private-test-secret" not in path.read_text()
    else: assert path.stat().st_mode & 0o777 == 0o600

def test_report_reading_refuses_path_traversal(tmp_path):
    with pytest.raises(bridge.BridgeError, match="run_id"):
        bridge.inspect_run("logforge_run_read", {"project_path": str(tmp_path), "run_id": "../../secret"})


def test_first_run_local_tools_without_appliance(tmp_path, monkeypatch):
    monkeypatch.setenv("LOGFORGE_MCP_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.delenv("LOGFORGE_MCP_URL", raising=False)
    monkeypatch.delenv("LOGFORGE_MCP_API_KEY", raising=False)
    gateway = bridge.runtime_gateway()
    initialized = bridge.dispatch(gateway, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert initialized["result"]["serverInfo"]["name"] == "logforge-local-diagnostics"
    listed = bridge.dispatch(gateway, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert {t["name"] for t in listed["result"]["tools"]} == {"logforge_runs_list", "logforge_run_read"}
    result = bridge.dispatch(gateway, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "logforge_runs_list", "arguments": {"project_path": str(tmp_path)}}})
    assert result["result"]["structuredContent"] == {"runs": []}
    assert not (tmp_path / "missing.json").exists()
    monkeypatch.setenv("LOGFORGE_MCP_URL", "https://example.com/mcp")
    with pytest.raises(bridge.BridgeError, match="together"):
        bridge.runtime_gateway()
