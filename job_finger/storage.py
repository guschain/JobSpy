from __future__ import annotations

import csv
import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


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
    """Small local data lake using JSONL snapshots and append-only logs."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.raw_dir = self.root / "raw" / "search_runs"
        self.curated_dir = self.root / "curated"
        self.applications_dir = self.root / "applications"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for directory in (self.raw_dir, self.curated_dir, self.applications_dir):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def ranked_snapshot_path(self) -> Path:
        return self.curated_dir / "ranked_jobs_latest.jsonl"

    @property
    def ranked_csv_path(self) -> Path:
        return self.curated_dir / "ranked_jobs_latest.csv"

    @property
    def application_events_path(self) -> Path:
        return self.applications_dir / "application_events.jsonl"

    @property
    def applications_latest_path(self) -> Path:
        return self.applications_dir / "applications_latest.json"

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
        date_part = now[:10]
        safe_search_name = _safe_path_part(search_name)
        run_dir = self.raw_dir / date_part
        run_dir.mkdir(parents=True, exist_ok=True)
        raw_path = run_dir / f"{now.replace(':', '')}_{safe_search_name}_{run_id}.jsonl"
        manifest_path = raw_path.with_suffix(".manifest.json")

        snapshot_updates: list[dict[str, Any]] = []
        with raw_path.open("w", encoding="utf-8") as raw_file:
            for item in ranked_items:
                job = dict(item.job)
                job_id = item.job_id
                score = item.score.to_dict()
                raw_record = {
                    "run_id": run_id,
                    "search_name": search_name,
                    "scraped_at": now,
                    "job_id": job_id,
                    "job": _clean_value(job),
                }
                raw_file.write(_to_json(raw_record) + "\n")
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

        manifest = {
            "run_id": run_id,
            "search_name": search_name,
            "search_term": search_term,
            "location": location,
            "sites": list(sites),
            "started_at": now,
            "completed_at": utc_now(),
            "raw_path": _relative_to_root(raw_path, self.root),
            "total_scraped": len(snapshot_updates),
        }
        manifest_path.write_text(_to_json(manifest) + "\n", encoding="utf-8")
        self._merge_ranked_snapshot(snapshot_updates)
        return run_id

    def _merge_ranked_snapshot(self, updates: Iterable[dict[str, Any]]) -> None:
        applications = self.load_applications_latest()
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
        _write_jsonl(self.ranked_snapshot_path, rows)
        export_ranked_csv(rows, self.ranked_csv_path)

    def read_ranked_snapshot(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.ranked_snapshot_path)

    def load_applications_latest(self) -> dict[str, dict[str, Any]]:
        if not self.applications_latest_path.exists():
            return {}
        with self.applications_latest_path.open("r", encoding="utf-8-sig") as file:
            data = json.load(file)
        return {str(key): value for key, value in data.items()}

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

        latest = self.load_applications_latest()
        existing = latest.get(job_id, {})
        latest[job_id] = {
            **existing,
            "job_id": job_id,
            "status": status,
            "last_updated_at": now,
            "notes": notes if notes is not None else existing.get("notes"),
            "applied_at": applied_at if applied_at is not None else existing.get("applied_at"),
            "next_action_at": next_action_at
            if next_action_at is not None
            else existing.get("next_action_at"),
            "resume_version": resume_version
            if resume_version is not None
            else existing.get("resume_version"),
            "cover_letter_path": cover_letter_path
            if cover_letter_path is not None
            else existing.get("cover_letter_path"),
            "contact_name": contact_name
            if contact_name is not None
            else existing.get("contact_name"),
            "contact_email": contact_email
            if contact_email is not None
            else existing.get("contact_email"),
        }
        self.applications_latest_path.write_text(
            _to_json(latest) + "\n", encoding="utf-8"
        )
        self._merge_ranked_snapshot([])


def list_ranked_jobs(
    lake_path: str | Path,
    limit: int = 25,
    min_score: float = 0,
    status: str | None = None,
) -> list[dict[str, Any]]:
    lake = JobLake(lake_path)
    applications = lake.load_applications_latest()
    rows: list[dict[str, Any]] = []
    for row in lake.read_ranked_snapshot():
        row = dict(row)
        application = applications.get(str(row.get("job_id")), {})
        row["application_status"] = application.get(
            "status", row.get("application_status", "new")
        )
        if float(row.get("score") or 0) < min_score:
            continue
        if status and row["application_status"] != status:
            continue
        rows.append(row)
    rows.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
    return rows[:limit]


def get_job_with_latest_score(
    lake_path: str | Path, job_id: str
) -> dict[str, Any] | None:
    for row in list_ranked_jobs(lake_path, limit=100000):
        if str(row.get("job_id")) == job_id:
            return row
    return None


def update_application(lake_path: str | Path, **kwargs: Any) -> None:
    JobLake(lake_path).update_application(**kwargs)


def export_ranked_csv(rows: Iterable[Mapping[str, Any]], path: str | Path) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
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
        "min_amount": clean_job.get("min_amount"),
        "max_amount": clean_job.get("max_amount"),
        "currency": clean_job.get("currency"),
        "is_remote": clean_job.get("is_remote"),
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


def _relative_to_root(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _safe_path_part(value: str) -> str:
    cleaned = "".join(char if char.isalnum() else "-" for char in value.lower())
    return "-".join(part for part in cleaned.split("-") if part)[:80] or "search"
