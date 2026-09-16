"""Create the schema and print what is in it.

    python -m scripts.init_db
    python -m scripts.init_db --stats
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from src.config import ConfigError, load_settings
from src.logging_utils import setup_logging
from src.storage import StorageError, create_storage


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Initialise the watch database")
    parser.add_argument("--stats", action="store_true", help="print row counts after migrating")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.env_file)
    except ConfigError as exc:
        print("configuration error: {}".format(exc), file=sys.stderr)
        return 2
    setup_logging(settings.log_level, "text")

    redacted = settings.database_url
    if "@" in redacted:
        redacted = redacted.split("://", 1)[0] + "://***@" + redacted.rsplit("@", 1)[1]

    try:
        with create_storage(settings.database_url) as storage:
            print("schema ready on {}".format(redacted))
            if args.stats:
                for table in (
                    "subjects",
                    "watch_state",
                    "watch_events",
                    "subject_observations",
                    "alerts",
                    "runs",
                ):
                    row = storage.fetchone("SELECT COUNT(*) FROM {}".format(table))
                    print("  {:<12} {}".format(table, row[0] if row else 0))
    except StorageError as exc:
        print("storage error: {}".format(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
