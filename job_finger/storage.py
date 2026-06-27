from __future__ import annotations

import csv
import hashlib
import json
import math
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from job_finger.matching import normalize_job_fields
from job_finger.resume import normalize_text
from job_finger.search_terms import unique_terms


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_run_id() -> str:
    return str(uuid.uuid4())


def job_fingerprint(job: Mapping[str, Any]) -> str:
    source_id = _clean_value(job.get("id"))
    if source_id:
        return str(source_id)
    url = _clean_value(job.get("job_url") or job.get("job_url_direct"))
    if url:
        return hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:24]
    fallback = "|".join(
        str(_clean_value(job.get(name)) or "")
        for name in ("site", "title", "company", "location")
    )
    return hashlib.sha256(fallback.encode("utf-8")).hexdigest()[:24]


class JobLake:
    """Flat local job data store with three human-readable files."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def scrapes_path(self) -> Path:
        return self.root / "scrapes.jsonl"

    @property
    def jobs_path(self) -> Path:
        return self.root / "jobs.jsonl"

    @property
    def application_events_path(self) -> Path:
        return self.root / "applications.jsonl"

    @property
    def feedback_path(self) -> Path:
        return self.root / "feedback.jsonl"

    def save_search_result(
        self,
        *,
        search_name: str,
        search_term: str,
        location: str,
        sites: Iterable[str],
        ranked_jobs: Iterable[Any],
    ) -> str:
        run_id = new_run_id()
        now = utc_now()
        ranked_items = list(ranked_jobs)
        run_record = {
            "record_type": "run",
            "run_id": run_id,
            "search_name": search_name,
            "search_term": search_term,
            "location": location,
            "sites": list(sites),
            "started_at": now,
            "completed_at": utc_now(),
            "total_scraped": len(ranked_items),
        }

        snapshot_updates: list[dict[str, Any]] = []
        with self.scrapes_path.open("a", encoding="utf-8") as file:
            file.write(_to_json(run_record) + "\n")
            for item in ranked_items:
                job = dict(item.job)
                job_id = item.job_id
                score = item.score.to_dict()
                file.write(
                    _to_json(
                        {
                            "record_type": "job",
                            "run_id": run_id,
                            "search_name": search_name,
                            "scraped_at": now,
                            "job_id": job_id,
                            "job": _clean_value(job),
                            "score": score,
                        }
                    )
                    + "\n"
                )
                snapshot_updates.append(
                    _snapshot_record(
                        job_id=job_id,
                        job=job,
                        score=score,
                        run_id=run_id,
                        search_name=search_name,
                        seen_at=now,
                    )
                )

        self._merge_job_snapshot(snapshot_updates)
        return run_id

    def _merge_job_snapshot(self, updates: Iterable[dict[str, Any]]) -> None:
        applications = self.load_application_state()
        latest: dict[str, dict[str, Any]] = {
            str(row["job_id"]): row for row in self.read_ranked_snapshot()
        }
        for row in updates:
            latest[str(row["job_id"])] = row
        for job_id, row in latest.items():
            application = applications.get(job_id, {})
            row["application_status"] = application.get("status", "new")
            row["next_action_at"] = application.get("next_action_at")
            row["application_notes"] = application.get("notes")
            row["applied_at"] = application.get("applied_at")
        rows = sorted(
            latest.values(),
            key=lambda row: (
                -float(row.get("score") or 0),
                str(row.get("date_posted") or ""),
                str(row.get("company") or ""),
            ),
        )
        _write_jsonl(self.jobs_path, rows)

    def read_ranked_snapshot(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.jobs_path)

    def load_application_state(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for event in _read_jsonl(self.application_events_path):
            job_id = str(event.get("job_id") or "")
            if not job_id:
                continue
            existing = latest.get(job_id, {})
            latest[job_id] = {
                **existing,
                **{key: value for key, value in event.items() if value is not None},
            }
        return latest

    def update_application(
        self,
        *,
        job_id: str,
        status: str,
        notes: str | None = None,
        applied_at: str | None = None,
        next_action_at: str | None = None,
        resume_version: str | None = None,
        cover_letter_path: str | None = None,
        contact_name: str | None = None,
        contact_email: str | None = None,
    ) -> None:
        now = utc_now()
        if status == "applied" and applied_at is None:
            applied_at = now
        event = {
            "event_id": str(uuid.uuid4()),
            "job_id": job_id,
            "status": status,
            "notes": notes,
            "applied_at": applied_at,
            "next_action_at": next_action_at,
            "resume_version": resume_version,
            "cover_letter_path": cover_letter_path,
            "contact_name": contact_name,
            "contact_email": contact_email,
            "updated_at": now,
        }
        with self.application_events_path.open("a", encoding="utf-8") as file:
            file.write(_to_json(event) + "\n")
        self._merge_job_snapshot([])

    def add_feedback(
        self,
        *,
        job_id: str,
        negative_terms: Iterable[str] | None = None,
        notes: str | None = None,
        apply_globally: bool = True,
    ) -> None:
        event = {
            "event_id": str(uuid.uuid4()),
            "job_id": job_id,
            "negative_terms": unique_terms(negative_terms or []),
            "notes": notes,
            "apply_globally": apply_globally,
            "updated_at": utc_now(),
        }
        with self.feedback_path.open("a", encoding="utf-8") as file:
            file.write(_to_json(event) + "\n")

    def learned_negative_terms(self) -> list[str]:
        terms: list[str] = []
        for event in _read_jsonl(self.feedback_path):
            if event.get("apply_globally", True):
                terms.extend(str(term) for term in event.get("negative_terms") or [])
        return unique_terms(terms)

    def rescore_snapshot(self, profile: Any) -> int:
        from job_finger.scoring import score_job

        now = utc_now()
        updates: list[dict[str, Any]] = []
        for row in self.read_ranked_snapshot():
            job_id = str(row.get("job_id") or "")
            if not job_id:
                continue
            raw_job = dict(row.get("raw_job") or {})
            if not raw_job:
                raw_job = _job_from_snapshot(row)
            score = score_job(raw_job, profile).to_dict()
            updates.append(
                _snapshot_record(
                    job_id=job_id,
                    job=raw_job,
                    score=score,
                    run_id=str(row.get("run_id") or "rescore"),
                    search_name=str(row.get("search_name") or "rescore"),
                    seen_at=str(row.get("last_seen_at") or now),
                )
            )
        self._merge_job_snapshot(updates)
        return len(updates)


def list_ranked_jobs(
    data_path: str | Path,
    limit: int = 25,
    min_score: float = 0,
    status: str | None = None,
    published_from: str | date | None = None,
    published_to: str | date | None = None,
    work_mode: str | None = None,
    seniority: str | None = None,
    min_salary: float | None = None,
    recommendation: str | None = None,
    min_cv_matches: int | None = None,
    max_cv_gaps: int | None = None,
    no_negative: bool = False,
    sort_by: str = "score",
) -> list[dict[str, Any]]:
    store = JobLake(data_path)
    applications = store.load_application_state()
    learned_negative_terms = store.learned_negative_terms()
    published_from_date = _parse_date(published_from)
    published_to_date = _parse_date(published_to)
    rows: list[dict[str, Any]] = []
    for row in store.read_ranked_snapshot():
        row = dict(row)
        application = applications.get(str(row.get("job_id")), {})
        row["application_status"] = application.get(
            "status", row.get("application_status", "new")
        )
        learned_matches = _matched_learned_terms(row, learned_negative_terms)
        if learned_matches:
            row["learned_negative_keywords"] = learned_matches
            row["negative_keywords"] = unique_terms(
                [*(row.get("negative_keywords") or []), *learned_matches]
            )
        if float(row.get("score") or 0) < min_score:
            continue
        if status and row["application_status"] != status:
            continue
        posted_date = _parse_date(row.get("date_posted"))
        if published_from_date and (
            posted_date is None or posted_date < published_from_date
        ):
            continue
        if published_to_date and (
            posted_date is None or posted_date > published_to_date
        ):
            continue
        if work_mode and str(row.get("work_mode") or "") != work_mode:
            continue
        if seniority and str(row.get("seniority") or "") != seniority:
            continue
        if min_salary is not None and (_best_salary(row) or 0) < min_salary:
            continue
        if recommendation and str(row.get("recommendation") or "") != recommendation:
            continue
        if min_cv_matches is not None and _list_count(row.get("cv_matched_keywords")) < min_cv_matches:
            continue
        if max_cv_gaps is not None and _list_count(row.get("cv_missing_keywords")) > max_cv_gaps:
            continue
        if no_negative and _list_count(row.get("negative_keywords")) > 0:
            continue
        rows.append(row)
    rows.sort(key=_sort_key(sort_by))
    return rows[:limit]


def get_job_with_latest_score(
    data_path: str | Path, job_id: str
) -> dict[str, Any] | None:
    for row in list_ranked_jobs(data_path, limit=100000):
        if str(row.get("job_id")) == job_id:
            return row
    return None


def update_application(data_path: str | Path, **kwargs: Any) -> None:
    JobLake(data_path).update_application(**kwargs)


def add_feedback(data_path: str | Path, **kwargs: Any) -> None:
    JobLake(data_path).add_feedback(**kwargs)


def learned_negative_terms(data_path: str | Path) -> list[str]:
    return JobLake(data_path).learned_negative_terms()


def rescore_ranked_jobs(data_path: str | Path, profile: Any) -> int:
    return JobLake(data_path).rescore_snapshot(profile)


def list_application_events(
    data_path: str | Path, job_id: str | None = None
) -> list[dict[str, Any]]:
    events = _read_jsonl(JobLake(data_path).application_events_path)
    if job_id is None:
        return events
    return [event for event in events if str(event.get("job_id")) == str(job_id)]


def export_ranked_csv(rows: Iterable[Mapping[str, Any]], path: str | Path) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    flat_rows = [_flatten_for_csv(row) for row in rows]
    with output_path.open("w", newline="", encoding="utf-8") as file:
        fieldnames = list(flat_rows[0].keys()) if flat_rows else []
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(flat_rows)
    return output_path


def _snapshot_record(
    *,
    job_id: str,
    job: Mapping[str, Any],
    score: Mapping[str, Any],
    run_id: str,
    search_name: str,
    seen_at: str,
) -> dict[str, Any]:
    clean_job = _clean_value(dict(job))
    analysis = dict(score.get("analysis") or {})
    normalized = {
        **normalize_job_fields(clean_job),
        **dict(analysis.get("normalized") or {}),
    }
    return {
        "job_id": job_id,
        "run_id": run_id,
        "search_name": search_name,
        "last_seen_at": seen_at,
        "site": clean_job.get("site"),
        "job_url": clean_job.get("job_url"),
        "job_url_direct": clean_job.get("job_url_direct"),
        "title": clean_job.get("title"),
        "company": clean_job.get("company"),
        "location": clean_job.get("location"),
        "date_posted": clean_job.get("date_posted"),
        "job_type": clean_job.get("job_type"),
        "interval": normalized.get("salary_interval") or clean_job.get("interval"),
        "min_amount": clean_job.get("min_amount"),
        "max_amount": clean_job.get("max_amount"),
        "currency": normalized.get("salary_currency") or clean_job.get("currency"),
        "salary_source": normalized.get("salary_source"),
        "salary_min": normalized.get("salary_min"),
        "salary_max": normalized.get("salary_max"),
        "salary_label": normalized.get("salary_label"),
        "is_remote": clean_job.get("is_remote"),
        "work_mode": normalized.get("work_mode"),
        "seniority": normalized.get("seniority"),
        "employment_type": normalized.get("employment_type"),
        "description": clean_job.get("description"),
        "company_industry": clean_job.get("company_industry"),
        "score": score.get("score"),
        "estimated_fit_probability": score.get("estimated_fit_probability"),
        "recommendation": score.get("recommendation"),
        "components": score.get("components", {}),
        "matched_keywords": score.get("matched_keywords", []),
        "missing_must_haves": score.get("missing_must_haves", []),
        "penalties": score.get("penalties", []),
        "reasons": score.get("reasons", []),
        "skills": analysis.get("job_skills", []),
        "cv_matched_keywords": analysis.get("cv_matched_keywords", []),
        "cv_missing_keywords": analysis.get("cv_missing_keywords", []),
        "cv_evidence": analysis.get("cv_evidence", []),
        "cv_match_strength": analysis.get("cv_match_strength"),
        "positive_keywords": analysis.get("positive_keywords", []),
        "negative_keywords": analysis.get("negative_keywords", []),
        "match_explanation": analysis.get("match_explanation", []),
        "application_suggestions": analysis.get("application_suggestions", []),
        "cover_letter_keywords": analysis.get("cover_letter_keywords", []),
        "cover_letter_draft": analysis.get("cover_letter_draft"),
        "analysis": analysis,
        "raw_job": clean_job,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8-sig") as file:
        for line in file:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _job_from_snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("job_id"),
        "site": row.get("site"),
        "job_url": row.get("job_url"),
        "job_url_direct": row.get("job_url_direct"),
        "title": row.get("title"),
        "company": row.get("company"),
        "location": row.get("location"),
        "description": row.get("description"),
        "date_posted": row.get("date_posted"),
        "job_type": row.get("job_type"),
        "interval": row.get("interval"),
        "min_amount": row.get("min_amount"),
        "max_amount": row.get("max_amount"),
        "currency": row.get("currency"),
        "is_remote": row.get("is_remote"),
        "company_industry": row.get("company_industry"),
    }


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(_to_json(row) + "\n")


def _to_json(value: Any) -> str:
    return json.dumps(_clean_value(value), sort_keys=True, default=str)


def _clean_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _clean_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean_value(item) for item in value]
    if isinstance(value, tuple):
        return [_clean_value(item) for item in value]
    return value


def _sort_key(sort_by: str):
    if sort_by == "newest":
        return lambda row: (
            -(_parse_date(row.get("date_posted")) or date.min).toordinal(),
            -float(row.get("score") or 0),
        )
    if sort_by == "salary":
        return lambda row: (
            -(_best_salary(row) or 0),
            -float(row.get("score") or 0),
        )
    if sort_by == "company":
        return lambda row: (
            str(row.get("company") or "").lower(),
            -float(row.get("score") or 0),
        )
    return lambda row: (-float(row.get("score") or 0), str(row.get("company") or ""))


def _best_salary(row: Mapping[str, Any]) -> float | None:
    values = [
        _safe_float(row.get("salary_max")),
        _safe_float(row.get("salary_min")),
        _safe_float(row.get("max_amount")),
        _safe_float(row.get("min_amount")),
    ]
    usable = [value for value in values if value is not None]
    return max(usable) if usable else None


def _list_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if value is None or value == "":
        return 0
    return 1


def _matched_learned_terms(
    row: Mapping[str, Any], learned_terms: Iterable[str]
) -> list[str]:
    text = normalize_text(
        " ".join(
            str(row.get(field) or "")
            for field in ("title", "company", "location", "description", "raw_job")
        )
    )
    matched = []
    for term in learned_terms:
        normalized = normalize_text(term)
        if normalized and normalized in text:
            matched.append(term)
    return unique_terms(matched)


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _flatten_for_csv(row: Mapping[str, Any]) -> dict[str, Any]:
    skipped = {"raw_job", "description"}
    flat: dict[str, Any] = {}
    for key, value in row.items():
        if key in skipped:
            continue
        if isinstance(value, (list, dict)):
            flat[key] = json.dumps(value, sort_keys=True)
        else:
            flat[key] = value
    return flat
