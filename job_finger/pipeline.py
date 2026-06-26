from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from job_finger.config import JobFingerConfig, SearchSpec
from job_finger.scoring import ScoreBreakdown, score_job
from job_finger.storage import (
    connect,
    create_search_run,
    finish_search_run,
    save_score,
    upsert_job,
)


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
    db_path: str | Path | None = None,
    dry_run: bool = False,
) -> list[SearchResult]:
    connection = None if dry_run else connect(config.resolve_storage_path(str(db_path) if db_path else None))
    try:
        results = []
        for search in config.selected_searches(search_names):
            results.append(_run_single_search(config, search, connection, dry_run=dry_run))
        return results
    finally:
        if connection is not None:
            connection.close()


def _run_single_search(
    config: JobFingerConfig,
    search: SearchSpec,
    connection,
    dry_run: bool,
) -> SearchResult:
    scrape_jobs = _load_scraper()
    dataframe = scrape_jobs(**search.to_scrape_kwargs())
    records = dataframe.to_dict(orient="records") if hasattr(dataframe, "to_dict") else []
    run_id = None
    if connection is not None:
        run_id = create_search_run(
            connection,
            search_name=search.name,
            search_term=search.search_term,
            location=search.location,
            sites=search.site_name,
        )

    ranked: list[RankedJob] = []
    stored_count = 0
    for record in records:
        breakdown = score_job(record, config.profile)
        if connection is not None:
            job_id = upsert_job(connection, record)
            save_score(connection, job_id, run_id, breakdown)
            stored_count += 1
        else:
            job_id = str(record.get("id") or record.get("job_url") or len(ranked) + 1)
        ranked.append(RankedJob(job_id=job_id, job=record, score=breakdown))

    ranked.sort(key=lambda item: item.score.score, reverse=True)
    if connection is not None and run_id:
        finish_search_run(connection, run_id, len(records), stored_count)

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
