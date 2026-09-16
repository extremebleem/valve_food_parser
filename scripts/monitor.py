"""One monitoring pass.

    python -m scripts.monitor                 # normal run (honours DRY_RUN)
    python -m scripts.monitor --dry-run       # never touch Telegram
    python -m scripts.monitor --test-telegram # send a test message and exit
    python -m scripts.monitor --chat-ids      # print chat ids the bot can see
    python -m scripts.monitor --limit 20      # only the 20 nearest open venues
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from typing import List, Optional

from src.config import ConfigError, load_settings
from src.logging_utils import get_logger, setup_logging
from src.monitor import Monitor, MonitorError
from src.storage import StorageError, create_storage
from src.telegram import MessageBuilder, TelegramClient

log = get_logger(__name__)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check current demand around the office")
    parser.add_argument("--dry-run", action="store_true", help="force DRY_RUN=true")
    parser.add_argument("--send", action="store_true", help="force DRY_RUN=false")
    parser.add_argument("--test-telegram", action="store_true", help="send a test alert and exit")
    parser.add_argument("--chat-ids", action="store_true", help="list chat ids from getUpdates")
    parser.add_argument(
        "--limit", type=int, help="override MAX_VENUES_PER_RUN (0 = no limit)"
    )
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument(
        "--ignore-window",
        action="store_true",
        help="run even outside ACTIVE_HOURS_START..END (for manual and local runs)",
    )
    parser.add_argument("--purge-days", type=int, help="delete observations older than N days")
    parser.add_argument(
        "--purge-only", action="store_true", help="only run housekeeping, then exit"
    )
    parser.add_argument("--env-file", default=".env")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        settings = load_settings(args.env_file)
    except ConfigError as exc:
        print("configuration error: {}".format(exc), file=sys.stderr)
        return 2

    if args.dry_run:
        settings = dataclasses.replace(settings, dry_run=True)
    if args.send:
        settings = dataclasses.replace(settings, dry_run=False)
    if args.limit is not None:
        settings = dataclasses.replace(settings, max_venues_per_run=int(args.limit))
    if args.ignore_window:
        settings = dataclasses.replace(
            settings, active_window=dataclasses.replace(settings.active_window, start_hour=0, end_hour=0)
        )

    setup_logging(settings.log_level, settings.log_format)

    if args.chat_ids:
        client = TelegramClient(dataclasses.replace(settings, dry_run=False))
        chats = client.recent_chat_ids()
        if not chats:
            print(
                "no chats found. Send any message to your bot (or add it to the group and "
                "post there), then run this again.",
                file=sys.stderr,
            )
            return 1
        print(json.dumps(chats, ensure_ascii=False, indent=2))
        return 0

    if args.test_telegram:
        client = TelegramClient(settings)
        if not client.configured and not settings.dry_run:
            print(
                "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set", file=sys.stderr
            )
            return 2
        if client.configured and not settings.dry_run:
            info = client.describe_chat()
            print("getChat: {}".format(json.dumps(info, ensure_ascii=False)[:500]))
            if isinstance(info, dict) and not info.get("ok", False):
                return 1
        ok = client.send_message(MessageBuilder(settings).test_message())
        print("test message {}".format("sent" if ok else "FAILED"))
        return 0 if ok else 1

    try:
        storage = create_storage(settings.database_url)
    except StorageError as exc:
        print("storage error: {}".format(exc), file=sys.stderr)
        return 2

    try:
        with storage:
            if args.purge_days:
                removed = storage.purge_observations(args.purge_days)
                log.info("observations purged", extra={"removed": removed})
                print("purged {} observations older than {} days".format(removed, args.purge_days))
            if args.purge_only:
                return 0
            monitor = Monitor(settings, storage)
            monitor.run(concurrency=args.concurrency)
    except MonitorError as exc:
        # systemic problem: misconfiguration or an empty venue table
        print("monitor cannot run: {}".format(exc), file=sys.stderr)
        return 2
    except StorageError as exc:
        print("storage failure: {}".format(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
