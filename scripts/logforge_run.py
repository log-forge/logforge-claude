"""Run a .logforge project plan and produce a JSON diagnostic report."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

MAX_EVENTS = 5000
MAX_BYTES = 6 * 1024 * 1024
LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
ROOT = Path(__file__).resolve().parents[1]

def now():
    return datetime.now(timezone.utc).isoformat()

def tags(values):
    return list(dict.fromkeys(t.strip().lower() for t in values if t.strip()))

def matches(event_tags, selected):
    return not selected or "*" in selected or bool(set(tags(event_tags)) & set(selected))

def load_plan(path):
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("kind") != "project" or plan.get("schema_version") != "1.0":
        raise ValueError("Expected a .logforge project document with schema_version 1.0")
    if not isinstance(plan.get("commands"), list) or not plan["commands"]:
        raise ValueError("At least one explicit command is required")
    for spec in [*plan["commands"], *plan.get("services", [])]:
        if not isinstance(spec.get("argv"), list) or not spec["argv"] or not all(isinstance(a, str) for a in spec["argv"]):
            raise ValueError("Commands must be nonempty argv arrays; shell strings are unsupported")
        cwd = (path.parent / spec.get("cwd", ".")).resolve()
        if not cwd.is_relative_to(path.parent.resolve()):
            raise ValueError("Command working directories must remain within the project")
        timeout = spec.get("timeout_seconds", 60)
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
            raise ValueError("Command timeouts must be between 0 and 3600 seconds")
    for expected in plan.get("expect", []):
        if not isinstance(expected.get("name"), str) or not isinstance(expected.get("tags", []), list):
            raise ValueError("Each expectation needs a name and optional tag list")
        if expected.get("kind", "output") not in ("output", "check") or not isinstance(expected.get("min_count", 1), int) or expected.get("min_count", 1) < 1:
            raise ValueError("Expected kind must be output/check and min_count must be positive")
        if expected.get("level", "debug") not in LEVELS:
            raise ValueError("Expected level must be debug/info/warn/error")
    return plan

class Capture:
    def __init__(self, run_id, selected):
        self.run_id, self.selected = run_id, selected
        self.events, self.summaries, self.seen = [], {}, set()
        self.bytes = self.dropped = self.rejected = 0
        self.lock = threading.Lock()

    def accept(self, data, producer):
        if not isinstance(data, dict) or data.get("_logforge") not in ("event", "summary"):
            return False
        if data.get("run_id") != self.run_id or data.get("schema_version") != "1.0":
            self.rejected += 1
            return False
        encoded_size = len(json.dumps(data).encode("utf-8"))
        if encoded_size > 65536:
            self.dropped += 1
            return False
        with self.lock:
            if data["_logforge"] == "summary":
                stats = data.get("stats")
                if not isinstance(stats, dict) or not all(isinstance(stats.get(key), int) and stats[key] >= 0 for key in ("emitted", "filtered", "errors")):
                    self.rejected += 1
                    return False
                self.summaries[(producer, data.get("service", "application"))] = data
                return True
            if not isinstance(data.get("tags"), list) or not all(isinstance(t, str) for t in data["tags"]):
                self.rejected += 1
                return False
            if not isinstance(data.get("event_id"), str) or not isinstance(data.get("name"), str) or data.get("kind") not in ("output", "check"):
                self.rejected += 1
                return False
            if not matches(data["tags"], self.selected):
                self.rejected += 1
                return False
            if data["event_id"] in self.seen:
                return True
            if len(self.events) >= MAX_EVENTS or self.bytes + encoded_size > MAX_BYTES:
                self.dropped += 1
                return False
            self.events.append(data)
            self.seen.add(data["event_id"])
            self.bytes += encoded_size
            return True

    def consume(self, pipe, producer):
        # Bound individual lines even when a program writes an enormous non-log line.
        while line := pipe.readline(262145):
            if len(line) > 262144:
                self.dropped += 1
                while line and not line.endswith("\n"):
                    line = pipe.readline(262145)
                continue
            try:
                self.accept(json.loads(line), producer)
            except (ValueError, TypeError):
                continue

def start_collector(capture, token, origins):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def allowed_origin(self):
            origin = self.headers.get("Origin")
            return not origin or origin in origins

        def respond(self, status):
            self.send_response(status)
            if self.headers.get("Origin") in origins:
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Vary", "Origin")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_OPTIONS(self):
            if not self.allowed_origin():
                self.respond(403)
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "null"))
            self.send_header("Access-Control-Allow-Methods", "POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            if self.path != "/events":
                self.respond(404)
                return
            if not self.allowed_origin() or not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.respond(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 65536:
                    self.respond(413)
                    return
                self.connection.settimeout(5)
                data = json.loads(self.rfile.read(length))
                ok = capture.accept(data, "http")
            except (ValueError, OSError, TypeError):
                self.respond(400)
                return
            self.respond(204 if ok else 422)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread

def source_context(source, project, source_roots=None):
    if not isinstance(source, dict) or not isinstance(source.get("file"), str):
        return source
    result = dict(source)
    value = source["file"]
    # Browser locations are URLs. They remain links, never local file read targets.
    if value.startswith(("http://", "https://", "webpack:")):
        for mapping in source_roots or []:
            prefix = mapping["url_prefix"]
            if value.startswith(prefix):
                value = str(Path(mapping.get("path", ".")) / urllib.parse.unquote(urllib.parse.urlsplit(value[len(prefix):]).path))
                break
        else:
            return result
    path = (project / value).resolve()
    if not path.is_relative_to(project) or not path.is_file() or path.stat().st_size > 1024 * 1024:
        return result
    try:
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        line = int(source.get("line", 0))
    except (OSError, ValueError, UnicodeError):
        return result
    if not 1 <= line <= len(lines):
        return result
    result.update(file=path.relative_to(project).as_posix(), sha256=hashlib.sha256(content.encode()).hexdigest(),
                  snippet={"start_line": max(1, line - 2), "lines": lines[max(0, line - 3):line + 2]})
    return result

def stop_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=5)

def launch(spec, project, environment, capture, producer):
    replacements = {"${PYTHON}": sys.executable, "${NODE}": shutil.which("node") or "node", "${PLUGIN_ROOT}": str(ROOT)}
    argv = []
    for original in spec["argv"]:
        arg = original
        for key, value in replacements.items():
            arg = arg.replace(key, value)
        argv.append(arg)
    child_env = {**environment, **spec.get("env", {})}
    # A child cannot accidentally split correlation or replace the selected run tags.
    for key in ("LOGFORGE_RUN_ID", "LOGFORGE_TAGS", "LOGFORGE_LEVEL", "LOGFORGE_PROJECT_ROOT", "LOGFORGE_COLLECTOR_URL", "LOGFORGE_COLLECTOR_TOKEN"):
        child_env[key] = environment[key]
    child_env["LOGFORGE_SERVICE"] = spec.get("name", producer)
    process = subprocess.Popen(argv, cwd=project / spec.get("cwd", "."), env=child_env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                               start_new_session=os.name != "nt", creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
    threads = [threading.Thread(target=capture.consume, args=(pipe, producer), daemon=True) for pipe in (process.stdout, process.stderr)]
    for thread in threads:
        thread.start()
    return process, threads

def wait_ready(process, spec):
    url = spec.get("ready_url")
    if not url:
        raise ValueError("A service requires ready_url so tests cannot race startup")
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Service readiness URLs must use loopback")
    deadline = time.monotonic() + spec.get("timeout_seconds", 30)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Service {spec.get('name', 'unnamed')} exited before readiness")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if 200 <= response.status < 400:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    raise RuntimeError(f"Service {spec.get('name', 'unnamed')} did not become ready")

def run(plan_path, selected, level="debug"):
    plan_path = plan_path.resolve()
    project, plan = plan_path.parent, load_plan(plan_path)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
    started, capture = now(), Capture(run_id, selected)
    token = secrets.token_urlsafe(32)
    collector, collector_thread = start_collector(capture, token, plan.get("browser_origins", []))
    environment = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
                   "PYTHONPATH": str(ROOT / "sdk/python") + os.pathsep + os.getenv("PYTHONPATH", ""),
                   "LOGFORGE_RUN_ID": run_id, "LOGFORGE_TAGS": ",".join(selected), "LOGFORGE_LEVEL": level,
                   "LOGFORGE_PROJECT_ROOT": str(project), "LOGFORGE_COLLECTOR_URL": f"http://127.0.0.1:{collector.server_port}/events",
                   "LOGFORGE_COLLECTOR_TOKEN": token}
    processes, commands, errors, services = [], [], [], []
    try:
        for index, spec in enumerate(plan.get("services", [])):
            process, threads = launch(spec, project, environment, capture, f"service-{index}")
            processes.append((process, threads))
            services.append((process, spec.get("name", f"service-{index}")))
            wait_ready(process, spec)
        for index, spec in enumerate(plan["commands"]):
            begin = time.monotonic()
            process, threads = launch(spec, project, environment, capture, f"command-{index}")
            processes.append((process, threads))
            timed_out = False
            try:
                process.wait(timeout=spec.get("timeout_seconds", 60))
            except subprocess.TimeoutExpired:
                timed_out = True
                stop_process(process)
            for thread in threads:
                thread.join(timeout=3)
            commands.append({"name": spec.get("name", f"command-{index}"), "argv": spec["argv"], "exit_code": process.returncode,
                             "timed_out": timed_out, "duration_ms": round((time.monotonic() - begin) * 1000)})
    except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as error:
        errors.append(f"{type(error).__name__}: {error}")
    finally:
        for process, name in services:
            if process.poll() is not None:
                errors.append(f"Service {name} exited before the run finished (exit {process.returncode})")
        for process, threads in reversed(processes):
            stop_process(process)
            for thread in threads:
                thread.join(timeout=3)
        collector.shutdown()
        collector.server_close()
        collector_thread.join(timeout=3)
    events = capture.events
    if not events:
        errors.append("No selected diagnostic events were observed")
    for event in events:
        event["source"] = source_context(event.get("source"), project, plan.get("source_roots"))
    expectations = []
    for expected in plan.get("expect", []):
        expected_level = expected.get("level", "info" if expected.get("kind") == "check" else "debug")
        selected_expected = matches(expected.get("tags", []), selected) and LEVELS[expected_level] >= LEVELS[level]
        observed = sum(e["name"] == expected["name"] and e["kind"] == expected.get("kind", "output") for e in events)
        state = "filtered" if not selected_expected else "reached" if observed >= expected.get("min_count", 1) else "unreached"
        expectations.append({**expected, "observed_count": observed, "status": state})
    checks = [e for e in events if e["kind"] == "check"]
    failed_checks = [e["name"] for e in checks if e.get("passed") is not True]
    diagnostic_errors = sum(s["stats"]["errors"] for s in capture.summaries.values())
    status = "failed" if errors or capture.dropped or capture.rejected or diagnostic_errors or failed_checks or any(e["status"] == "unreached" for e in expectations) or any(c["exit_code"] != 0 or c["timed_out"] for c in commands) else "passed"
    report = {"schema_version": "1.0", "kind": "run", "run_id": run_id, "project": plan.get("name", project.name),
              "plan": plan_path.name, "started_at": started, "finished_at": now(), "selected_tags": selected, "level": level,
              "status": status, "commands": commands, "expectations": expectations, "events": events,
              "producers": list(capture.summaries.values()), "errors": errors,
              "summary": {"outputs": sum(e["kind"] == "output" for e in events), "checks": len(checks), "failed_checks": failed_checks,
                          "filtered": sum(s.get("stats", {}).get("filtered", 0) for s in capture.summaries.values()),
                          "unreached": sum(e["status"] == "unreached" for e in expectations), "dropped": capture.dropped, "rejected": capture.rejected, "diagnostic_errors": diagnostic_errors},
              "coverage_note": "Coverage is measured against this plan's explicit expectations. Unexecuted code is not automatically tested."}
    directory = project / ".logforge/runs"
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / (run_id + ".logforge")
    output.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    from logforge_usage import record_run, kick
    record_run(report)
    kick()
    return output, report

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "out"])
    parser.add_argument("plan", type=Path)
    parser.add_argument("--tag", action="append", default=[], help="Case-insensitive OR filter; repeat to select multiple tags. Omit to select all.")
    parser.add_argument("--level", choices=["debug", "info", "warn", "error"], default="debug")
    args = parser.parse_args()
    try:
        path, report = run(args.plan, tags(args.tag), args.level)
        print(json.dumps({"report": str(path), "status": report["status"], "selected_tags": report["selected_tags"], "summary": report["summary"]}, indent=2))
        return 0 if report["status"] == "passed" else 1
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
