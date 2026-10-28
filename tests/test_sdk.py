import logging
from logforge_out import LogForge, LogForgeHandler

def setup_function():
    LogForge.reset()
    LogForge.configure(tags=[], level="debug", enabled=True, sink=lambda event: None)

def test_case_insensitive_lazy_filter():
    events = []
    LogForge.configure(tags=[" CHECKOUT "], sink=events.append)
    LogForge.out("catalog", lambda: 1 / 0)
    LogForge.out("Checkout", lambda: {"ok": True}, tags=["BACKEND"])
    assert len(events) == 1
    assert events[0]["tags"] == ["checkout", "backend"]
    assert LogForge.stats()["filtered"] == 1
    assert LogForge.stats()["errors"] == 0

def test_levels_and_all_tags():
    events = []
    LogForge.configure(sink=events.append, level="warn")
    LogForge.debug("catalog", lambda: 1 / 0)
    LogForge.warn("checkout", {"slow": True})
    LogForge.error("catalog", {"failed": True})
    assert [e["level"] for e in events] == ["warn", "error"]
    assert LogForge.stats()["errors"] == 0

def test_redaction_and_cycles():
    events = []
    LogForge.configure(sink=events.append)
    value = {"token": "hidden", "nested": {"password": "secret"}}
    value["cycle"] = value
    LogForge.out("checkout", value)
    assert events[0]["data"] == {"token": "[REDACTED]", "nested": {"password": "[REDACTED]"}, "cycle": "[circular]"}

def test_check_source_and_failures():
    events = []
    LogForge.configure(sink=events.append)
    LogForge.check("checkout", "truth", lambda: True)
    LogForge.check("checkout", "falsehood", lambda: False)
    LogForge.out("checkout", lambda: 1 / 0)
    assert events[0]["passed"] is True
    assert events[1]["passed"] is False
    assert events[0]["source"]["file"].endswith("test_sdk.py")
    assert events[0]["source"]["line"] > 0
    assert "diagnostic_error" in events[2]["data"]

def test_logging_handler_preserves_call_site_and_is_lazy():
    events = []
    LogForge.configure(tags=["checkout"], sink=events.append)
    logger = logging.getLogger("orders-test")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = LogForgeHandler("checkout", ["backend"])
    logger.addHandler(handler)
    try:
        logger.debug("Saved order %s", 12)
    finally:
        logger.removeHandler(handler)
    assert events[0]["data"]["message"] == "Saved order 12"
    assert events[0]["source"]["file"].endswith("test_sdk.py")

def test_sink_failure_does_not_raise():
    def fail(_):
        raise OSError("unavailable")
    LogForge.configure(sink=fail)
    LogForge.out("checkout", 1)
    assert LogForge.stats()["errors"] == 1
