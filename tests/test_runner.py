import json
from pathlib import Path
import jsonschema
import pytest
import logforge_run as runner

def plan(tmp_path, source, expect=None, timeout=10):
    (tmp_path / "app.py").write_text(source)
    path = tmp_path / "project.logforge"
    path.write_text(json.dumps({"schema_version": "1.0", "kind": "project", "commands": [{"argv": ["${PYTHON}", "app.py"], "timeout_seconds": timeout}], "expect": expect or []}))
    return path

def test_run_reports_reached_filtered_and_unreached(tmp_path):
    path = plan(tmp_path, 'from logforge_out import LogForge\nLogForge.out("Checkout", lambda: {"id": 1}, name="saved")\nLogForge.out("catalog", lambda: 1/0, name="catalog")\nLogForge.check("checkout", "row", lambda: True)\n', [
        {"name": "saved", "tags": ["checkout"]}, {"name": "row", "kind": "check", "tags": ["checkout"]},
        {"name": "catalog", "tags": ["catalog"]}, {"name": "missing", "tags": ["checkout"]}])
    output, report = runner.run(path, ["checkout"])
    assert report["status"] == "failed"
    assert [e["status"] for e in report["expectations"]] == ["reached", "reached", "filtered", "unreached"]
    assert report["summary"]["filtered"] == 1
    assert report["events"][0]["source"]["snippet"]["start_line"] == 1
    assert report["events"][0]["source"]["file"] == "app.py"
    assert json.loads(output.read_text())["run_id"] == report["run_id"]
    schema = json.loads((runner.ROOT / "schemas/run.schema.json").read_text())
    jsonschema.validate(report, schema)

def test_false_check_fails_even_when_command_succeeds(tmp_path):
    _, report = runner.run(plan(tmp_path, 'from logforge_out import LogForge\nLogForge.check("checkout", "broken", lambda: False)'), [])
    assert report["status"] == "failed"
    assert report["summary"]["failed_checks"] == ["broken"]

def test_level_filter_marks_expected_outputs_filtered(tmp_path):
    path = plan(tmp_path, 'from logforge_out import LogForge\nLogForge.debug("checkout", lambda: 1/0, name="verbose")\nLogForge.warn("checkout", "slow", name="slow")', [
        {"name": "verbose", "tags": ["checkout"]}, {"name": "slow", "tags": ["checkout"], "level": "warn"}])
    _, report = runner.run(path, ["checkout"], "warn")
    assert report["status"] == "passed"
    assert [e["status"] for e in report["expectations"]] == ["filtered", "reached"]
    assert report["summary"]["diagnostic_errors"] == 0

def test_empty_run_does_not_claim_verification(tmp_path):
    _, report = runner.run(plan(tmp_path, 'print("no diagnostics here")'), [])
    assert report["status"] == "failed"
    assert "No selected diagnostic events" in report["errors"][0]

def test_timeout_stops_only_the_owned_process(tmp_path):
    _, report = runner.run(plan(tmp_path, 'import time\ntime.sleep(30)', timeout=0.2), [])
    assert report["status"] == "failed"
    assert report["commands"][0]["timed_out"] is True

def test_external_source_is_not_read(tmp_path):
    source = {"file": "../secret.txt", "line": 1}
    assert "snippet" not in runner.source_context(source, tmp_path)

def test_plans_require_explicit_argv_and_contained_cwd(tmp_path):
    path = tmp_path / "project.logforge"
    for command in ({"argv": "echo hello"}, {"argv": ["python"], "cwd": ".."}):
        path.write_text(json.dumps({"schema_version": "1.0", "kind": "project", "commands": [command]}))
        with pytest.raises(ValueError): runner.load_plan(path)

def test_capture_rejects_wrong_run_and_deduplicates():
    capture = runner.Capture("run", ["checkout"])
    event = {"_logforge": "event", "schema_version": "1.0", "run_id": "run", "event_id": "one", "name": "saved", "kind": "output", "tags": ["checkout"]}
    assert capture.accept(event, "app")
    assert capture.accept(event, "app")
    assert len(capture.events) == 1
    assert not capture.accept({**event, "run_id": "other"}, "app")
