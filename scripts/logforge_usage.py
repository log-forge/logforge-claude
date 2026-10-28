"""Default-on publisher usage aggregates with a persistent off switch. No application payloads or tags."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from uuid import uuid4

VERSION = "0.2.0"
DEFAULT_ENDPOINT = "https://usage.logforge.dev/v1/plugin-reports"
EVENTS = frozenset({"bridge.started", "connection.succeeded", "connection.failed", "run.passed", "run.failed",
                    "language.javascript", "language.typescript", "language.python", "outputs", "checks",
                    "checks_failed", "filtered", "unreached"})
TOOLS = frozenset({"logforge_runs_list", "logforge_run_read", "logforge_describe_stack", "logforge_inventory_search",
                   "logforge_logs_query", "logforge_metrics_query", "logforge_alerts_list", "logforge_alerts_get",
                   "logforge_actions_preview", "logforge_actions_execute", "logforge_graph_get", "logforge_alert_history_search",
                   "logforge_metrics_query_range", "logforge_file_list", "logforge_file_read"})
BUCKETS = ("under_100ms", "under_1s", "under_10s", "10s_or_more")


def state_path():
    if os.environ.get("LOGFORGE_USAGE_STATE"):
        return Path(os.environ["LOGFORGE_USAGE_STATE"]).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "LogForge/claude-plugin-usage.sqlite3"
    return Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local/state")) / "logforge/claude-plugin-usage.sqlite3"


@contextmanager
def database(create=False):
    path = state_path()
    if not create and not path.is_file():
        yield None
        return
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            path.parent.chmod(0o700)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    conn = sqlite3.connect(path, timeout=1)
    try:
        if create:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reports(day TEXT NOT NULL, version TEXT NOT NULL, distribution TEXT NOT NULL,
              environment TEXT NOT NULL, revision INTEGER NOT NULL, sent INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL,
              PRIMARY KEY(day,version,distribution,environment));""")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def settings(conn):
    row = conn.execute("SELECT value FROM settings WHERE id=1").fetchone()
    return json.loads(row[0]) if row else {}


def save_settings(conn, config):
    conn.execute("INSERT OR REPLACE INTO settings VALUES(1,?)", (json.dumps(config),))


def today():
    return datetime.now(timezone.utc).date().isoformat()


def environment_disabled():
    return os.environ.get("LOGFORGE_USAGE_DISABLED", "").strip().lower() in ("1", "true", "yes", "on")


def fresh_settings(endpoint, environment, distribution, preference):
    from logforge_mcp import protect_secret
    token = secrets.token_hex(32)
    return {"enabled": True, "instance_id": str(uuid4()), "first_activation": None,
            "token_dpapi" if os.name == "nt" else "token": protect_secret(token) if os.name == "nt" else token,
            "endpoint": endpoint, "environment": environment, "distribution": distribution,
            "preference": preference, "notice_version": 2, "next_attempt": 0}


def build_distribution():
    try:
        value = json.loads((Path(__file__).resolve().parents[1] / "distribution.json").read_text(encoding="utf-8"))["channel"]
        return value if value in ("local", "claude-directory") else "local"
    except (OSError, ValueError, KeyError, TypeError):
        return "local"


def enable(endpoint=DEFAULT_ENDPOINT, environment="production", distribution=None):
    from logforge_mcp import validate_url
    distribution = distribution or build_distribution()
    validate_url(endpoint)
    if environment not in ("development", "production") or distribution not in ("local", "claude-directory"):
        raise ValueError("Invalid reporting environment or distribution")
    if environment == "production" and endpoint != DEFAULT_ENDPOINT:
        raise ValueError("Use development mode for a custom usage endpoint")
    with database(create=True) as conn:
        config = settings(conn)
        # Changing destination gets a new identity: never forward old data or tokens to a new host.
        if not config or not config.get("enabled") or config.get("endpoint") != endpoint:
            config = fresh_settings(endpoint, environment, distribution, "enabled")
            conn.execute("DELETE FROM reports")
        config.update(enabled=True, endpoint=endpoint, environment=environment, distribution=distribution,
                      preference="enabled", notice_version=2, next_attempt=0)
        save_settings(conn, config)
    return status()


def disable():
    # Persist the choice even before first use, without generating an identity/token.
    with database(create=True) as conn:
        config = settings(conn)
        config.update(enabled=False, preference="disabled", notice_version=2)
        save_settings(conn, config)
        conn.execute("DELETE FROM reports")
    return status()


