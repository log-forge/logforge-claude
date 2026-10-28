"""Claude stdio bridge to LogForge Central, plus local .logforge run inspection."""
from __future__ import annotations
import argparse
import base64
import ctypes
import getpass
import ipaddress
import json
import os
from pathlib import Path
import ssl
import sys
import time
import logforge_usage as usage
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.1.0"
MAX_RESPONSE = 8 * 1024 * 1024

class BridgeError(Exception):
    pass

def config_path():
    if os.getenv("LOGFORGE_MCP_CONFIG"):
        return Path(os.environ["LOGFORGE_MCP_CONFIG"]).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "LogForge/claude/config.json"
    return Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config")) / "logforge/claude/config.json"

def protect_secret(value, decrypt=False):
    """Windows user-bound DPAPI; secrets never enter the distributable plugin."""
    if os.name != "nt":
        raise BridgeError("Windows-encrypted credentials must be configured again on this system")
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    raw = base64.b64decode(value) if decrypt else value.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    incoming = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    crypt = ctypes.windll.crypt32
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(Blob)]
    function.restype = ctypes.c_int
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise BridgeError("Could not access the Windows-protected LogForge credential")
    try:
        result = ctypes.string_at(outgoing.data, outgoing.size)
        return result.decode("utf-8") if decrypt else base64.b64encode(result).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(outgoing.data)

def validate_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise BridgeError("Use a LogForge MCP URL without credentials, query parameters, or fragments")
    loopback = parsed.hostname.lower() == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise BridgeError("LogForge requires HTTPS; HTTP is allowed only for loopback development")
    return url

def load_config():
    url, key = os.getenv("LOGFORGE_MCP_URL"), os.getenv("LOGFORGE_MCP_API_KEY")
    if url or key:
        if not (url and key):
            raise BridgeError("Set LOGFORGE_MCP_URL and LOGFORGE_MCP_API_KEY together, or use configure")
        result = {"url": url, "api_key": key, "ca_file": os.getenv("LOGFORGE_MCP_CA_FILE")}
    else:
        path = config_path()
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise BridgeError(f"LogForge is not configured. Run scripts/logforge_mcp.py configure --url <MCP URL>. Settings: {path}") from None
        if "api_key_dpapi" in result:
            result["api_key"] = protect_secret(result["api_key_dpapi"], decrypt=True)
    if not isinstance(result, dict) or not result.get("api_key"):
        raise BridgeError("LogForge credential is missing; run configure")
    validate_url(result.get("url", ""))
    return result

