"""Official MCP Python SDK transport; shared services own all business logic."""

import argparse
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .analytics import list_metrics
from .commands import CommandService
from .service import CopilotService
from .storage import DEFAULT_DB, Store


def create_server(db_path: Path | str = DEFAULT_DB) -> FastMCP:
    server = FastMCP(
        "WHOOP Personal Copilot — synthetic",
        instructions=(
            "This is a synthetic-only demonstration, with no real WHOOP account or medical advice. "
            "Use registered analysis tools for numerical statements and cite the returned evidence. "
            "Create training drafts only for explicit user requests. A draft is not performed exercise "
            "or a confirmed user fact. Approval is issued by the trusted local operator outside MCP; "
            "never invent an approval token. No real WHOOP writes or network operations are available."
        ),
    )
    read = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
    write = ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )

    @server.tool(annotations=read)
    def metrics() -> dict:
        """List supported metric keys, units and official source semantics."""
        return list_metrics()

    @server.tool(annotations=read)
    def analyze(
        metric: str,
        start: str,
        end: str,
        provider: str | None = None,
        as_of: str | None = None,
        resource: str | None = None,
    ) -> dict:
        """Compare equal-duration window halves with versioned evidence. Use timezone-aware times."""
        with Store(db_path) as store:
            return CopilotService(store).analyze(
                metric, start, end, provider, as_of, resource=resource
            )

    @server.tool(annotations=read)
    def analysis_run(run_id: str) -> dict:
        """Read an existing analysis and its stale flag and evidence bundle."""
        with Store(db_path) as store:
            return CopilotService(store).get_run(run_id)

    @server.tool(annotations=read)
    def reproduce_analysis(run_id: str) -> dict:
        """Recompute an analysis using its saved source revision IDs and calculation version."""
        with Store(db_path) as store:
            return CopilotService(store).reproduce(run_id)

    @server.tool(annotations=write)
    def draft_training(title: str, details: str, idempotency_key: str) -> dict:
        """Persist a user-requested local training draft; this does not activate a plan."""
        with Store(db_path) as store:
            return CommandService(store.db).draft(title, details, idempotency_key)

    @server.tool(annotations=write)
    def propose_training_update(
        plan_id: str, title: str, details: str, expected_version: int, idempotency_key: str
    ) -> dict:
        """Propose a change to an active plan without changing its current contents."""
        with Store(db_path) as store:
            return CommandService(store.db).propose_update(
                plan_id,
                title,
                details,
                expected_version,
                idempotency_key,
            )

    @server.tool(annotations=read)
    def training_action(action_id: str) -> dict:
        """Read the exact action payload and scope before local operator review."""
        with Store(db_path) as store:
            return CommandService(store.db).get_action(action_id)

    @server.tool(annotations=read)
    def training_plan(plan_id: str) -> dict:
        """Read the current plan and target version."""
        with Store(db_path) as store:
            return CommandService(store.db).get_plan(plan_id)

    @server.tool(annotations=write)
    def commit_training(action_id: str, approval_token: str) -> dict:
        """Submit only the exact locally approved action, with version and replay checks."""
        with Store(db_path) as store:
            return CommandService(store.db).commit(action_id, approval_token)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic WHOOP Copilot MCP over local stdio")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    create_server(args.db).run(transport="stdio")


if __name__ == "__main__":
    main()
