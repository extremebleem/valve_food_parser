"""One watch pass over Valve's public state.

    python -m scripts.watch                 # honours DRY_RUN
    python -m scripts.watch --dry-run       # never touch Telegram
    python -m scripts.watch --show          # print current state and exit
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from typing import List, Optional

from src.config import ConfigError, load_settings
from src.logging_utils import get_logger, setup_logging
from src.providers.github import GitHubProvider
from src.providers.steam import SteamNewsProvider, SteamVersionProvider
from src.storage import StorageError, create_storage
from src.watch_telegram import WatchNotifier
from src.watcher import Watcher

log = get_logger(__name__)


def build_providers(settings):
    return [SteamVersionProvider(settings), SteamNewsProvider(settings), GitHubProvider(settings)]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch Valve's public state for early signals")
    parser.add_argument("--dry-run", action="store_true", help="force DRY_RUN=true")
    parser.add_argument("--send", action="store_true", help="force DRY_RUN=false")
    parser.add_argument("--show", action="store_true", help="print the stored state and exit")
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
    setup_logging(settings.log_level, settings.log_format)

    try:
        storage = create_storage(settings.database_url)
    except StorageError as exc:
        print("storage error: {}".format(exc), file=sys.stderr)
        return 2

    try:
        with storage:
            if args.show:
                for subject in storage.list_subjects():
                    row = []
                    for key in ("required_version", "latest_prerelease", "latest_news", "latest_release"):
                        value = storage.get_watch_value(subject.id, key)
                        if value:
                            row.append("{}={}".format(key, (value["label"] or value["value"])[:40]))
                    print("{:<38} {}".format(subject.name[:38], " | ".join(row) or "—"))
                print("\nсобытий в журнале: {}".format(storage.count_watch_events()))
                return 0

            watcher = Watcher(
                settings,
                storage,
                build_providers(settings),
                notifier=WatchNotifier(settings, storage),
            )
            watcher.run()
    except StorageError as exc:
        print("storage failure: {}".format(exc), file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print("watch cannot run: {}".format(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
