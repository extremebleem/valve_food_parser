"""Rebuild the venue list from every enabled discovery source.

    python -m scripts.discover [--radius 2500] [--output venues.json] [--dry-run]
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from typing import List, Optional

from src.config import ConfigError, Settings, load_settings
from src.discovery import DiscoveryRunner
from src.logging_utils import get_logger, github_summary, setup_logging
from src.providers.base import ProviderError
from src.storage import StorageError, create_storage

log = get_logger(__name__)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover food venues around the office")
    parser.add_argument("--radius", type=int, help="override SEARCH_RADIUS_METERS")
    parser.add_argument("--output", help="also write the merged venue list to this JSON file")
    parser.add_argument("--no-store", action="store_true", help="do not write to the database")
    parser.add_argument("--env-file", default=".env")
    return parser.parse_args(argv)


def apply_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    if args.radius:
        office = dataclasses.replace(settings.office, radius_meters=int(args.radius))
        office.validate()
        settings = dataclasses.replace(settings, office=office)
    return settings


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        settings = apply_overrides(load_settings(args.env_file), args)
    except ConfigError as exc:
        print("configuration error: {}".format(exc), file=sys.stderr)
        return 2

    setup_logging(settings.log_level, settings.log_format)
    log.info(
        "discovery starting",
        extra={
            "office": settings.office.name,
            "lat": settings.office.latitude,
            "lon": settings.office.longitude,
            "radius_m": settings.office.radius_meters,
        },
    )

    try:
        storage = create_storage(settings.database_url)
    except StorageError as exc:
        print("storage error: {}".format(exc), file=sys.stderr)
        return 2

    try:
        with storage:
            runner = DiscoveryRunner(settings, storage)
            if not runner.providers:
                print("no discovery providers enabled", file=sys.stderr)
                return 2
            stats = runner.run(store=not args.no_store)
            venues = storage.list_venues(active_only=True)
    except ProviderError as exc:
        print("discovery failed: {}".format(exc), file=sys.stderr)
        return 1
    except StorageError as exc:
        print("storage failure: {}".format(exc), file=sys.stderr)
        return 2

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump([v.to_dict() for v in venues], fh, ensure_ascii=False, indent=2)
        log.info("venue snapshot written", extra={"path": args.output, "venues": len(venues)})

    summary = [
        "### Venue discovery",
        "",
        "- office: **{}** ({:.5f}, {:.5f}), radius **{} m**".format(
            settings.office.name,
            settings.office.latitude,
            settings.office.longitude,
            settings.office.radius_meters,
        ),
        "- raw candidates: **{}** -> merged: **{}** (removed {} duplicates)".format(
            stats["raw_candidates"], stats["merged"], stats["duplicates_removed"]
        ),
        "- inserted: {}, updated: {}, deactivated as stale: {}".format(
            stats["inserted"], stats["updated"], stats["deactivated_stale"]
        ),
        "",
        "| Source | Venues |",
        "| --- | --- |",
    ]
    for name, count in sorted(stats["per_source"].items()):
        summary.append("| {} | {} |".format(name, count))
    summary += ["", "| Category | Venues |", "| --- | --- |"]
    for name, count in stats["categories"].items():
        summary.append("| {} | {} |".format(name, count))
    if stats["failures"]:
        summary += ["", "**Failed sources:**"] + [
            "- `{}`: {}".format(k, v) for k, v in stats["failures"].items()
        ]
    github_summary("\n".join(summary))

    print(
        "discovery: raw={raw_candidates} merged={merged} inserted={inserted} "
        "updated={updated} deactivated={deactivated_stale}".format(**stats)
    )
    # a partial result is still a success; only a total failure returns non-zero
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
