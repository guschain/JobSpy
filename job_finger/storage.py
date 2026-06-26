from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from job_finger.scoring import ScoreBreakdown


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    ensure_schema(connection)
    return connection


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS search_runs (
            id TEXT PRIMARY KEY,
            search_name TEXT NOT NULL,
            search_term TEXT NOT NULL,
            location TEXT,
            sites_json TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            total_scraped INTEGER DEFAULT 0,
            total_upserted INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            source_id TEXT,
            site TEXT,
            job_url TEXT,
            job_url_direct TEXT,
            title TEXT,
            company TEXT,
            location TEXT,
            date_posted TEXT,
            job_type TEXT,
            min_amount REAL,
            max_amount REAL,
            currency TEXT,
            is_remote INTEGER,
            description TEXT,
            company_industry TEXT,
            raw_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(job_url);
        CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);

        CREATE TABLE IF NOT EXISTS job_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            run_id TEXT,
            score REAL NOT NULL,
            estimated_fit_probability INTEGER NOT NULL,
            recommendation TEXT NOT NULL,
            components_json TEXT NOT NULL,
            matched_keywords_json TEXT NOT NULL,
            missing_must_haves_json TEXT NOT NULL,
            penalties_json TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES jobs(job_id),
            FOREIGN KEY(run_id) REFERENCES search_runs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_job_scores_job_id ON job_scores(job_id);
        CREATE INDEX IF NOT EXISTS idx_job_scores_score ON job_scores(score);

        CREATE TABLE IF NOT EXISTS applications (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'saved',
            applied_at TEXT,
            last_updated_at TEXT NOT NULL,
            next_action_at TEXT,
            resume_version TEXT,
            cover_letter_path TEXT,
            contact_name TEXT,
            contact_email TEXT,
            notes TEXT,
            FOREIGN KEY(job_id) REFERENCES jobs(job_id)
        );

        CREATE TABLE IF NOT EXISTS application_documents (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            path TEXT NOT NULL,
            content_hash TEXT,
            created_at TEXT NOT NULL,
            metadata_json TEXT,
            FOREIGN KEY(job_id) REFERENCES jobs(job_id)
        );
        """
    )
    connection.commit()


def new_run_id() -> str:
    return str(uuid.uuid4())


def create_search_run(
    connection: sqlite3.Connection,
    search_name: str,
    search_term: str,
    location: str,
    sites: Iterable[str],
) -> str:
    run_id = new_run_id()
    connection.execute(
        """
        INSERT INTO search_runs (
            id, search_name, search_term, location, sites_json, started_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, search_name, search_term, location, json.dumps(list(sites)), utc_now()),
    )
    connection.commit()
    return run_id


def finish_search_run(
    connection: sqlite3.Connection,
    run_id: str,
    total_scraped: int,
    total_upserted: int,
) -> None:
    connection.execute(
        """
        UPDATE search_runs
        SET completed_at = ?, total_scraped = ?, total_upserted = ?
        WHERE id = ?
        """,
        (utc_now(), total_scraped, total_upserted, run_id),
    )
    connection.commit()


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


def upsert_job(connection: sqlite3.Connection, job: Mapping[str, Any]) -> str:
    job_id = job_fingerprint(job)
    now = utc_now()
    clean = {key: _clean_value(value) for key, value in dict(job).items()}
    connection.execute(
        """
        INSERT INTO jobs (
            job_id, source_id, site, job_url, job_url_direct, title, company,
            location, date_posted, job_type, min_amount, max_amount, currency,
            is_remote, description, company_industry, raw_json, first_seen_at,
            last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            site = excluded.site,
            job_url = excluded.job_url,
            job_url_direct = excluded.job_url_direct,
            title = excluded.title,
            company = excluded.company,
            location = excluded.location,
            date_posted = excluded.date_posted,
            job_type = excluded.job_type,
            min_amount = excluded.min_amount,
            max_amount = excluded.max_amount,
            currency = excluded.currency,
            is_remote = excluded.is_remote,
            description = excluded.description,
            company_industry = excluded.company_industry,
            raw_json = excluded.raw_json,
            last_seen_at = excluded.last_seen_at
        """,
        (
            job_id,
            clean.get("id"),
            clean.get("site"),
            clean.get("job_url"),
            clean.get("job_url_direct"),
            clean.get("title"),
            clean.get("company"),
            clean.get("location"),
            clean.get("date_posted"),
            clean.get("job_type"),
            _safe_float(clean.get("min_amount")),
            _safe_float(clean.get("max_amount")),
            clean.get("currency"),
            _int_bool(clean.get("is_remote")),
            clean.get("description"),
            clean.get("company_industry"),
            json.dumps(clean, sort_keys=True, default=str),
            now,
            now,
        ),
    )
    connection.commit()
    return job_id


