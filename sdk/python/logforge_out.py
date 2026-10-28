"""Structured diagnostics. Tag and level filters run before lazy payloads."""
from __future__ import annotations
import atexit
import copy
import inspect
import json
import logging
import os
from pathlib import Path
import re
import threading
from datetime import datetime, timezone
from uuid import uuid4

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
_SECRET = re.compile(r"password|passwd|secret|token|authorization|cookie|api.?key", re.I)

def _clean(value, seen=None, depth=0):
    if depth > 8:
        return "[depth limited]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, str):
        return value[:8192] + ("[truncated]" if len(value) > 8192 else "")
    seen = set() if seen is None else seen
    if id(value) in seen:
        return "[circular]"
    seen.add(id(value))
    if isinstance(value, dict):
        result = {str(k): "[REDACTED]" if _SECRET.search(str(k)) else _clean(v, seen, depth + 1) for k, v in list(value.items())[:100]}
    elif isinstance(value, (list, tuple, set)):
        result = [_clean(v, seen, depth + 1) for v in list(value)[:100]]
    elif isinstance(value, BaseException):
        result = {"name": type(value).__name__, "message": _clean(str(value))}
    else:
        result = _clean(str(value))
    seen.remove(id(value))
    return result

def _stdout(event):
    print(json.dumps(event, ensure_ascii=True, allow_nan=False), flush=True)

class _Logger:
    def __init__(self):
        self._lock = threading.RLock()
        self.tags = [t.strip().lower() for t in os.getenv("LOGFORGE_TAGS", "").split(",") if t.strip()]
        self.level = os.getenv("LOGFORGE_LEVEL", "debug")
        self.enabled = os.getenv("LOGFORGE_ENABLED", "true") != "false"
        self.run_id = os.getenv("LOGFORGE_RUN_ID")
        self.service = os.getenv("LOGFORGE_SERVICE", "application")
        self.sink = _stdout
        self.reset()

    def configure(self, *, tags=None, level=None, enabled=None, run_id=None, service=None, sink=None):
        with self._lock:
            if level is not None and level not in _LEVELS:
                raise ValueError("Unknown LogForge level")
            if tags is not None and (not isinstance(tags, list) or not all(isinstance(t, str) for t in tags)):
                raise ValueError("tags must be a list of strings")
            for key, value in locals().copy().items():
                if key != "self" and value is not None:
                    setattr(self, key, list(dict.fromkeys(t.strip().lower() for t in value if t.strip())) if key == "tags" else value)
        return self

    def reset(self):
        with self._lock:
            self._stats = {"emitted": 0, "filtered": 0, "errors": 0, "sites": {}}
            self._summary_written = False

    def is_enabled(self, tags, level="debug"):
        tags = [tags] if isinstance(tags, str) else tags
        return self.enabled and _LEVELS.get(level, 0) >= _LEVELS.get(self.level, 10) and (not self.tags or "*" in self.tags or bool({t.strip().lower() for t in tags} & set(self.tags)))

    def _source(self):
        frame = inspect.currentframe()
        try:
            while frame:
                filename = frame.f_code.co_filename
                if filename != __file__:
                    path = Path(filename).resolve()
                    root = Path(os.getenv("LOGFORGE_PROJECT_ROOT", os.getcwd())).resolve()
                    try:
                        filename = path.relative_to(root).as_posix()
                    except ValueError:
                        filename = str(path)
                    return {"file": filename, "line": frame.f_lineno, "function": frame.f_code.co_name, "language": "python"}
                frame = frame.f_back
        finally:
            del frame

    def _deliver(self, event):
        try:
            if len(json.dumps(event)) > 65536:
                event["data"] = {"truncated": True, "reason": "payload exceeds 64 KiB"}
            self.sink(event)
        except Exception:
            self._stats["errors"] += 1

    def out(self, tag="app", payload=None, *, tags=None, level="debug", name=None, id=None, source=None, _kind="output"):
        if not isinstance(tag, str):
            payload, tag = tag, "app"
        all_tags = list(dict.fromkeys(t.strip().lower() for t in [tag, *(tags or [])] if t.strip()))
        with self._lock:
            self._summary_written = False
            key = id or name or tag
            site = self._stats["sites"].setdefault(key, {"name": name or tag, "tags": all_tags, "emitted": 0, "filtered": 0})
            if not self.is_enabled(all_tags, level):
                self._stats["filtered"] += 1
                site["filtered"] += 1
                return None
            try:
                data = _clean(payload() if callable(payload) else payload)
            except Exception as exc:
                self._stats["errors"] += 1
                data = {"diagnostic_error": _clean(exc)}
            event = {"_logforge": "event", "schema_version": "1.0", "stream": "logforge.out", "event_id": str(uuid4()),
                     "run_id": self.run_id, "timestamp": datetime.now(timezone.utc).isoformat(), "service": self.service,
                     "kind": _kind, "name": name or tag, "site_id": id, "tags": all_tags, "level": level,
                     "source": source or self._source(), "data": data}
            if _kind == "check":
                event["passed"] = data is True
            self._stats["emitted"] += 1
            site["emitted"] += 1
            self._deliver(event)
            return event

    def debug(self, tag, payload=None, **options):
        return self.out(tag, payload, **{**options, "level": "debug"})

    def info(self, tag, payload=None, **options):
        return self.out(tag, payload, **{**options, "level": "info"})

    def warn(self, tag, payload=None, **options):
        return self.out(tag, payload, **{**options, "level": "warn"})

    def error(self, tag, payload=None, **options):
        return self.out(tag, payload, **{**options, "level": "error"})

    def check(self, tag, name, predicate, **options):
        return self.out(tag, predicate, **{**options, "name": name, "level": options.get("level", "info"), "_kind": "check"})

    def stats(self):
        with self._lock:
            return copy.deepcopy(self._stats)

    def flush(self):
        with self._lock:
            if not self._summary_written and (self._stats["emitted"] or self._stats["filtered"]):
                self._deliver({"_logforge": "summary", "schema_version": "1.0", "run_id": self.run_id,
                               "service": self.service, "timestamp": datetime.now(timezone.utc).isoformat(), "stats": self.stats()})
                self._summary_written = True

LogForge = _Logger()
atexit.register(LogForge.flush)

class LogForgeHandler(logging.Handler):
    """Attach to Python logging to retain existing logger.debug/info call sites."""
    def __init__(self, tag="app", tags=None):
        super().__init__()
        self.tag, self.tags = tag, tags or []

    def emit(self, record):
        level = "error" if record.levelno >= 40 else "warn" if record.levelno >= 30 else "info" if record.levelno >= 20 else "debug"
        LogForge.out(self.tag, lambda: {"message": record.getMessage()}, tags=self.tags, level=level, name=record.name,
                     source={"file": record.pathname, "line": record.lineno, "function": record.funcName, "language": "python"})
