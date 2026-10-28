"""Opt-in integration against the configured real LogForge instance."""
import asyncio
import json
import os
from pathlib import Path
import sys
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = Path(os.getenv("LOGFORGE_TEST_PLUGIN_ROOT", str(ROOT)))

@pytest.mark.skipif(os.getenv("LOGFORGE_TEST_LIVE") != "1", reason="Set LOGFORGE_TEST_LIVE=1 for the configured appliance")
def test_real_gateway_and_local_report_through_mcp_sdk():
    async def scenario():
        params = StdioServerParameters(command=sys.executable, args=[str(PLUGIN / "scripts/logforge_mcp.py"), "serve"], cwd=str(PLUGIN))
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                initialized = await session.initialize()
                assert initialized.model_dump(by_alias=True)["serverInfo"]["name"] == "logforge-central-mcp"
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                assert {"logforge_logs_query", "logforge_inventory_search", "logforge_runs_list", "logforge_run_read"} <= names
                result = await session.call_tool("logforge_inventory_search", {"limit": 5})
                assert not result.model_dump(by_alias=True).get("isError")
                runs = await session.call_tool("logforge_runs_list", {"project_path": str(ROOT / "examples/checkout")})
                assert not runs.model_dump(by_alias=True).get("isError")
                data = json.loads(runs.content[0].text)
                assert data["runs"]
                report = await session.call_tool("logforge_run_read", {"project_path": str(ROOT / "examples/checkout"), "run_id": data["runs"][0]["run_id"], "tag": "CHECKOUT"})
                assert not report.model_dump(by_alias=True).get("isError")
                assert json.loads(report.content[0].text)["matching_event_count"] >= 1
    asyncio.run(scenario())

@pytest.mark.skipif(os.getenv("LOGFORGE_TEST_LIVE") != "1" or not os.getenv("LOGFORGE_TEST_CONTAINER_KEY"), reason="Requires an explicitly selected test workload")
def test_tagged_event_is_retained_in_central():
    async def scenario():
        params = StdioServerParameters(command=sys.executable, args=[str(PLUGIN / "scripts/logforge_mcp.py"), "serve"], cwd=str(PLUGIN))
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                key = os.environ["LOGFORGE_TEST_CONTAINER_KEY"]
                result = await session.call_tool("logforge_logs_query", {"container_key": key, "limit": 20})
                assert not result.model_dump(by_alias=True).get("isError")
                data = json.loads(result.content[0].text)
                events = [json.loads(row["_msg"]) for row in data["rows"]]
                selected = [event for event in events if event.get("run_id") == os.environ["LOGFORGE_TEST_RUN_ID"]]
                assert selected, "Expected tagged test events were not retained by Central"
                assert all(event["stream"] == "logforge.out" and "checkout" in event["tags"] for event in selected)
                assert all(event["name"] != "catalog.hidden" for event in selected)
    asyncio.run(scenario())
