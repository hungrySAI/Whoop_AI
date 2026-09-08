"""Local operator interface. JSON on stdout; errors and confirmation previews on stderr."""

import argparse
import json
import sys
from pathlib import Path

from .adapters import parse_csv, parse_whoop
from .analytics import list_metrics
from .commands import CommandService
from .credentials import database_key
from .live_cli import OAUTH_CONFIG, add_commands, execute_live, store_for
from .protection import DATABASE_ERRORS
from .service import CopilotService
from .storage import SCHEMA_VERSION, restore_backup


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="WHOOP Personal Copilot — synthetic foundation")
    root.add_argument("--db", type=Path)
    root.add_argument("--environment", choices=["synthetic", "real"], default="synthetic")
    root.add_argument("--oauth-config", type=Path, default=OAUTH_CONFIG)
    sub = root.add_subparsers(dest="command", required=True)
    add_commands(sub)
    dashboard = sub.add_parser("dashboard", help="Run the loopback-only local dashboard")
    dashboard.add_argument("--port", type=int, default=8766)
    dashboard.add_argument(
        "--demo", action="store_true", help="Use separate synthetic dashboard samples"
    )
    for name, help_text in (
        ("dashboard-open", "Start/reuse the local dashboard and open it in the browser"),
        ("dashboard-stop", "Gracefully stop this project's verified local dashboard"),
    ):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("--port", type=int, default=8766)
        item.add_argument("--demo", action="store_true")
    sub.add_parser("init", help="Create or validate the synthetic database")
    sub.add_parser("metrics", help="List registered metrics")
    demo = sub.add_parser("demo", help="Import the repository's synthetic fixtures and query HRV")
    demo.add_argument("--fixtures", type=Path, default=Path("tests/fixtures"))
    for name in ("import-whoop", "import-csv"):
        item = sub.add_parser(name)
        item.add_argument("path", type=Path)
    query = sub.add_parser("analyze")
    query.add_argument("metric")
    query.add_argument("--start", required=True)
    query.add_argument("--end", required=True)
    query.add_argument("--provider")
    query.add_argument("--as-of")
    query.add_argument("--resource")
    for name in ("run", "reproduce"):
        item = sub.add_parser(name)
        item.add_argument("run_id")
    sub.add_parser("tasks")
    work = sub.add_parser("work", help="Process one bounded batch of durable tasks")
    work.add_argument("--limit", type=int, default=20)
    backup = sub.add_parser("backup")
    backup.add_argument("target", type=Path)
    restore = sub.add_parser("restore")
    restore.add_argument("source", type=Path)
    restore.add_argument("target", type=Path)
    plans = sub.add_parser("plan")
    action = plans.add_subparsers(dest="plan_command", required=True)
    draft = action.add_parser("draft")
    draft.add_argument("--title", required=True)
    draft.add_argument("--details", default="")
    draft.add_argument("--key", required=True, help="Stable idempotency key for this user request")
    update = action.add_parser("propose-update")
    update.add_argument("plan_id")
    update.add_argument("--title", required=True)
    update.add_argument("--details", default="")
    update.add_argument("--version", type=int, required=True)
    update.add_argument("--key", required=True)
    get = action.add_parser("get")
    get.add_argument("plan_id")
    inspect = action.add_parser("inspect")
    inspect.add_argument("action_id")
    approve = action.add_parser("approve", help="Trusted local operator review and approval")
    approve.add_argument("action_id")
    approve.add_argument("--ttl", type=int, default=300)
    approve.add_argument(
        "--accept-hash", help="Explicitly approve a previously inspected payload hash"
    )
    commit = action.add_parser("commit")
    commit.add_argument("action_id")
    commit.add_argument("--token", required=True)
    revoke = action.add_parser("revoke")
    revoke.add_argument("action_id")
    return root