def status(preview=False):
    with database() as conn:
        config = settings(conn) if conn is not None else {}
        if not config:
            config = {"enabled": True, "preference": "default", "notice_version": 2,
                      "endpoint": DEFAULT_ENDPOINT, "environment": "production", "distribution": build_distribution()}
        rows = conn.execute("SELECT payload FROM reports WHERE revision>sent ORDER BY day").fetchall() if conn is not None else []
        result = {k: config.get(k) for k in ("enabled", "endpoint", "environment", "distribution", "instance_id", "first_activation", "last_delivery", "preference", "notice_version")}
        result["enabled"] = config.get("enabled") is True and not environment_disabled()
        result["environment_disabled"] = environment_disabled()
        result["reporting_notice"] = ("Publisher aggregate reporting defaults to ON. Current setting: "
            + ("ON" if result["enabled"] else "OFF")
            + ". Disable persistently with: python scripts/logforge_usage.py disable. See docs/usage-privacy.md.")
        result["pending_reports"] = len(rows)
        if preview:
            result["reports"] = [json.loads(r[0]) for r in rows]
        return result


def record(events=None, tool=None, error=False, duration_ms=0):
    """Best effort local transaction only. Never network I/O in the diagnostic hot path."""
    try:
        events = {} if events is None else events
        if (not isinstance(events, dict) or set(events) - EVENTS
                or any(type(v) is not int or not 0 <= v <= 1_000_000_000 for v in events.values())
                or (tool is not None and not isinstance(tool, str)) or type(error) is not bool
                or type(duration_ms) not in (int, float) or not math.isfinite(duration_ms) or duration_ms < 0
                or (not events and tool is None) or environment_disabled()):
            return False
        with database(create=True) as conn:
            config = settings(conn)
            if not config:
                config = fresh_settings(DEFAULT_ENDPOINT, "production", build_distribution(), "default")
                save_settings(conn, config)
            if config.get("enabled") is not True:
                return False
            if not config.get("notice_shown"):
                print("LogForge publisher aggregate reporting defaults to ON (counts only; no source, prompts, logs or tags). "
                      "Disable: python <plugin-root>/scripts/logforge_usage.py disable. See docs/usage-privacy.md.", file=sys.stderr)
                config.update(notice_shown=True, notice_version=2)
                save_settings(conn, config)
            day = today()
            if config.get("first_activation") is None:
                config["first_activation"] = day
                save_settings(conn, config)
            keys = (day, VERSION, config["distribution"], config["environment"])
            old = conn.execute("SELECT payload,revision,sent FROM reports WHERE day=? AND version=? AND distribution=? AND environment=?", keys).fetchone()
            platform = {"win32": "windows", "linux": "linux", "darwin": "macos"}.get(sys.platform, "other")
            payload = json.loads(old[0]) if old else {"schema_version": 1, "product": "logforge-claude-plugin",
                "instance_id": config["instance_id"], "first_activation": config["first_activation"], "day": day,
                "plugin_version": VERSION, "distribution": config["distribution"], "environment": config["environment"],
                "platform": platform, "events": {}, "tools": {}}
            for name, count in events.items():
                payload["events"][name] = min(1_000_000_000, payload["events"].get(name, 0) + count)
            if tool is not None:
                name = tool if tool in TOOLS else "other"
                values = payload["tools"].setdefault(name, {"calls": 0, "errors": 0, "duration_ms": 0, "latency": dict.fromkeys(BUCKETS, 0)})
                if values["calls"] < 1_000_000:
                    duration = max(0, min(3_600_000, int(duration_ms)))
                    bucket = BUCKETS[0 if duration < 100 else 1 if duration < 1000 else 2 if duration < 10_000 else 3]
                    values["calls"] += 1
                    values["errors"] += int(bool(error))
                    values["duration_ms"] += duration
                    values["latency"][bucket] += 1
            payload["revision"] = old[1] + 1 if old else 1
            conn.execute("INSERT OR REPLACE INTO reports VALUES(?,?,?,?,?,?,?)", (*keys, payload["revision"], old[2] if old else 0, json.dumps(payload)))
            cutoff = (datetime.now(timezone.utc).date() - timedelta(days=8)).isoformat()
            conn.execute("DELETE FROM reports WHERE day < ?", (cutoff,))
            conn.execute("DELETE FROM reports WHERE rowid IN (SELECT rowid FROM reports ORDER BY day DESC,revision DESC LIMIT -1 OFFSET 100)")
        return True
    except Exception:
        return False  # Usage failure must never change business or MCP results.


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def flush(force=False):
    """Send cumulative snapshots; acknowledged revisions survive retries without double-counting."""
    try:
        if environment_disabled():
            return {"sent": 0, "status": "disabled"}
        with database() as conn:
            if conn is None:
                return {"sent": 0, "status": "no_activity"}
            config = settings(conn)
            if config.get("enabled") is not True:
                return {"sent": 0, "status": "disabled"}
            if not force and config.get("next_attempt", 0) > time.time():
                return {"sent": 0, "status": "scheduled"}
            config["next_attempt"] = time.time() + 60
            save_settings(conn, config)
            cutoff = (datetime.now(timezone.utc).date() - timedelta(days=8)).isoformat()
            conn.execute("DELETE FROM reports WHERE day < ?", (cutoff,))
            rows = conn.execute("SELECT day,version,distribution,environment,revision,payload FROM reports WHERE revision>sent ORDER BY day LIMIT 9").fetchall()
        from logforge_mcp import protect_secret, validate_url
        endpoint = validate_url(config["endpoint"])
        token = protect_secret(config["token_dpapi"], decrypt=True) if "token_dpapi" in config else config["token"]
        opener, sent = urllib.request.build_opener(NoRedirect()), 0
        for row in rows:
            with database() as conn:
                current = settings(conn)
                if environment_disabled() or current.get("enabled") is not True or current.get("instance_id") != config["instance_id"]:
                    break
            req = urllib.request.Request(endpoint, data=row[5].encode(), headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method="POST")
            with opener.open(req, timeout=2) as response:
                if response.status != 204:
                    raise ValueError("Unexpected receiver response")
            with database() as conn:
                current = settings(conn)
                if current.get("enabled") and current.get("instance_id") == config["instance_id"]:
                    conn.execute("UPDATE reports SET sent=MAX(sent,?) WHERE day=? AND version=? AND distribution=? AND environment=?", (row[4], *row[:4]))
                    current["last_delivery"] = "delivered"
                    save_settings(conn, current)
            sent += 1
        return {"sent": sent, "status": "delivered"}
    except Exception:
        try:
            with database() as conn:
                if conn is not None:
                    config = settings(conn)
                    config.update(next_attempt=time.time() + 300, last_delivery="retry_pending")
                    save_settings(conn, config)
        except Exception:
            pass
        return {"sent": 0, "status": "retry_pending"}