def save_score(
    connection: sqlite3.Connection,
    job_id: str,
    run_id: str | None,
    breakdown: ScoreBreakdown,
) -> None:
    connection.execute(
        """
        INSERT INTO job_scores (
            job_id, run_id, score, estimated_fit_probability, recommendation,
            components_json, matched_keywords_json, missing_must_haves_json,
            penalties_json, reasons_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            run_id,
            breakdown.score,
            breakdown.estimated_fit_probability,
            breakdown.recommendation,
            json.dumps(breakdown.components, sort_keys=True),
            json.dumps(breakdown.matched_keywords),
            json.dumps(breakdown.missing_must_haves),
            json.dumps(breakdown.penalties),
            json.dumps(breakdown.reasons),
            utc_now(),
        ),
    )
    connection.commit()


def update_application(
    connection: sqlite3.Connection,
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
    existing = connection.execute(
        "SELECT job_id FROM applications WHERE job_id = ?", (job_id,)
    ).fetchone()
    if not existing:
        connection.execute(
            """
            INSERT INTO applications (
                job_id, status, applied_at, last_updated_at, next_action_at,
                resume_version, cover_letter_path, contact_name, contact_email, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                status,
                applied_at,
                utc_now(),
                next_action_at,
                resume_version,
                cover_letter_path,
                contact_name,
                contact_email,
                notes,
            ),
        )
    else:
        connection.execute(
            """
            UPDATE applications
            SET status = ?,
                applied_at = COALESCE(?, applied_at),
                last_updated_at = ?,
                next_action_at = COALESCE(?, next_action_at),
                resume_version = COALESCE(?, resume_version),
                cover_letter_path = COALESCE(?, cover_letter_path),
                contact_name = COALESCE(?, contact_name),
                contact_email = COALESCE(?, contact_email),
                notes = COALESCE(?, notes)
            WHERE job_id = ?
            """,
            (
                status,
                applied_at,
                utc_now(),
                next_action_at,
                resume_version,
                cover_letter_path,
                contact_name,
                contact_email,
                notes,
                job_id,
            ),
        )
    connection.commit()


def list_ranked_jobs(
    connection: sqlite3.Connection,
    limit: int = 25,
    min_score: float = 0,
    status: str | None = None,
) -> list[sqlite3.Row]:
    status_filter = ""
    params: list[Any] = [min_score]
    if status:
        status_filter = "AND COALESCE(a.status, 'new') = ?"
        params.append(status)
    params.append(limit)
    return list(
        connection.execute(
            f"""
            WITH latest_score AS (
                SELECT js.*
                FROM job_scores js
                JOIN (
                    SELECT job_id, MAX(id) AS id
                    FROM job_scores
                    GROUP BY job_id
                ) latest ON latest.id = js.id
            )
            SELECT
                j.job_id,
                j.site,
                j.title,
                j.company,
                j.location,
                j.date_posted,
                j.job_url,
                j.job_url_direct,
                j.min_amount,
                j.max_amount,
                j.currency,
                latest_score.score,
                latest_score.estimated_fit_probability,
                latest_score.recommendation,
                latest_score.reasons_json,
                latest_score.matched_keywords_json,
                COALESCE(a.status, 'new') AS application_status,
                a.next_action_at,
                a.notes
            FROM jobs j
            JOIN latest_score ON latest_score.job_id = j.job_id
            LEFT JOIN applications a ON a.job_id = j.job_id
            WHERE latest_score.score >= ?
            {status_filter}
            ORDER BY latest_score.score DESC, j.date_posted DESC, j.company ASC
            LIMIT ?
            """,
            params,
        )
    )


def get_job_with_latest_score(
    connection: sqlite3.Connection, job_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        """
        WITH latest_score AS (
            SELECT js.*
            FROM job_scores js
            JOIN (
                SELECT job_id, MAX(id) AS id
                FROM job_scores
                WHERE job_id = ?
            ) latest ON latest.id = js.id
        )
        SELECT
            j.*,
            latest_score.score,
            latest_score.estimated_fit_probability,
            latest_score.recommendation,
            latest_score.components_json,
            latest_score.matched_keywords_json,
            latest_score.missing_must_haves_json,
            latest_score.penalties_json,
            latest_score.reasons_json,
            COALESCE(a.status, 'new') AS application_status,
            a.notes,
            a.resume_version,
            a.cover_letter_path
        FROM jobs j
        LEFT JOIN latest_score ON latest_score.job_id = j.job_id
        LEFT JOIN applications a ON a.job_id = j.job_id
        WHERE j.job_id = ?
        """,
        (job_id, job_id),
    ).fetchone()


def export_ranked_csv(rows: Iterable[sqlite3.Row], path: str | Path) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    with output_path.open("w", newline="", encoding="utf-8") as file:
        fieldnames = list(rows[0].keys()) if rows else []
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
    return output_path


def _clean_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (list, dict)):
        return value
    return value


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


def _int_bool(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(bool(value))
    return int(str(value).strip().lower() in {"1", "true", "yes", "remote"})