def execute(args: argparse.Namespace) -> object:
    if args.command in {"dashboard-open", "dashboard-stop"}:
        from .launcher import open_dashboard, stop_dashboard

        return open_dashboard(args) if args.command == "dashboard-open" else stop_dashboard(args)
    if args.command == "dashboard":
        from .web import run_dashboard

        return run_dashboard(args)
    if args.command in {
        "init-real",
        "export-inspect",
        "import-export",
        "purge-expired",
        "forget-source",
        "whoop",
    }:
        return execute_live(args)
    if args.command == "metrics":
        return list_metrics()
    if args.command == "restore":
        keys = (
            {
                "encryption_key": database_key(args.source),
                "target_key": database_key(args.target, create=True),
            }
            if args.environment == "real"
            else {}
        )
        return restore_backup(args.source, args.target, environment=args.environment, **keys)
    with store_for(args) as store:
        service = CopilotService(store)
        commands = CommandService(store.db)
        if args.command == "init":
            return {
                "database": str(store.path.resolve()),
                "environment": store.environment,
                "schema_version": SCHEMA_VERSION,
            }
        if args.command == "demo":
            if store.environment != "synthetic":
                raise ValueError("Demo fixtures cannot be loaded into a real database")
            whoop = store.ingest(parse_whoop(args.fixtures / "synthetic_whoop.json"))
            body = store.ingest(parse_csv(args.fixtures / "synthetic_body.csv"))
            analysis = service.analyze(
                "whoop.hrv_rmssd", "2026-08-01T00:00:00Z", "2026-08-15T00:00:00Z"
            )
            draft = commands.draft(
                "合成演示训练计划", "用户请求草稿示例；尚未确认或执行。", "synthetic-demo-plan-v1"
            )
            return {
                "environment": "synthetic",
                "imports": {"whoop": whoop, "body": body},
                "analysis": analysis,
                "training_draft": draft,
                "tasks": service.run_pending(),
            }
        if args.command == "import-whoop":
            return store.ingest(parse_whoop(args.path))
        if args.command == "import-csv":
            return store.ingest(parse_csv(args.path))
        if args.command == "analyze":
            return service.analyze(
                args.metric, args.start, args.end, args.provider, args.as_of, resource=args.resource
            )
        if args.command == "run":
            return service.get_run(args.run_id)
        if args.command == "reproduce":
            return service.reproduce(args.run_id)
        if args.command == "tasks":
            return service.list_tasks()
        if args.command == "work":
            return service.run_pending(args.limit)
        if args.command == "backup":
            return store.backup(
                args.target,
                target_key=database_key(args.target, create=True)
                if store.environment == "real"
                else None,
            )
        if args.command == "plan":
            if args.plan_command == "draft":
                return commands.draft(args.title, args.details, args.key)
            if args.plan_command == "propose-update":
                return commands.propose_update(
                    args.plan_id, args.title, args.details, args.version, args.key
                )
            if args.plan_command == "get":
                return commands.get_plan(args.plan_id)
            if args.plan_command == "inspect":
                return commands.get_action(args.action_id)
            if args.plan_command == "approve":
                preview = commands.get_action(args.action_id)
                print(json.dumps(preview, ensure_ascii=False, indent=2), file=sys.stderr)
                if args.accept_hash is not None:
                    if args.accept_hash != preview["payload_hash"]:
                        raise ValueError("The reviewed payload hash does not match this action")
                else:
                    if not sys.stdin.isatty():
                        raise ValueError(
                            "Approval requires an interactive operator or an explicit --accept-hash"
                        )
                    print("确认以上具体内容后，输入完整 payload_hash：", file=sys.stderr)
                    if input().strip() != preview["payload_hash"]:
                        raise ValueError("Approval cancelled; hash did not match")
                return commands.approve(args.action_id, args.ttl)
            if args.plan_command == "commit":
                return commands.commit(args.action_id, args.token)
            if args.plan_command == "revoke":
                return commands.revoke(args.action_id)
    raise ValueError("Unknown command")


def main() -> None:
    args = parser().parse_args()
    try:
        result = execute(args)
    except (ValueError, OSError, *DATABASE_ERRORS) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
