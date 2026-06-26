from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from job_finger.config import DEFAULT_CONFIG_PATH, load_config, write_example_config
from job_finger.drafts import write_application_brief
from job_finger.pipeline import run_searches
from job_finger.storage import (
    export_ranked_csv,
    get_job_with_latest_score,
    list_ranked_jobs,
    update_application,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-finger",
        description="Portugal-first job filtering and application tracking.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="write an example config")
    init_parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    init_parser.add_argument("--force", action="store_true")
    init_parser.set_defaults(func=cmd_init)

    search_parser = subparsers.add_parser("search", help="scrape, score, and store jobs")
    add_config_lake_args(search_parser)
    search_parser.add_argument("--search", action="append", dest="searches")
    search_parser.add_argument("--dry-run", action="store_true")
    search_parser.add_argument("--top", type=int, default=10)
    search_parser.set_defaults(func=cmd_search)

    rank_parser = subparsers.add_parser("rank", help="show ranked jobs from storage")
    add_config_lake_args(rank_parser)
    rank_parser.add_argument("--limit", type=int, default=25)
    rank_parser.add_argument("--min-score", type=float, default=0)
    rank_parser.add_argument("--status")
    rank_parser.add_argument("--csv")
    rank_parser.set_defaults(func=cmd_rank)

    track_parser = subparsers.add_parser("track", help="update application status")
    add_config_lake_args(track_parser)
    track_parser.add_argument("job_id")
    track_parser.add_argument(
        "--status",
        required=True,
        choices=[
            "new",
            "saved",
            "applied",
            "follow_up",
            "interview",
            "offer",
            "rejected",
            "ignored",
        ],
    )
    track_parser.add_argument("--notes")
    track_parser.add_argument("--applied-at")
    track_parser.add_argument("--next-action-at")
    track_parser.add_argument("--resume-version")
    track_parser.add_argument("--cover-letter-path")
    track_parser.add_argument("--contact-name")
    track_parser.add_argument("--contact-email")
    track_parser.set_defaults(func=cmd_track)

    brief_parser = subparsers.add_parser(
        "brief", help="write a resume and cover-letter prep brief"
    )
    add_config_lake_args(brief_parser)
    brief_parser.add_argument("job_id")
    brief_parser.add_argument("--out")
    brief_parser.set_defaults(func=cmd_brief)

    return parser


def add_config_lake_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--lake")


def cmd_init(args) -> int:
    path = write_example_config(args.config, force=args.force)
    print(f"Wrote {path}")
    return 0


def cmd_search(args) -> int:
    config = load_config(args.config)
    results = run_searches(
        config,
        search_names=args.searches,
        lake_path=args.lake,
        dry_run=args.dry_run,
    )
    for result in results:
        target = "dry run" if args.dry_run else f"run {result.run_id}"
        print(
            f"{result.search_name}: scraped {result.total_scraped}, "
            f"stored {result.total_stored} ({target})"
        )
        _print_ranked_items(result.ranked_jobs[: args.top])
    return 0


def cmd_rank(args) -> int:
    config = load_config(args.config)
    lake_path = config.resolve_storage_path(args.lake)
    rows = list_ranked_jobs(
        lake_path, limit=args.limit, min_score=args.min_score, status=args.status
    )
    if args.csv:
        path = export_ranked_csv(rows, args.csv)
        print(f"Wrote {path}")
    else:
        _print_rows(rows)
    return 0


def cmd_track(args) -> int:
    config = load_config(args.config)
    lake_path = config.resolve_storage_path(args.lake)
    update_application(
        lake_path,
        job_id=args.job_id,
        status=args.status,
        notes=args.notes,
        applied_at=args.applied_at,
        next_action_at=args.next_action_at,
        resume_version=args.resume_version,
        cover_letter_path=args.cover_letter_path,
        contact_name=args.contact_name,
        contact_email=args.contact_email,
    )
    print(f"Updated {args.job_id} to {args.status}")
    return 0


def cmd_brief(args) -> int:
    config = load_config(args.config)
    lake_path = config.resolve_storage_path(args.lake)
    row = get_job_with_latest_score(lake_path, args.job_id)
    if row is None:
        raise SystemExit(f"No job found with id {args.job_id}")
    out_path = args.out or f"briefs/{args.job_id}.md"
    path = write_application_brief(dict(row), config.profile, out_path)
    print(f"Wrote {path}")
    return 0


def _print_ranked_items(items) -> None:
    rows = []
    for item in items:
        rows.append(
            {
                "job_id": item.job_id,
                "score": item.score.score,
                "prob": item.score.estimated_fit_probability,
                "recommendation": item.score.recommendation,
                "title": item.job.get("title"),
                "company": item.job.get("company"),
                "site": item.job.get("site"),
            }
        )
    _print_dicts(rows)


def _print_rows(rows) -> None:
    _print_dicts(
        [
            {
                "job_id": row["job_id"],
                "score": row["score"],
                "prob": row["estimated_fit_probability"],
                "status": row["application_status"],
                "recommendation": row["recommendation"],
                "title": row["title"],
                "company": row["company"],
                "site": row["site"],
            }
            for row in rows
        ]
    )


def _print_dicts(rows: list[dict]) -> None:
    if not rows:
        print("No rows.")
        return
    columns = list(rows[0].keys())
    widths = {
        column: min(
            42,
            max(len(column), *(len(_cell(row.get(column))) for row in rows)),
        )
        for column in columns
    }
    header = "  ".join(column.ljust(widths[column]) for column in columns)
    print(header)
    print("  ".join("-" * widths[column] for column in columns))
    for row in rows:
        print(
            "  ".join(
                _cell(row.get(column))[: widths[column]].ljust(widths[column])
                for column in columns
            )
        )


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value).replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
