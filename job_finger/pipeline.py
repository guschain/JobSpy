from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from job_finger.config import JobFingerConfig, SearchSpec
from job_finger.scoring import ScoreBreakdown, score_job
from job_finger.storage import JobLake, job_fingerprint


@dataclass(frozen=True)
class RankedJob:
    job_id: str
    job: dict[str, Any]
    score: ScoreBreakdown


@dataclass(frozen=True)
class SearchResult:
    search_name: str
    run_id: str | None
    total_scraped: int
    total_stored: int
    ranked_jobs: list[RankedJob] = field(default_factory=list)


def run_searches(
    config: JobFingerConfig,
    search_names: list[str] | None = None,
    search_specs: list[SearchSpec] | None = None,
    lake_path: str | Path | None = None,
    dry_run: bool = False,
) -> list[SearchResult]:
    lake = None if dry_run else JobLake(
        config.resolve_storage_path(str(lake_path) if lake_path else None)
    )
    results = []
    if search_names is None:
        selected_searches = [] if search_specs else config.searches
    elif search_names:
        selected_searches = config.selected_searches(search_names)
    else:
        selected_searches = []
    searches = [*selected_searches, *(search_specs or [])]
    for search in searches:
        results.append(_run_single_search(config, search, lake, dry_run=dry_run))
    return results


def _run_single_search(
    config: JobFingerConfig,
    search: SearchSpec,
    lake: JobLake | None,
    dry_run: bool,
) -> SearchResult:
    scrape_jobs = _load_scraper()
    dataframe = scrape_jobs(**search.to_scrape_kwargs())
    records = (
        dataframe.to_dict(orient="records") if hasattr(dataframe, "to_dict") else []
    )

    ranked: list[RankedJob] = []
    for record in records:
        breakdown = score_job(
            record,
            config.profile,
            search_focus_keywords=search.focus_keywords,
            search_required_keywords=search.required_keywords,
        )
        job_id = job_fingerprint(record)
        ranked.append(RankedJob(job_id=job_id, job=record, score=breakdown))

    ranked.sort(key=lambda item: item.score.score, reverse=True)
    run_id = None
    stored_count = 0
    if lake is not None:
        run_id = lake.save_search_result(
            search_name=search.name,
            search_term=search.search_term,
            location=search.location,
            sites=search.site_name,
            ranked_jobs=ranked,
        )
        stored_count = len(ranked)

    return SearchResult(
        search_name=search.name,
        run_id=run_id,
        total_scraped=len(records),
        total_stored=stored_count,
        ranked_jobs=ranked,
    )


def _load_scraper():
    from jobspy import scrape_jobs

    return scrape_jobs