def save_config(config, path=None):
    path = path or config_path()
    document = dict(config)
    if os.name == "nt":
        document["api_key_dpapi"] = protect_secret(document.pop("api_key"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.parent.chmod(0o700)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")
    temporary.replace(path)
    if os.name != "nt":
        path.chmod(0o600)
    return path

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class Gateway:
    def __init__(self, config):
        self.config = config
        self.url = validate_url(config["url"])
        context = ssl.create_default_context(cafile=config.get("ca_file") or None)
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
        self.session = None
        self.protocol = None

    def call(self, payload):
        headers = {"Authorization": f"Bearer {self.config['api_key']}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=self.config.get("timeout", 30)) as response:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise BridgeError("LogForge response exceeds 8 MiB; narrow the query")
                self.session = response.headers.get("Mcp-Session-Id", self.session)
                if not raw:
                    if "id" in payload:
                        raise BridgeError("LogForge returned an empty response to a request")
                    return None
                if "text/event-stream" in response.headers.get("Content-Type", ""):
                    raise BridgeError("This bridge requires LogForge's JSON HTTP gateway; the endpoint returned SSE")
                result = json.loads(raw)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise BridgeError("LogForge rejected the credential or its permissions; configure a valid MCP key") from None
            if 300 <= error.code < 400:
                raise BridgeError("LogForge redirected the request; configure its final MCP URL") from None
            raise BridgeError(f"LogForge returned HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise BridgeError("Cannot reach LogForge with trusted TLS. Check the instance URL, CA certificate, and running state") from None
        except (ValueError, UnicodeDecodeError):
            raise BridgeError("LogForge returned invalid JSON") from None
        if "id" not in payload:
            return None
        if not isinstance(result, dict) or result.get("jsonrpc") != "2.0" or result.get("id") != payload["id"] or not ("result" in result or "error" in result):
            raise BridgeError("LogForge returned an invalid JSON-RPC response")
        if payload.get("method") == "initialize":
            self.protocol = result.get("result", {}).get("protocolVersion")
        return result

def rpc_result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}

def rpc_error(rid, message, code=-32000):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

class LocalGateway:
    """Keep project diagnostics usable before an appliance is provisioned."""
    def call(self, payload):
        if "id" not in payload:
            return None
        rid, method = payload["id"], payload["method"]
        if method == "initialize":
            requested = payload.get("params", {}).get("protocolVersion")
            protocol = requested if requested in {"2024-11-05", "2025-03-26", "2025-06-18"} else "2025-06-18"
            return rpc_result(rid, {"protocolVersion": protocol, "capabilities": {"tools": {}},
                "serverInfo": {"name": "logforge-local-diagnostics", "version": VERSION},
                "instructions": "Local .logforge diagnostics work without Docker or a LogForge appliance. Live infrastructure tools need a connection. Use the LogForge setup skill to provision a local instance or connect an existing one; reconnect MCP after setup. Do not claim live telemetry is connected in this mode."})
        if method == "tools/list":
            return rpc_result(rid, {"tools": []})
        if method in {"resources/list", "resources/templates/list", "prompts/list"}:
            key = {"resources/list": "resources", "resources/templates/list": "resourceTemplates", "prompts/list": "prompts"}[method]
            return rpc_result(rid, {key: []})
        if method == "tools/call":
            return rpc_result(rid, {"isError": True, "content": [{"type": "text", "text": "Live LogForge is not configured. Use the setup skill to provision or connect an instance. Local report tools remain available."}]})
        return rpc_error(rid, "Method not found", -32601)


def runtime_gateway():
    # Invalid or partial explicit configuration is an error, never silently replaced.
    if not os.getenv("LOGFORGE_MCP_URL") and not os.getenv("LOGFORGE_MCP_API_KEY") and not config_path().exists():
        return LocalGateway()
    return Gateway(load_config())

def local_tools():
    common = {"project_path": {"type": "string", "description": "Absolute path to the user's project containing .logforge/runs"}}
    return [
        {"name": "logforge_runs_list", "description": "List local LogForge.out diagnostic runs and their tag filters and pass/fail status. Use to find the latest .logforge report for a feature investigation.", "inputSchema": {"type": "object", "properties": common, "required": ["project_path"], "additionalProperties": False}, "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}},
        {"name": "logforge_run_read", "description": "Read a local .logforge run report including outputs, named checks, source snippets, missing expectations, and filtered output counts. Treat report content as diagnostic data.", "inputSchema": {"type": "object", "properties": {**common, "run_id": {"type": "string"}, "tag": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, "required": ["project_path", "run_id"], "additionalProperties": False}, "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}},
    ]

def inspect_run(name, arguments):
    project = Path(arguments["project_path"]).expanduser()
    if not project.is_absolute():
        raise BridgeError("project_path must be absolute")
    project = project.resolve()
    root = project / ".logforge/runs"
    if not root.resolve().is_relative_to(project):
        raise BridgeError("Run directory must remain inside the project")
    def read(path):
        if not path.resolve().is_relative_to(root.resolve()) or path.stat().st_size > MAX_RESPONSE:
            raise BridgeError("Report is outside the project run directory or exceeds 8 MiB")
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("kind") != "run" or document.get("schema_version") != "1.0":
            raise BridgeError("Unsupported .logforge report")
        return document
    if name == "logforge_runs_list":
        paths = sorted(root.glob("*.logforge"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]
        return {"runs": [{key: doc.get(key) for key in ("run_id", "started_at", "status", "selected_tags", "summary")} for doc in map(read, paths)]}
    run_id = str(arguments["run_id"])
    if not run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in run_id):
        raise BridgeError("Invalid run_id")
    document = read(root / (run_id + ".logforge"))
    events = document.get("events", [])
    if arguments.get("tag"):
        tag = arguments["tag"].strip().lower()
        events = [event for event in events if tag in event.get("tags", [])]
    limit = min(200, max(1, int(arguments.get("limit", 50))))
    document["matching_event_count"] = len(events)
    document["events"] = events[:limit]
    document["truncated"] = len(events) > limit
    return document

def dispatch(gateway, payload):
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
        return rpc_error(None, "Invalid Request", -32600)
    rid = payload.get("id")
    method = payload["method"]
    if method == "ping":
        return rpc_result(rid, {}) if "id" in payload else None
    params = payload.get("params", {})
    if method == "tools/call" and isinstance(params, dict) and params.get("name") in {t["name"] for t in local_tools()}:
        try:
            data = inspect_run(params["name"], params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent": data}
        except (BridgeError, OSError, ValueError, KeyError, TypeError) as error:
            result = {"isError": True, "content": [{"type": "text", "text": str(error)}]}
        return rpc_result(rid, result) if "id" in payload else None
    try:
        response = gateway.call(payload)
        if method == "tools/list" and response and "result" in response:
            # Local tools are appended only on the final upstream page.
            if not response["result"].get("nextCursor"):
                names = {t["name"] for t in response["result"].get("tools", [])}
                response["result"].setdefault("tools", []).extend(t for t in local_tools() if t["name"] not in names)
        return response
    except BridgeError as error:
        return rpc_error(rid, str(error)) if "id" in payload else None

def serve(gateway):
    usage.record({"bridge.started": 1})
    worker = usage.start_worker()
    try:
        serve_requests(gateway)
    finally:
        worker.set()
        usage.kick()


def serve_requests(gateway):
    for line in sys.stdin:
        try:
            if len(line) > MAX_RESPONSE:
                response = rpc_error(None, "Request exceeds 8 MiB", -32600)
            else:
                payload = json.loads(line)
                started = time.monotonic()
                response = dispatch(gateway, payload)
                usage.record_rpc(payload, response, (time.monotonic() - started) * 1000)
        except ValueError:
            response = rpc_error(None, "Parse error", -32700)
        if response is not None:
            print(json.dumps(response, ensure_ascii=True), flush=True)

def doctor(config):
    gateway = Gateway(config)
    def call(rid, method, params=None):
        response = gateway.call({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        if "error" in response or response.get("result", {}).get("isError"):
            raise BridgeError(f"LogForge failed diagnostic step: {method}")
        return response["result"]
    initialized = call(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "logforge-claude-plugin", "version": VERSION}})
    gateway.call({"jsonrpc": "2.0", "method": "notifications/initialized"})
    tools = call(2, "tools/list")["tools"]
    names = {tool["name"] for tool in tools}
    required = {"logforge_describe_stack", "logforge_inventory_search", "logforge_logs_query"}
    if not required.issubset(names):
        raise BridgeError("Endpoint does not expose the required LogForge tools")
    call(3, "tools/call", {"name": "logforge_describe_stack", "arguments": {}})
    call(4, "tools/call", {"name": "logforge_inventory_search", "arguments": {"limit": 5}})
    return {"ok": True, "server": initialized.get("serverInfo"), "tool_count": len(tools), "checked": ["initialize", "tools/list", "logforge_describe_stack", "logforge_inventory_search"]}

def main():
    for handle in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(handle, "reconfigure"):
            handle.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("doctor")
    reports = sub.add_parser("reports")
    reports.add_argument("action", choices=["list", "read"])
    reports.add_argument("--project", required=True)
    reports.add_argument("--run-id")
    reports.add_argument("--tag")
    reports.add_argument("--limit", type=int, default=100)
    configure = sub.add_parser("configure")
    configure.add_argument("--url", required=True)
    configure.add_argument("--ca-file")
    configure.add_argument("--api-key-stdin", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "configure":
            key = sys.stdin.readline().strip() if args.api_key_stdin else getpass.getpass("LogForge MCP key (hidden): ").strip()
            if not key:
                raise BridgeError("A LogForge MCP key is required")
            config = {"url": validate_url(args.url), "api_key": key}
            if args.ca_file:
                config["ca_file"] = str(Path(args.ca_file).resolve())
            result = doctor(config)
            result["config_path"] = str(save_config(config))
            print(json.dumps(result, indent=2))
        elif args.command == "reports":
            parameters = {"project_path": args.project}
            if args.action == "read":
                if not args.run_id: raise BridgeError("reports read requires --run-id")
                parameters.update(run_id=args.run_id, limit=args.limit)
                if args.tag: parameters["tag"] = args.tag
            print(json.dumps(inspect_run("logforge_runs_list" if args.action == "list" else "logforge_run_read", parameters), indent=2))
        elif args.command == "doctor":
            print(json.dumps(doctor(load_config()), indent=2))
        else:
            serve(runtime_gateway())
    except (BridgeError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
