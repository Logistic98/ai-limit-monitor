from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from contextlib import suppress

from application.monitor_service import LimitMonitor
from config.settings import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Claude and Codex usage limit monitor")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("run", help="run monitor and Telegram bot polling")

    once_parser = subparsers.add_parser("once", help="run one check and print report")
    once_parser.add_argument(
        "--notify",
        action="store_true",
        help="also send the report to Telegram",
    )

    subparsers.add_parser("send-test", help="send a Telegram test message")
    subparsers.add_parser("reset-codex-cards", help="query Codex reset credits")
    subparsers.add_parser("reset-codex-tasks", help="query automatic Codex reset tasks")
    use_parser = subparsers.add_parser("reset-codex-use", help="use a Codex reset credit")
    use_parser.add_argument("--confirm", metavar="TOKEN", help="confirm a prepared reset credit")
    auto_parser = subparsers.add_parser("reset-codex-auto", help="set automatic Codex reset use")
    auto_parser.add_argument("enabled", choices=["on", "off"])
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = build_parser()
    args = parser.parse_args()
    command = args.command or "run"
    with suppress(KeyboardInterrupt):
        asyncio.run(_run(command, args))


async def _run(command: str, args: argparse.Namespace) -> None:
    settings = Settings()
    monitor = LimitMonitor(settings)
    try:
        if command == "run":
            await monitor.run_forever()
        elif command == "once":
            result = await monitor.check()
            print(await monitor.report(result))
            if args.notify:
                await monitor.check_and_notify(force_report=True)
        elif command == "send-test":
            await monitor.send_test_message()
        elif command in {
            "reset-codex-cards", "reset-codex-tasks", "reset-codex-use", "reset-codex-auto",
        }:
            options = []
            if command == "reset-codex-use":
                if args.confirm:
                    options = ["confirm", args.confirm]
            elif command == "reset-codex-auto":
                options = [args.enabled]
            print(await monitor.codex_reset_command("/" + command.replace("-", "_"), options))
        else:
            print(f"Unknown command: {command}", file=sys.stderr)
            raise SystemExit(2)
    finally:
        await monitor.close()


if __name__ == "__main__":
    main()
