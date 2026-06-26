from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from job_finger.config import (
    DEFAULT_CONFIG_PATH,
    SearchSpec,
    ensure_workspace_files,
    load_config,
    write_example_config,
)
from job_finger.drafts import write_application_brief
from job_finger.pipeline import run_searches
from job_finger.resume import convert_resume_to_markdown, write_resume_profile
from job_finger.search_terms import (
    build_keyword_query,
    expand_related_topics,
    filter_rows_excluding_terms,
    filter_rows_by_terms,
    unique_terms,
)
from job_finger.storage import (
    export_ranked_csv,
    get_job_with_latest_score,
    list_ranked_jobs,
    update_application,
)
from job_finger.ui_server import run_ui_server


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

    cv_parser = subparsers.add_parser(
        "cv", help="convert a CV PDF/DOCX/etc. into workspace/cv.md"
    )
    cv_parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    cv_parser.add_argument("--input")
    cv_parser.add_argument("--out")
    cv_parser.add_argument("--profile-out")
    cv_parser.set_defaults(func=cmd_cv)

    search_parser = subparsers.add_parser("search", help="scrape, score, and store jobs")
    add_config_data_args(search_parser)
    search_parser.add_argument("--search", action="append", dest="searches")
    add_keyword_args(search_parser)
    add_ad_hoc_search_args(search_parser)
    search_parser.add_argument("--dry-run", action="store_true")
    search_parser.add_argument("--top", type=int, default=10)
    search_parser.set_defaults(func=cmd_search)

    rank_parser = subparsers.add_parser("rank", help="show ranked jobs from storage")
    add_config_data_args(rank_parser)
    rank_parser.add_argument("--limit", type=int, default=25)
    rank_parser.add_argument("--min-score", type=float, default=0)
    rank_parser.add_argument("--status")
    rank_parser.add_argument("--published-from")
    rank_parser.add_argument("--published-to")
    rank_parser.add_argument("--work-mode", choices=["remote", "hybrid", "office", "unknown"])
    rank_parser.add_argument("--seniority", choices=["intern", "junior", "mid", "senior", "unknown"])
    rank_parser.add_argument("--min-salary", type=float)
    rank_parser.add_argument("--recommendation", choices=["priority", "strong", "review", "low"])
    rank_parser.add_argument("--min-cv-matches", type=int)
    rank_parser.add_argument("--max-cv-gaps", type=int)
    rank_parser.add_argument("--no-negative", action="store_true")
    rank_parser.add_argument(
        "--sort",
        choices=["score", "newest", "salary", "company"],
        default="score",
    )
    rank_parser.add_argument("--csv")
    add_keyword_args(rank_parser)
    add_exclude_args(rank_parser)
    rank_parser.set_defaults(func=cmd_rank)

    track_parser = subparsers.add_parser("track", help="update application status")
    add_config_data_args(track_parser)
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
    add_config_data_args(brief_parser)
    brief_parser.add_argument("job_id")
    brief_parser.add_argument("--out")
    brief_parser.set_defaults(func=cmd_brief)

    ui_parser = subparsers.add_parser("ui", help="start the local web UI")
    add_config_data_args(ui_parser)
    ui_parser.add_argument("--host", default="127.0.0.1")
    ui_parser.add_argument("--port", type=int, default=8765)
    ui_parser.set_defaults(func=cmd_ui)

    return parser


def add_config_data_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--data", dest="data")
    parser.add_argument("--lake", dest="data", help=argparse.SUPPRESS)


def add_keyword_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--keyword", action="append", default=[])
    parser.add_argument("--keywords", nargs="+", default=[])
    parser.add_argument("--related-to", action="append", default=[])
    parser.add_argument("--match", choices=["any", "all"], default="any")


def add_exclude_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--exclude-keyword", action="append", default=[])
    parser.add_argument("--exclude-keywords", nargs="+", default=[])
    parser.add_argument(
        "--exclude-scope",
        choices=["all", "title", "content"],
        default="all",
    )


def add_ad_hoc_search_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name")
    parser.add_argument("--site", action="append", dest="sites")
    parser.add_argument("--location")
    parser.add_argument("--country", default="Portugal")
    parser.add_argument("--results", type=int)
    parser.add_argument("--hours-old", type=int)
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--job-type")
    parser.add_argument("--no-linkedin-description", action="store_true")


def cmd_init(args) -> int:
    path = write_example_config(args.config, force=args.force)
    print(f"Wrote {path}")
    print(f"Workspace ready at {Path(path).parent}")
    return 0


