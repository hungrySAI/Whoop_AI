"""Real local stdio protocol checks using the official MCP client and server SDK.

These checks do not connect a Codex client, use a network service, or read an
actual WHOOP account. Each server process uses a temporary synthetic database.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from whoop_copilot.adapters import parse_csv, parse_whoop
from whoop_copilot.commands import CommandService
from whoop_copilot.service import CopilotService
from whoop_copilot.storage import Store

REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
REQUEST = {
    "metric": "whoop.hrv_rmssd",
    "start": "2026-08-01T00:00:00Z",
    "end": "2026-08-15T00:00:00Z",
}
EXPOSED_TOOLS = {
    "metrics",
    "analyze",
    "analysis_run",
    "reproduce_analysis",
    "draft_training",
    "propose_training_update",
    "training_action",
    "training_plan",
    "commit_training",
}


def _server(db_path):
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "whoop_copilot.mcp_server", "--db", str(db_path)],
        cwd=REPOSITORY,
    )


def _payload(result):
    assert result.isError is False, result
    if result.structuredContent is not None:
        return result.structuredContent
    texts = [item.text for item in result.content if item.type == "text"]
    assert len(texts) == 1
    return json.loads(texts[0])


async def test_stdio_queries_match_cli_and_shared_service(tmp_path):
    db_path = tmp_path / "synthetic.sqlite3"
    with Store(db_path) as store:
        store.ingest(parse_whoop(FIXTURES / "synthetic_whoop.json"))
        store.ingest(parse_csv(FIXTURES / "synthetic_body.csv"))
        expected = CopilotService(store).analyze(**REQUEST)

    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "whoop_copilot.cli",
            "--db",
            str(db_path),
            "analyze",
            REQUEST["metric"],
            "--start",
            REQUEST["start"],
            "--end",
            REQUEST["end"],
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert json.loads(cli.stdout) == expected

    async def exercise():
        async with stdio_client(_server(db_path)) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "WHOOP Personal Copilot — synthetic"
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                assert set(tools) == EXPOSED_TOOLS
                assert tools["analyze"].annotations.readOnlyHint is True
                assert tools["commit_training"].annotations.readOnlyHint is False
                assert all(
                    forbidden not in name
                    for name in tools
                    for forbidden in ("approve", "sql", "shell", "import")
                )

                metrics = _payload(await session.call_tool("metrics"))
                assert metrics["whoop.hrv_rmssd"]["unit"] == "ms"
                assert metrics["whoop.hrv_rmssd"]["official"] is True
                assert metrics["body.weight"]["unit"] == "kg"

                analysis = _payload(await session.call_tool("analyze", REQUEST))
                assert analysis == expected
                assert analysis["run_id"] == expected["run_id"]
                assert analysis["evidence"]["references"] == expected["evidence"]["references"]
                assert analysis["result"]["count"] == 14
                loaded = _payload(
                    await session.call_tool("analysis_run", {"run_id": analysis["run_id"]})
                )
                assert loaded == analysis
                reproduced = _payload(
                    await session.call_tool("reproduce_analysis", {"run_id": analysis["run_id"]})
                )
                assert reproduced["matches"] is True
                assert reproduced["result"] == analysis["result"]

                for invalid in (
                    {**REQUEST, "metric": "not.registered"},
                    {**REQUEST, "start": "2026-08-01T00:00:00"},
                    {**REQUEST, "start": []},
                ):
                    rejected = await session.call_tool("analyze", invalid)
                    assert rejected.isError is True
                unknown = await session.call_tool("approve", {"action_id": "no-such-action"})
                assert unknown.isError is True

    await asyncio.wait_for(exercise(), timeout=20)


async def test_stdio_draft_requires_local_approval_and_commit_replays(tmp_path):
    db_path = tmp_path / "synthetic.sqlite3"
    with Store(db_path):
        pass

    async def exercise():
        async with stdio_client(_server(db_path)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                request = {
                    "title": "合成测试训练草稿",
                    "details": "用户请求的本地草稿；不是已经完成的运动。",
                    "idempotency_key": "stdio-synthetic-draft",
                }
                draft = _payload(await session.call_tool("draft_training", request))
                assert draft["status"] == "proposed"
                assert _payload(await session.call_tool("draft_training", request)) == draft
                assert "approval_token" not in draft

                forged = {
                    "action_id": draft["action_id"],
                    "approval_token": "forged_token_never_approved_00000",
                }
                rejected = await session.call_tool("commit_training", forged)
                assert rejected.isError is True
                plan = _payload(
                    await session.call_tool("training_plan", {"plan_id": draft["plan_id"]})
                )
                assert plan["status"] == "draft"
                assert plan["version"] == 0
                inspected = _payload(
                    await session.call_tool("training_action", {"action_id": draft["action_id"]})
                )
                assert inspected == draft

                # The model-facing stdio server cannot mint this capability.
                # Simulate the separate trusted operator reviewing the exact action.
                with Store(db_path) as store:
                    commands = CommandService(store.db)
                    assert (
                        commands.get_action(draft["action_id"])["payload_hash"]
                        == draft["payload_hash"]
                    )
                    approved = commands.approve(draft["action_id"])
                commit_arguments = {
                    "action_id": draft["action_id"],
                    "approval_token": approved["approval_token"],
                }
                committed = _payload(await session.call_tool("commit_training", commit_arguments))
                assert committed["status"] == "active"
                assert committed["version"] == 1
                assert committed["title"] == request["title"]
                replayed = _payload(await session.call_tool("commit_training", commit_arguments))
                assert replayed == committed
                assert (await session.call_tool("commit_training", forged)).isError is True

                with Store(db_path) as store:
                    assert CommandService(store.db).get_plan(draft["plan_id"]) == committed
                    commits = store.db.execute(
                        "SELECT COUNT(*) FROM command_audit WHERE action_id=? AND event='committed'",
                        (draft["action_id"],),
                    ).fetchone()[0]
                    assert commits == 1

    await asyncio.wait_for(exercise(), timeout=20)
