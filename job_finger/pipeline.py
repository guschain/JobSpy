from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

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
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
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
        results.append(
            _run_single_search(
                config,
                search,
                lake,
                dry_run=dry_run,
                progress_callback=progress_callback,
            )
        )
    return results


def _run_single_search(
    config: JobFingerConfig,
    search: SearchSpec,
    lake: JobLake | None,
    dry_run: bool,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> SearchResult:
    _emit_progress(
        progress_callback,
        stage="prepare",
        message=f"Preparing search '{search.name}'",
        percent=5,
        search_name=search.name,
        location=search.location,
        sites=search.site_name,
        results_wanted=search.results_wanted,
    )
    scrape_jobs = _load_scraper()
    _emit_progress(
        progress_callback,
        stage="scrape",
        message=(
            f"Contacting {', '.join(search.site_name)} for "
            f"{search.results_wanted} result(s) in {search.location}"
        ),
        percent=15,
        search_name=search.name,
    )
    dataframe = scrape_jobs(**search.to_scrape_kwargs())
    records = (
        dataframe.to_dict(orient="records") if hasattr(dataframe, "to_dict") else []
    )
    _emit_progress(
        progress_callback,
        stage="scraped",
        message=f"Received {len(records)} job(s) from the board response",
        percent=50,
        search_name=search.name,
        total_scraped=len(records),
    )

    ranked: list[RankedJob] = []
    total_records = len(records)
    for index, record in enumerate(records, start=1):
        breakdown = score_job(
            record,
            config.profile,
            search_focus_keywords=search.focus_keywords,
            search_required_keywords=search.required_keywords,
        )
        job_id = job_fingerprint(record)
        ranked.append(RankedJob(job_id=job_id, job=record, score=breakdown))
        if index == total_records or index == 1 or index % 10 == 0:
            percent = 55 if total_records == 0 else 55 + round((index / total_records) * 30)
            _emit_progress(
                progress_callback,
                stage="scoring",
                message=f"Scored {index} of {total_records} job(s)",
                percent=min(percent, 85),
                search_name=search.name,
                scored=index,
                total_scraped=total_records,
            )

    ranked.sort(key=lambda item: item.score.score, reverse=True)
    run_id = None
    stored_count = 0
    if lake is not None:
        _emit_progress(
            progress_callback,
            stage="saving",
            message=f"Saving and deduping {len(ranked)} ranked job(s)",
            percent=90,
            search_name=search.name,
            total_scraped=len(records),
        )
        run_id = lake.save_search_result(
            search_name=search.name,
            search_term=search.search_term,
            location=search.location,
            sites=search.site_name,
            ranked_jobs=ranked,
        )
        stored_count = len(ranked)

    _emit_progress(
        progress_callback,
        stage="complete",
        message=f"Stored {stored_count} job(s)",
        percent=100,
        search_name=search.name,
        run_id=run_id,
        total_scraped=len(records),
        total_stored=stored_count,
    )
    return SearchResult(
        search_name=search.name,
        run_id=run_id,
        total_scraped=len(records),
        total_stored=stored_count,
        ranked_jobs=ranked,
    )


def _emit_progress(
    progress_callback: Callable[[dict[str, Any]], None] | None,
    **event: Any,
) -> None:
    if progress_callback is None:
        return
    progress_callback(event)


def _load_scraper():
    from jobspy import scrape_jobs

    return scrape_jobs