def cmd_cv(args) -> int:
    config_path = Path(args.config)
    workspace = config_path.parent
    ensure_workspace_files(config_path)
    source = Path(args.input) if args.input else workspace / "cv.pdf"
    target = Path(args.out) if args.out else workspace / "cv.md"
    path = convert_resume_to_markdown(source, target)
    profile_path = (
        Path(args.profile_out) if args.profile_out else workspace / "cv_profile.json"
    )
    written_profile = write_resume_profile(path, profile_path)
    print(f"Wrote {path}")
    print(f"Wrote {written_profile}")
    return 0


def cmd_search(args) -> int:
    config = load_config(args.config)
    ad_hoc_search = build_ad_hoc_search_spec(args, config)
    results = run_searches(
        config,
        search_names=args.searches,
        search_specs=[ad_hoc_search] if ad_hoc_search else None,
        lake_path=args.data,
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
    lake_path = config.resolve_storage_path(args.data)
    keyword_terms = collect_keyword_terms(args, config)
    exclude_terms = collect_exclude_terms(args)
    fetch_limit = 100000 if keyword_terms or exclude_terms else args.limit
    rows = list_ranked_jobs(
        lake_path,
        limit=fetch_limit,
        min_score=args.min_score,
        status=args.status,
        published_from=args.published_from,
        published_to=args.published_to,
        work_mode=args.work_mode,
        seniority=args.seniority,
        min_salary=args.min_salary,
        recommendation=args.recommendation,
        min_cv_matches=args.min_cv_matches,
        max_cv_gaps=args.max_cv_gaps,
        no_negative=args.no_negative,
        sort_by=args.sort,
    )
    if keyword_terms:
        rows = filter_rows_by_terms(rows, keyword_terms, match=args.match)
    if exclude_terms:
        rows = filter_rows_excluding_terms(
            rows, exclude_terms, scope=args.exclude_scope
        )
    if keyword_terms or exclude_terms:
        rows = rows[: args.limit]
    if args.csv:
        path = export_ranked_csv(rows, args.csv)
        print(f"Wrote {path}")
    else:
        _print_rows(rows)
    return 0


def build_ad_hoc_search_spec(args, config) -> SearchSpec | None:
    keywords = collect_raw_keywords(args)
    related_to = unique_terms(args.related_to)
    if not keywords and not related_to:
        return None
    query, focus_terms = build_keyword_query(
        keywords=keywords,
        related_to=related_to,
        groups=config.related_keyword_groups,
        match=args.match,
    )
    required_keywords = keywords if args.match == "all" else []
    name_parts = [*(keywords[:3]), *(related_to[:2])]
    name = args.name or f"ad-hoc-{'-'.join(name_parts) or 'keywords'}"
    return SearchSpec(
        name=name,
        search_term=query,
        location=args.location or config.profile.base_location or "Portugal",
        site_name=args.sites or ["indeed", "linkedin"],
        results_wanted=args.results or 50,
        hours_old=args.hours_old if args.hours_old is not None else 168,
        country_indeed=args.country,
        is_remote=args.remote,
        job_type=args.job_type,
        description_format="plain",
        linkedin_fetch_description=not args.no_linkedin_description,
        focus_keywords=focus_terms,
        required_keywords=required_keywords,
        related_to=related_to,
    )


def collect_keyword_terms(args, config) -> list[str]:
    return unique_terms(
        [
            *collect_raw_keywords(args),
            *expand_related_topics(args.related_to, config.related_keyword_groups),
        ]
    )


def collect_raw_keywords(args) -> list[str]:
    return unique_terms([*args.keyword, *args.keywords])


def collect_exclude_terms(args) -> list[str]:
    return unique_terms([*args.exclude_keyword, *args.exclude_keywords])


def cmd_track(args) -> int:
    config = load_config(args.config)
    lake_path = config.resolve_storage_path(args.data)
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
    lake_path = config.resolve_storage_path(args.data)
    row = get_job_with_latest_score(lake_path, args.job_id)
    if row is None:
        raise SystemExit(f"No job found with id {args.job_id}")
    out_path = args.out or f"briefs/{args.job_id}.md"
    path = write_application_brief(dict(row), config.profile, out_path)
    print(f"Wrote {path}")
    return 0


def cmd_ui(args) -> int:
    config_path = Path(args.config)
    if not config_path.exists():
        write_example_config(config_path)
    run_ui_server(
        config_path=args.config,
        data_path=args.data,
        host=args.host,
        port=args.port,
    )
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