def kick():
    """One detached, quiet, throttled uploader after a short CLI process exits."""
    try:
        if not status().get("enabled"):
            return
        options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS} if os.name == "nt" else {"start_new_session": True}
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "flush"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **options)
    except Exception:
        pass


def start_worker():
    stop = threading.Event()
    def worker():
        while not stop.wait(60):
            flush()
    threading.Thread(target=worker, daemon=True, name="logforge-usage").start()
    kick()
    return stop


def record_run(report):
    try:
        summary = report["summary"]
        events = {"run." + report["status"]: 1, "outputs": summary["outputs"], "checks": summary["checks"],
                  "checks_failed": len(summary["failed_checks"]), "filtered": summary["filtered"], "unreached": summary["unreached"]}
        languages = set()
        for event in report["events"]:
            source = event.get("source") or {}
            suffix = Path(source.get("file", "")).suffix.lower()
            lang = "typescript" if suffix in (".ts", ".tsx") else "python" if suffix == ".py" else "javascript" if suffix in (".js", ".mjs", ".cjs", ".jsx") else None
            if lang:
                languages.add(lang)
        events.update({"language." + lang: 1 for lang in languages})
        record(events)
    except Exception:
        pass


def record_rpc(payload, response, duration_ms):
    try:
        if not isinstance(payload, dict) or not isinstance(response, dict):
            return
        result = response.get("result")
        failed = bool(response.get("error") or (isinstance(result, dict) and result.get("isError")))
        if payload.get("method") == "tools/call":
            record(tool=payload.get("params", {}).get("name", "other"), error=failed, duration_ms=duration_ms)
        elif payload.get("method") == "initialize":
            record({"connection.failed" if failed else "connection.succeeded": 1})
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "preview", "disable", "flush"):
        sub.add_parser(name)
    sub.choices["flush"].add_argument("--force", action="store_true")
    opt = sub.add_parser("enable")
    opt.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    opt.add_argument("--environment", choices=["development", "production"], default="production")
    opt.add_argument("--distribution", choices=["local", "claude-directory"], help="Override the packaged distribution label; this is not an installation receipt")
    opt.add_argument("--consent", action="store_true", help="Backward-compatible acknowledgment of docs/usage-privacy.md; reporting is now on by default")
    args = parser.parse_args()
    try:
        result = enable(args.endpoint, args.environment, args.distribution) if args.command == "enable" else disable() if args.command == "disable" else flush(args.force) if args.command == "flush" else status(args.command == "preview")
        print(json.dumps(result, indent=2))
        return 1 if result.get("status") == "retry_pending" else 0
    except Exception:
        print("Could not update usage reporting. Check the endpoint and local state permissions.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
