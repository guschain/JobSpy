from __future__ import annotations

import json
import mimetypes
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from job_finger.config import (
    DEFAULT_CONFIG_PATH,
    JobFingerConfig,
    SearchSpec,
    ensure_workspace_files,
    example_config,
    load_config,
)
from job_finger.drafts import write_application_brief, write_cover_letter
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
    JobLake,
    canonical_role_family,
    get_job_with_latest_score,
    list_application_events,
    list_ranked_jobs,
    rescore_ranked_jobs,
    update_application,
    add_feedback,
    learned_negative_terms,
)


DEFAULT_OBSERVATION_TEMPLATE = """Outcome:

Fit notes:

Concerns:

Next action:
"""

ASSET_ROOT = Path(__file__).with_name("assets")
ASSET_CONTENT_TYPES = {
    ".avif": "image/avif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}


@dataclass
class UIServerContext:
    config: JobFingerConfig
    config_path: Path
    data_path: Path
    search_jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    search_lock: threading.Lock = field(default_factory=threading.Lock)


def run_ui_server(
    *,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    data_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    ensure_workspace_files(config_path)
    resolved_config_path = Path(config_path)
    config = _load_config_or_default(resolved_config_path)
    resolved_data_path = config.resolve_storage_path(str(data_path) if data_path else None)
    context = UIServerContext(
        config=config,
        config_path=resolved_config_path,
        data_path=resolved_data_path,
    )

    class Handler(JobFingerUIHandler):
        server_context = context

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Job Finger UI running at http://{host}:{port}")
    print(f"Data folder: {resolved_data_path}")
    server.serve_forever()


class JobFingerUIHandler(BaseHTTPRequestHandler):
    server_context: UIServerContext

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(INDEX_HTML)
            return
        if parsed.path.startswith("/assets/"):
            self._send_asset(parsed.path)
            return
        if parsed.path == "/api/jobs":
            self._handle_jobs(parse_qs(parsed.query))
            return
        if parsed.path.startswith("/api/jobs/"):
            job_id = unquote(parsed.path.removeprefix("/api/jobs/"))
            self._handle_job_detail(job_id)
            return
        if parsed.path == "/api/template":
            self._send_json({"template": read_observation_template(self.server_context.data_path)})
            return
        if parsed.path == "/api/profile":
            self._handle_profile()
            return
        if parsed.path.startswith("/api/searches/"):
            search_id = unquote(parsed.path.removeprefix("/api/searches/"))
            self._handle_search_status(search_id)
            return
        if parsed.path == "/api/preferences":
            self._send_json(
                {
                    "learned_negative_terms": learned_negative_terms(
                        self.server_context.data_path
                    )
                }
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/search":
            try:
                self._handle_search(self._read_json_body())
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
            return
        if parsed.path == "/api/search/start":
            try:
                self._handle_search_start(self._read_json_body())
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
            return
        if parsed.path == "/api/cv":
            self._handle_cv_update()
            return
        if parsed.path == "/api/rescore":
            self._handle_rescore()
            return
        if parsed.path == "/api/applications":
            self._handle_application_update(self._read_json_body())
            return
        if parsed.path == "/api/briefs":
            self._handle_brief(self._read_json_body())
            return
        if parsed.path == "/api/feedback":
            self._handle_feedback(self._read_json_body())
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _handle_profile(self) -> None:
        config = self._refresh_config()
        self._send_json(
            profile_status(
                config,
                self.server_context.config_path,
                self.server_context.data_path,
            )
        )

    def _handle_cv_update(self) -> None:
        workspace = self.server_context.config_path.parent
        source = workspace / "cv.pdf"
        if not source.exists():
            self._send_json({"error": f"CV PDF not found at {source}"}, status=404)
            return
        markdown_path = convert_resume_to_markdown(source, workspace / "cv.md")
        write_resume_profile(markdown_path, workspace / "cv_profile.json")
        config = self._refresh_config()
        count = rescore_ranked_jobs(self.server_context.data_path, config.profile)
        self._send_json(
            {
                "ok": True,
                "rescore_count": count,
                "profile": profile_status(
                    config,
                    self.server_context.config_path,
                    self.server_context.data_path,
                ),
            }
        )

    def _handle_rescore(self) -> None:
        config = self._refresh_config()
        count = rescore_ranked_jobs(self.server_context.data_path, config.profile)
        self._send_json(
            {
                "ok": True,
                "rescore_count": count,
                "profile": profile_status(
                    config,
                    self.server_context.config_path,
                    self.server_context.data_path,
                ),
            }
        )

    def _refresh_config(self) -> JobFingerConfig:
        self.server_context.config = _load_config_or_default(
            self.server_context.config_path
        )
        return self.server_context.config

    def _handle_jobs(self, query: dict[str, list[str]]) -> None:
        limit = _int_query(query, "limit", 100)
        min_score = _float_query(query, "min_score", 0)
        status = _first_query(query, "status") or None
        published_from = _first_query(query, "published_from") or None
        published_to = _first_query(query, "published_to") or None
        work_mode = _first_query(query, "work_mode") or None
        work_schedule = _first_query(query, "work_schedule") or None
        seniority = _first_query(query, "seniority") or None
        min_salary = _float_query(query, "min_salary", None)
        recommendation = _first_query(query, "recommendation") or None
        min_cv_matches = _optional_int_query(query, "min_cv_matches")
        max_cv_gaps = _optional_int_query(query, "max_cv_gaps")
        no_negative = _bool_query(query, "no_negative")
        sort_by = _first_query(query, "sort") or "score"
        exclude_scope = _first_query(query, "exclude_scope") or "all"
        text_query = _first_query(query, "query")
        keyword_terms = unique_terms(query.get("keyword", []))
        exclude_terms = unique_terms(query.get("exclude_keyword", []))
        related_terms = expand_related_topics(
            query.get("related_to", []), self.server_context.config.related_keyword_groups
        )
        rows = list_ranked_jobs(
            self.server_context.data_path,
            limit=100000
            if text_query or keyword_terms or related_terms or exclude_terms
            else limit,
            min_score=min_score,
            status=status,
            published_from=published_from,
            published_to=published_to,
            work_mode=work_mode,
            work_schedule=work_schedule,
            seniority=seniority,
            min_salary=min_salary,
            recommendation=recommendation,
            min_cv_matches=min_cv_matches,
            max_cv_gaps=max_cv_gaps,
            no_negative=no_negative,
            sort_by=sort_by,
        )
        terms = unique_terms([*(keyword_terms or []), *(related_terms or [])])
        if terms:
            rows = filter_rows_by_terms(rows, terms)
        if exclude_terms:
            rows = filter_rows_excluding_terms(
                rows, exclude_terms, scope=exclude_scope
            )
        if text_query:
            rows = _filter_rows_by_text(rows, text_query)
        summary = summarize_rows(rows)
        rows = rows[:limit]
        self._send_json(
            {
                "jobs": [_list_row(row) for row in rows],
                "total": summary["total"],
                "summary": summary,
                "data_path": str(self.server_context.data_path),
            }
        )

    def _handle_job_detail(self, job_id: str) -> None:
        job = get_job_with_latest_score(self.server_context.data_path, job_id)
        if not job:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        events = list_application_events(self.server_context.data_path, job_id)
        self._send_json(
            {
                "job": job,
                "events": events,
                "last_applied_at": last_applied_at(events, job),
                "observation_template": read_observation_template(
                    self.server_context.data_path
                ),
            }
        )

    def _handle_search(self, payload: dict[str, Any]) -> None:
        search_spec = build_search_spec(payload, self.server_context.config)
        results = run_searches(
            self.server_context.config,
            search_names=[],
            search_specs=[search_spec],
            lake_path=self.server_context.data_path,
        )
        result = results[0]
        self._send_json(
            {
                "run_id": result.run_id,
                "search_name": result.search_name,
                "total_scraped": result.total_scraped,
                "jobs": [_ranked_row(item) for item in result.ranked_jobs[:100]],
            }
        )

    def _handle_search_start(self, payload: dict[str, Any]) -> None:
        config = self._refresh_config()
        search_spec = build_search_spec(payload, config)
        search_id = str(uuid.uuid4())
        _set_search_record(
            self.server_context,
            search_id,
            {
                "search_id": search_id,
                "status": "queued",
                "stage": "queued",
                "message": "Queued search",
                "percent": 0,
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
                "events": [
                    {
                        "stage": "queued",
                        "message": "Queued search",
                        "percent": 0,
                        "updated_at": _utc_now(),
                    }
                ],
                "payload": {
                    "keywords": payload.get("keywords") or payload.get("keyword") or [],
                    "related_to": payload.get("related_to") or [],
                    "location": search_spec.location,
                    "sites": search_spec.site_name,
                    "results": search_spec.results_wanted,
                },
            },
        )
        thread = threading.Thread(
            target=_run_background_search,
            args=(self.server_context, search_id, config, search_spec),
            daemon=True,
        )
        thread.start()
        self._send_json(_get_search_record(self.server_context, search_id), status=202)

    def _handle_search_status(self, search_id: str) -> None:
        record = _get_search_record(self.server_context, search_id)
        if record is None:
            self._send_json({"error": f"No search found with id {search_id}"}, status=404)
            return
        self._send_json(record)

    def _handle_application_update(self, payload: dict[str, Any]) -> None:
        job_id = str(payload.get("job_id") or "").strip()
        status = str(payload.get("status") or "").strip()
        if not job_id or not status:
            self._send_json({"error": "job_id and status are required"}, status=400)
            return
        update_application(
            self.server_context.data_path,
            job_id=job_id,
            status=status,
            notes=payload.get("notes"),
            applied_at=payload.get("applied_at"),
            next_action_at=payload.get("next_action_at"),
            resume_version=payload.get("resume_version"),
            cover_letter_path=payload.get("cover_letter_path"),
            contact_name=payload.get("contact_name"),
            contact_email=payload.get("contact_email"),
        )
        events = list_application_events(self.server_context.data_path, job_id)
        job = get_job_with_latest_score(self.server_context.data_path, job_id)
        self._send_json(
            {
                "ok": True,
                "events": events,
                "job": job,
                "last_applied_at": last_applied_at(events, job or {}),
            }
        )

    def _handle_brief(self, payload: dict[str, Any]) -> None:
        job_id = str(payload.get("job_id") or "").strip()
        if not job_id:
            self._send_json({"error": "job_id is required"}, status=400)
            return
        job = get_job_with_latest_score(self.server_context.data_path, job_id)
        if not job:
            self._send_json({"error": f"No job found with id {job_id}"}, status=404)
            return
        out_path = (
            self.server_context.data_path.parent
            / "output"
            / "briefs"
            / f"{_safe_filename(job_id)}.md"
        )
        cover_path = (
            self.server_context.data_path.parent
            / "output"
            / "cover_letters"
            / f"{_safe_filename(job_id)}.md"
        )
        path = write_application_brief(
            dict(job), self.server_context.config.profile, out_path
        )
        written_cover = write_cover_letter(
            dict(job), self.server_context.config.profile, cover_path
        )
        self._send_json(
            {
                "ok": True,
                "path": str(path),
                "cover_letter_path": str(written_cover),
                "brief": path.read_text(encoding="utf-8"),
                "cover_letter": written_cover.read_text(encoding="utf-8"),
            }
        )

    def _handle_feedback(self, payload: dict[str, Any]) -> None:
        job_id = str(payload.get("job_id") or "").strip()
        if not job_id:
            self._send_json({"error": "job_id is required"}, status=400)
            return
        job = get_job_with_latest_score(self.server_context.data_path, job_id)
        if not job:
            self._send_json({"error": f"No job found with id {job_id}"}, status=404)
            return
        terms = unique_terms(payload.get("negative_terms") or [])
        notes = payload.get("notes")
        add_feedback(
            self.server_context.data_path,
            job_id=job_id,
            negative_terms=terms,
            notes=notes,
            apply_globally=bool(payload.get("apply_globally", True)),
        )
        if payload.get("status"):
            update_application(
                self.server_context.data_path,
                job_id=job_id,
                status=str(payload.get("status")),
                notes=notes,
            )
        self._send_json(
            {
                "ok": True,
                "learned_negative_terms": learned_negative_terms(
                    self.server_context.data_path
                ),
            }
        )

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        body = self.rfile.read(length).decode("utf-8")
        return json.loads(body or "{}")

    def _send_html(self, html: str) -> None:
        encoded = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_asset(self, request_path: str) -> None:
        relative = Path(unquote(request_path.removeprefix("/assets/")))
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        root = ASSET_ROOT.resolve()
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        payload = target.read_bytes()
        content_type = (
            ASSET_CONTENT_TYPES.get(target.suffix.lower())
            or mimetypes.guess_type(str(target))[0]
            or "application/octet-stream"
        )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        encoded = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def build_search_spec(payload: dict[str, Any], config: JobFingerConfig) -> SearchSpec:
    keywords = unique_terms(payload.get("keywords") or [])
    related_to = unique_terms(payload.get("related_to") or [])
    match = str(payload.get("match") or "any")
    if keywords or related_to:
        query, focus_terms = build_keyword_query(
            keywords=keywords,
            related_to=related_to,
            groups=config.related_keyword_groups,
            match=match,
        )
    else:
        query = str(payload.get("search_term") or "").strip()
        focus_terms = keywords
    if not query:
        raise ValueError("Search term, keyword, or related topic is required.")
    required_keywords = keywords if match == "all" else []
    name = str(payload.get("name") or "ui-search").strip()
    return SearchSpec(
        name=name,
        search_term=query,
        location=str(payload.get("location") or config.profile.base_location or "Portugal"),
        site_name=payload.get("sites") or ["indeed", "linkedin"],
        results_wanted=int(payload.get("results") or 50),
        hours_old=int(payload.get("hours_old") or 168),
        country_indeed=str(payload.get("country") or "Portugal"),
        is_remote=bool(payload.get("remote")),
        description_format="plain",
        linkedin_fetch_description=not bool(payload.get("skip_linkedin_description")),
        focus_keywords=focus_terms,
        required_keywords=required_keywords,
        related_to=related_to,
    )


def read_observation_template(data_path: str | Path) -> str:
    data_path = Path(data_path)
    candidates = [
        data_path.parent / "observation_template.md",
        data_path / "observation_template.md",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8-sig")
    return DEFAULT_OBSERVATION_TEMPLATE


def last_applied_at(events: list[dict[str, Any]], job: dict[str, Any]) -> str | None:
    applied_values = [
        event.get("applied_at") or event.get("updated_at")
        for event in events
        if event.get("status") == "applied" or event.get("applied_at")
    ]
    if applied_values:
        return str(applied_values[-1])
    return job.get("applied_at") or None


def _load_config_or_default(config_path: str | Path) -> JobFingerConfig:
    path = Path(config_path)
    if path.exists():
        return load_config(path)
    return JobFingerConfig.from_dict(example_config(), source_path=path)


def profile_status(
    config: JobFingerConfig, config_path: str | Path, data_path: str | Path
) -> dict[str, Any]:
    config_path = Path(config_path)
    workspace = config_path.parent
    resume_profile = config.profile.resume_profile or {}
    evidence = dict(resume_profile.get("evidence") or {})
    return {
        "name": config.profile.name,
        "workspace": str(workspace),
        "cv_pdf_path": str(workspace / "cv.pdf"),
        "cv_pdf_exists": (workspace / "cv.pdf").exists(),
        "cv_markdown_path": str(workspace / "cv.md"),
        "cv_markdown_exists": (workspace / "cv.md").exists(),
        "cv_profile_path": str(workspace / "cv_profile.json"),
        "cv_profile_exists": (workspace / "cv_profile.json").exists(),
        "resume_keywords_count": len(config.profile.resume_keywords),
        "target_titles": config.profile.target_titles,
        "languages": config.profile.languages,
        "evidence_terms_count": len(evidence),
        "summary_signals": resume_profile.get("summary_signals") or [],
        "stored_jobs_count": len(JobLake(data_path).read_ranked_snapshot()),
    }


def _run_background_search(
    context: UIServerContext,
    search_id: str,
    config: JobFingerConfig,
    search_spec: SearchSpec,
) -> None:
    _update_search_record(
        context,
        search_id,
        status="running",
        stage="starting",
        message="Starting job board search",
        percent=2,
    )
    try:
        results = run_searches(
            config,
            search_names=[],
            search_specs=[search_spec],
            lake_path=context.data_path,
            progress_callback=lambda event: _update_search_record(
                context,
                search_id,
                status="running",
                **event,
            ),
        )
        result = results[0]
        _update_search_record(
            context,
            search_id,
            status="complete",
            stage="complete",
            message=(
                f"Done: scraped {result.total_scraped}, "
                f"stored {result.total_stored}"
            ),
            percent=100,
            result={
                "run_id": result.run_id,
                "search_name": result.search_name,
                "total_scraped": result.total_scraped,
                "total_stored": result.total_stored,
                "jobs": [_ranked_row(item) for item in result.ranked_jobs[:100]],
            },
        )
    except Exception as exc:  # UI surface needs the failure instead of silence.
        _update_search_record(
            context,
            search_id,
            status="error",
            stage="error",
            message=str(exc),
            percent=100,
            error=str(exc),
        )


def _set_search_record(
    context: UIServerContext, search_id: str, record: dict[str, Any]
) -> None:
    with context.search_lock:
        context.search_jobs[search_id] = record
        _trim_search_jobs(context.search_jobs)


def _get_search_record(
    context: UIServerContext, search_id: str
) -> dict[str, Any] | None:
    with context.search_lock:
        record = context.search_jobs.get(search_id)
        if record is None:
            return None
        return json.loads(json.dumps(record))


def _update_search_record(
    context: UIServerContext,
    search_id: str,
    **updates: Any,
) -> None:
    with context.search_lock:
        record = context.search_jobs.setdefault(
            search_id,
            {
                "search_id": search_id,
                "status": "running",
                "events": [],
                "created_at": _utc_now(),
            },
        )
        event = {
            "stage": updates.get("stage", record.get("stage", "running")),
            "message": updates.get("message", record.get("message", "")),
            "percent": updates.get("percent", record.get("percent", 0)),
            "updated_at": _utc_now(),
        }
        event.update(
            {
                key: value
                for key, value in updates.items()
                if key
                not in {
                    "status",
                    "result",
                    "error",
                }
            }
        )
        events = [*(record.get("events") or []), event]
        record.update(updates)
        record["updated_at"] = event["updated_at"]
        record["events"] = events[-30:]
        _trim_search_jobs(context.search_jobs)


def _trim_search_jobs(search_jobs: dict[str, dict[str, Any]], limit: int = 20) -> None:
    if len(search_jobs) <= limit:
        return
    ordered = sorted(
        search_jobs.items(),
        key=lambda item: str(item[1].get("updated_at") or ""),
        reverse=True,
    )
    keep = {search_id for search_id, _ in ordered[:limit]}
    for search_id in list(search_jobs):
        if search_id not in keep:
            search_jobs.pop(search_id, None)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _list_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": row.get("job_id"),
        "score": row.get("score"),
        "estimated_fit_probability": row.get("estimated_fit_probability"),
        "recommendation": row.get("recommendation"),
        "status": row.get("application_status"),
        "title": row.get("title"),
        "role_family": canonical_role_family(row),
        "company": row.get("company"),
        "location": row.get("location"),
        "site": row.get("site"),
        "date_posted": row.get("date_posted"),
        "published_at": row.get("date_posted"),
        "salary_label": salary_label(row),
        "salary_annual_min": row.get("salary_annual_min"),
        "salary_annual_max": row.get("salary_annual_max"),
        "work_mode": row.get("work_mode") or infer_work_mode_label(row),
        "work_schedule": row.get("work_schedule"),
        "work_hours_label": row.get("work_hours_label"),
        "seniority": row.get("seniority"),
        "skills": row.get("skills", []),
        "cv_matched_keywords": row.get("cv_matched_keywords", []),
        "cv_missing_keywords": row.get("cv_missing_keywords", []),
        "cv_evidence": row.get("cv_evidence", []),
        "cv_match_strength": row.get("cv_match_strength"),
        "job_type": row.get("job_type"),
        "last_applied_at": row.get("applied_at"),
        "observations": row.get("application_notes"),
    }


def _ranked_row(item: Any) -> dict[str, Any]:
    normalized = item.score.analysis.get("normalized", {})
    return {
        "job_id": item.job_id,
        "score": item.score.score,
        "estimated_fit_probability": item.score.estimated_fit_probability,
        "recommendation": item.score.recommendation,
        "status": "new",
        "title": item.job.get("title"),
        "role_family": canonical_role_family({**item.job, **normalized}),
        "company": item.job.get("company"),
        "location": item.job.get("location"),
        "site": item.job.get("site"),
        "date_posted": item.job.get("date_posted"),
        "published_at": item.job.get("date_posted"),
        "salary_label": normalized.get("salary_label") or salary_label(item.job),
        "salary_annual_min": normalized.get("salary_annual_min"),
        "salary_annual_max": normalized.get("salary_annual_max"),
        "work_mode": infer_work_mode_label(item.job),
        "work_schedule": normalized.get("work_schedule"),
        "work_hours_label": normalized.get("work_hours_label"),
        "seniority": normalized.get("seniority"),
        "skills": item.score.analysis.get("job_skills", []),
        "cv_matched_keywords": item.score.analysis.get("cv_matched_keywords", []),
        "cv_missing_keywords": item.score.analysis.get("cv_missing_keywords", []),
        "cv_evidence": item.score.analysis.get("cv_evidence", []),
        "cv_match_strength": item.score.analysis.get("cv_match_strength"),
        "job_type": item.job.get("job_type"),
    }


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    scores = [float(row.get("score") or 0) for row in rows]
    return {
        "total": total,
        "average_score": round(sum(scores) / total, 1) if total else 0,
        "priority": _count_value(rows, "recommendation", "priority"),
        "strong": _count_value(rows, "recommendation", "strong"),
        "review": _count_value(rows, "recommendation", "review"),
        "low": _count_value(rows, "recommendation", "low"),
        "remote": _count_value(rows, "work_mode", "remote"),
        "hybrid": _count_value(rows, "work_mode", "hybrid"),
        "office": _count_value(rows, "work_mode", "office"),
        "full_time": _count_value(rows, "work_schedule", "full_time"),
        "part_time": _count_value(rows, "work_schedule", "part_time"),
        "with_salary": sum(1 for row in rows if row.get("salary_label") or row.get("salary_max") or row.get("salary_min")),
        "with_cv_matches": sum(1 for row in rows if _list_count(row.get("cv_matched_keywords")) > 0),
        "with_cv_evidence": sum(1 for row in rows if _list_count(row.get("cv_evidence")) > 0),
        "with_gaps": sum(1 for row in rows if _list_count(row.get("cv_missing_keywords")) > 0),
        "with_negative": sum(1 for row in rows if _list_count(row.get("negative_keywords")) > 0),
        "new": _count_value(rows, "application_status", "new"),
        "saved": _count_value(rows, "application_status", "saved"),
        "applied": _count_value(rows, "application_status", "applied"),
        "ignored": _count_value(rows, "application_status", "ignored"),
    }


def _count_value(rows: list[dict[str, Any]], field: str, value: str) -> int:
    return sum(1 for row in rows if str(row.get(field) or "") == value)


def _list_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if value is None or value == "":
        return 0
    return 1


def salary_label(row: dict[str, Any]) -> str:
    if row.get("salary_label"):
        return str(row.get("salary_label"))
    min_amount = row.get("min_amount")
    max_amount = row.get("max_amount")
    currency = row.get("currency") or ""
    interval = row.get("interval") or ""
    if min_amount and max_amount and min_amount != max_amount:
        label = f"{_compact_money(min_amount)}-{_compact_money(max_amount)} {currency}"
    elif max_amount or min_amount:
        label = f"{_compact_money(max_amount or min_amount)} {currency}"
    else:
        return ""
    return f"{label.strip()} {interval}".strip()


def infer_work_mode_label(row: dict[str, Any]) -> str:
    raw = str(row.get("work_mode") or "").strip()
    if raw:
        return raw
    if row.get("is_remote") is True:
        return "remote"
    text = json.dumps(row, default=str).lower()
    if "hybrid" in text or "hibrido" in text:
        return "hybrid"
    if "remote" in text or "teletrabalho" in text:
        return "remote"
    if "presencial" in text or "in office" in text or "on site" in text:
        return "office"
    return "unknown"


def _compact_money(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number >= 1000:
        return f"{number / 1000:g}k"
    return f"{number:g}"


def _safe_filename(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "-_." else "-" for char in value)
    return safe.strip("-") or "job"


def _filter_rows_by_text(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    terms = [term.lower() for term in query.split() if term.strip()]
    if not terms:
        return rows
    filtered = []
    for row in rows:
        text = json.dumps(row, default=str).lower()
        if all(term in text for term in terms):
            filtered.append(row)
    return filtered


def _first_query(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name) or [""]
    return values[0]


def _int_query(query: dict[str, list[str]], name: str, default: int) -> int:
    try:
        return int(_first_query(query, name) or default)
    except ValueError:
        return default


def _optional_int_query(query: dict[str, list[str]], name: str) -> int | None:
    raw = _first_query(query, name)
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _float_query(query: dict[str, list[str]], name: str, default: float | None) -> float | None:
    raw = _first_query(query, name)
    if raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _bool_query(query: dict[str, list[str]], name: str) -> bool:
    return _first_query(query, name).lower() in {"1", "true", "yes", "on"}


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Job Finger</title>
  <style>
    :root {
      --bg: #f7f6f3;
      --panel: #ffffff;
      --soft: #fbfaf8;
      --line: #e4e0d9;
      --text: #222222;
      --muted: #717171;
      --accent: #b84f43;
      --accent-dark: #7f342c;
      --ink: #1f2933;
      --blue: #256f85;
      --warn: #9f580a;
      --bad: #9a3412;
      --good: #13795b;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background: var(--bg);
      letter-spacing: 0;
    }
    header {
      display: grid;
      gap: 14px;
      padding: 18px 28px 16px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 4;
      box-shadow: 0 1px 0 rgba(34, 34, 34, .04);
    }
    h1 {
      margin: 0;
      font-size: 21px;
      line-height: 1.2;
      font-weight: 700;
    }
    .brand-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }
    .brand-mark {
      width: 32px;
      height: 32px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      color: #fff;
      background: var(--accent);
      font-weight: 800;
    }
    .brand-subtitle {
      color: var(--muted);
      font-size: 12px;
      margin-top: 2px;
    }
    label {
      display: grid;
      gap: 4px;
      font-size: 12px;
      color: var(--muted);
      min-width: 0;
    }
    input, select, textarea, button {
      font: inherit;
      letter-spacing: 0;
    }
    input, select, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 9px 10px;
      background: #fff;
      color: var(--text);
      min-width: 0;
    }
    textarea {
      min-height: 150px;
      resize: vertical;
    }
    button {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--text);
      border-radius: 999px;
      padding: 9px 13px;
      cursor: pointer;
      white-space: nowrap;
    }
    button.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }
    button.primary:hover { background: var(--accent-dark); }
    button:disabled {
      cursor: not-allowed;
      opacity: .55;
    }
    .toolbar {
      display: grid;
      grid-template-columns: minmax(180px, 1.15fr) minmax(150px, .85fr) 128px 96px 128px auto;
      gap: 0;
      align-items: stretch;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #fff;
      box-shadow: 0 8px 22px rgba(34, 34, 34, .09);
      overflow: hidden;
      max-width: 1120px;
    }
    .toolbar label {
      padding: 9px 14px;
      border-right: 1px solid var(--line);
    }
    .toolbar input, .toolbar select {
      border: 0;
      border-radius: 0;
      padding: 0;
      background: transparent;
      min-height: 22px;
    }
    .toolbar button {
      margin: 7px;
      align-self: center;
    }
    .category-bar {
      display: flex;
      gap: 12px;
      align-items: center;
      overflow-x: auto;
      padding: 2px 0 4px;
      max-width: 1120px;
      scrollbar-width: none;
    }
    .category-bar::-webkit-scrollbar { display: none; }
    .category-chip {
      border: 0;
      border-radius: 0;
      border-bottom: 2px solid transparent;
      padding: 8px 2px 9px;
      color: var(--muted);
      background: transparent;
      font-size: 13px;
    }
    .category-chip:hover,
    .category-chip.active {
      color: var(--text);
      border-bottom-color: var(--text);
    }
    .search-progress {
      display: grid;
      gap: 9px;
      border: 1px solid #ecd7d2;
      border-radius: 14px;
      background: #fff8f6;
      padding: 12px 14px;
      max-width: 1120px;
    }
    .search-progress.hidden { display: none; }
    .progress-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
    }
    .progress-title {
      font-weight: 700;
      line-height: 1.25;
    }
    .progress-track {
      width: 100%;
      height: 8px;
      overflow: hidden;
      border-radius: 999px;
      background: #eaded8;
    }
    .progress-fill {
      height: 100%;
      width: 0;
      background: var(--accent);
      transition: width .25s ease;
    }
    .progress-steps {
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
    }
    .progress-step {
      border: 1px solid #eaded8;
      background: #fff;
      color: var(--muted);
      border-radius: 999px;
      padding: 4px 8px;
      font-size: 12px;
    }
    .progress-step.active {
      border-color: var(--accent);
      color: var(--accent-dark);
      font-weight: 700;
    }
    .cv-panel {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--soft);
      padding: 8px 10px;
      min-width: 0;
      max-width: 1120px;
    }
    header > .brand-row,
    header > .toolbar,
    header > .category-bar,
    header > .search-progress,
    header > .cv-panel {
      width: 100%;
      max-width: calc(1560px - 56px);
      margin-left: auto;
      margin-right: auto;
    }
    .cv-title {
      font-weight: 700;
      font-size: 13px;
      line-height: 1.25;
    }
    .cv-actions {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      justify-content: end;
    }
    .pill.good {
      border-color: #7dd3c7;
      color: var(--good);
      background: #ecfdf9;
    }
    .pill.warn {
      border-color: #f3c677;
      color: var(--warn);
      background: #fff7ed;
    }
    .pill.bad {
      border-color: #fdba74;
      color: var(--bad);
      background: #fff7ed;
    }
    main {
      display: block;
      min-height: calc(100vh - 214px);
    }
    aside {
      background: var(--bg);
      min-width: 0;
    }
    .results-head {
      padding: 20px 28px 8px;
      display: grid;
      gap: 4px;
      max-width: 1560px;
      margin: 0 auto;
    }
    .results-title {
      font-size: 18px;
      font-weight: 750;
      line-height: 1.25;
    }
    .filters {
      display: flex;
      gap: 8px;
      padding: 10px 28px;
      align-items: end;
      max-width: 1560px;
      margin: 0 auto;
      overflow-x: auto;
      scrollbar-width: none;
    }
    .filters::-webkit-scrollbar { display: none; }
    .filters label {
      min-width: 180px;
    }
    .filters label:first-child {
      min-width: 280px;
    }
    .filter-panel {
      margin: 0 auto 12px;
      max-width: calc(1560px - 56px);
      border: 1px solid var(--line);
      border-radius: 14px;
      background: #fff;
      overflow: hidden;
    }
    .filter-panel summary {
      cursor: pointer;
      padding: 12px 14px;
      font-weight: 700;
      list-style: none;
    }
    .filter-panel summary::-webkit-details-marker { display: none; }
    .exclude-filters {
      display: grid;
      grid-template-columns: minmax(160px, 1fr) 105px 100px 100px 100px;
      gap: 8px;
      padding: 0 14px 14px;
      align-items: end;
    }
    .summary-strip {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
      padding: 0 28px 16px;
      background: transparent;
      max-width: 1560px;
      margin: 0 auto;
      scrollbar-width: none;
    }
    .summary-strip::-webkit-scrollbar { display: none; }
    .summary-item {
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fff;
      padding: 10px;
      min-width: 0;
    }
    .summary-value {
      font-weight: 700;
      font-size: 15px;
      line-height: 1.2;
    }
    .summary-label {
      color: var(--muted);
      font-size: 11px;
      line-height: 1.2;
      margin-top: 2px;
    }
    .job-list {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(258px, 1fr));
      gap: 22px;
      padding: 0 28px 34px;
      max-width: 1560px;
      margin: 0 auto;
    }
    .job-row {
      width: 100%;
      display: block;
      padding: 0;
      border: 1px solid var(--line);
      border-radius: 16px;
      text-align: left;
      background: #fff;
      box-shadow: 0 1px 2px rgba(18, 25, 38, .04);
      overflow: hidden;
    }
    .job-row:hover, .job-row.active {
      border-color: #cfada4;
      box-shadow: 0 9px 24px rgba(34, 34, 34, .10);
    }
    .card-visual {
      position: relative;
      height: 174px;
      padding: 12px;
      display: grid;
      align-content: space-between;
      overflow: hidden;
      background: #f1eee9;
    }
    .card-visual::after {
      content: "";
      position: absolute;
      inset: 0;
      background:
        linear-gradient(180deg, rgba(0,0,0,.06), rgba(0,0,0,.28)),
        linear-gradient(135deg, rgba(184,79,67,.08), rgba(37,111,133,.08));
      z-index: 0;
    }
    .card-image {
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      object-fit: cover;
      transform: scale(1.01);
    }
    .visual-top, .visual-bottom {
      position: relative;
      z-index: 1;
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 8px;
      min-width: 0;
    }
    .visual-bottom {
      align-items: end;
    }
    .company-chip {
      position: relative;
      max-width: 142px;
      min-height: 34px;
      border-radius: 10px;
      background: rgba(255,255,255,.94);
      display: flex;
      align-items: center;
      color: var(--ink);
      font-weight: 800;
      font-size: 12px;
      line-height: 1.15;
      padding: 7px 9px;
      box-shadow: 0 1px 8px rgba(34, 34, 34, .08);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .date-badge {
      border: 1px solid rgba(255,255,255,.58);
      border-radius: 12px;
      min-width: 66px;
      padding: 7px 8px 6px;
      color: #fff;
      text-align: center;
      box-shadow: 0 8px 20px rgba(34, 34, 34, .18);
    }
    .date-age {
      display: block;
      font-size: 18px;
      font-weight: 900;
      line-height: 1;
    }
    .date-label {
      display: block;
      font-size: 10px;
      font-weight: 800;
      line-height: 1.1;
      margin-top: 3px;
      text-transform: uppercase;
    }
    .recency-hot { background: #b91c1c; }
    .recency-fresh { background: #d85f2a; }
    .recency-warm { background: #c08403; }
    .recency-cool { background: #256f85; }
    .recency-old { background: #57534e; }
    .recency-unknown { background: #6b7280; }
    .role-type-badge {
      border-radius: 999px;
      padding: 5px 8px;
      color: var(--ink);
      background: rgba(255,255,255,.92);
      font-size: 11px;
      font-weight: 800;
      max-width: 142px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .score-badge {
      position: relative;
      justify-self: start;
      border-radius: 999px;
      padding: 5px 8px;
      color: #fff;
      background: var(--ink);
      font-size: 12px;
      font-weight: 800;
    }
    .card-body {
      display: grid;
      gap: 7px;
      padding: 13px 13px 12px;
      min-width: 0;
    }
    .card-top {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: start;
    }
    .card-facts {
      color: var(--text);
      font-size: 13px;
      line-height: 1.35;
    }
    .score {
      display: grid;
      place-items: center;
      align-self: start;
      min-width: 44px;
      min-height: 36px;
      border-radius: 6px;
      color: #fff;
      background: var(--accent);
      font-weight: 700;
      font-size: 13px;
    }
    .job-title {
      font-weight: 700;
      font-size: 15px;
      line-height: 1.25;
    }
    .meta, .small {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }
    .status-line {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 6px;
    }
    .pill {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 8px;
      background: #fff;
      color: var(--muted);
      font-size: 12px;
    }
    .skill-line {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-top: 8px;
    }
    .skill-pill {
      border-radius: 999px;
      padding: 3px 7px;
      background: #eef6f7;
      color: var(--blue);
      font-size: 11px;
    }
    .decision-panel {
      border-top: 1px solid var(--line);
      margin-top: 10px;
      padding-top: 10px;
      display: grid;
      gap: 7px;
    }
    .decision-label {
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: .04em;
      text-transform: uppercase;
    }
    .quick-actions {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1.25fr) minmax(0, .8fr);
      gap: 7px;
    }
    .action-button {
      border-radius: 8px;
      padding: 8px 9px;
      font-size: 12px;
      font-weight: 800;
      min-height: 36px;
      box-shadow: none;
    }
    .action-button:hover {
      transform: translateY(-1px);
      box-shadow: 0 7px 16px rgba(34, 34, 34, .10);
    }
    .action-button:disabled {
      transform: none;
      box-shadow: none;
    }
    .action-save {
      border-color: #7dd3c7;
      color: #0f766e;
      background: #ecfdf9;
    }
    .action-brief {
      border-color: var(--accent);
      color: #fff;
      background: var(--accent);
    }
    .action-ignore {
      border-color: #d7d2cc;
      color: #57534e;
      background: #f7f5f2;
    }
    .action-button.active {
      outline: 2px solid rgba(31, 41, 51, .16);
      outline-offset: 2px;
    }
    .action-status {
      min-height: 15px;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.35;
    }
    .toast {
      position: fixed;
      right: 22px;
      bottom: 22px;
      z-index: 10;
      max-width: min(360px, calc(100vw - 36px));
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fff;
      color: var(--text);
      padding: 12px 14px;
      box-shadow: 0 18px 40px rgba(34, 34, 34, .18);
      font-size: 13px;
      font-weight: 700;
    }
    .toast.good {
      border-color: #7dd3c7;
      color: var(--good);
      background: #ecfdf9;
    }
    .toast.warn {
      border-color: #fdba74;
      color: var(--bad);
      background: #fff7ed;
    }
    .toast.hidden {
      display: none;
    }
    .match-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      max-width: 980px;
    }
    .match-panel {
      border: 1px solid var(--line);
      border-radius: 14px;
      background: #fff;
      padding: 12px;
      min-width: 0;
    }
    .match-panel.wide {
      grid-column: 1 / -1;
    }
    .match-panel h3 {
      margin: 0 0 8px;
      font-size: 14px;
    }
    .match-panel ul {
      margin: 0;
      padding-left: 18px;
      color: var(--text);
      font-size: 13px;
      line-height: 1.5;
    }
    .cover-draft {
      white-space: pre-wrap;
      line-height: 1.5;
      font-size: 13px;
      background: #f8fafc;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 12px;
    }
    section.detail {
      min-width: 0;
      display: none;
      background: #fff;
      border-top: 1px solid var(--line);
      max-width: 1180px;
      margin: 0 auto 42px;
      border-radius: 22px;
      overflow: hidden;
      box-shadow: 0 12px 36px rgba(34, 34, 34, .12);
    }
    section.detail.open {
      display: block;
    }
    .detail-head {
      padding: 22px 24px 18px;
      border-bottom: 1px solid var(--line);
      background: #fff;
    }
    .detail-title {
      margin: 0 0 6px;
      font-size: 26px;
      line-height: 1.2;
    }
    .detail-story {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 128px;
      gap: 18px;
      align-items: start;
    }
    .detail-score {
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
      text-align: center;
      box-shadow: 0 8px 18px rgba(34, 34, 34, .08);
    }
    .detail-score-value {
      font-size: 30px;
      line-height: 1;
      font-weight: 800;
      color: var(--accent-dark);
    }
    .links {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 12px;
    }
    .links a {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 9px;
      color: var(--accent-dark);
      background: #fff;
      text-decoration: none;
      font-size: 13px;
    }
    .tabs {
      display: flex;
      gap: 8px;
      padding: 14px 24px 0;
      background: #fff;
    }
    .tabs button.active {
      border-color: var(--accent);
      color: var(--accent-dark);
      font-weight: 700;
    }
    .pane {
      padding: 18px 24px;
      display: none;
    }
    .pane.active { display: block; }
    .description {
      white-space: pre-wrap;
      line-height: 1.5;
      font-size: 14px;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 14px;
    }
    .highlight-row {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }
    .highlight {
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px;
      background: var(--soft);
    }
    .highlight strong {
      display: block;
      margin-bottom: 4px;
    }
    pre {
      white-space: pre-wrap;
      overflow: auto;
      background: #111827;
      color: #f9fafb;
      border-radius: 6px;
      padding: 14px;
      font-size: 12px;
      line-height: 1.45;
    }
    .form-grid {
      display: grid;
      grid-template-columns: 150px 1fr;
      gap: 10px;
      max-width: 760px;
    }
    .form-grid .wide { grid-column: 1 / -1; }
    .events {
      display: grid;
      gap: 8px;
      margin-top: 14px;
    }
    .event {
      border: 1px solid var(--line);
      background: #fff;
      border-radius: 6px;
      padding: 10px;
    }
    .empty {
      padding: 26px;
      color: var(--muted);
    }
    @media (max-width: 900px) {
      .cv-panel, main, .exclude-filters, .summary-strip, .form-grid {
        grid-template-columns: 1fr;
      }
      header { padding: 16px 18px 12px; }
      .brand-subtitle { display: none; }
      .toolbar {
        grid-template-columns: minmax(0, 1fr) auto;
        border-radius: 999px;
        max-width: 100%;
      }
      .toolbar label {
        border: 0;
        padding: 11px 18px;
      }
      .toolbar label:not(:first-child) {
        display: none;
      }
      .toolbar button {
        width: 46px;
        min-height: 46px;
        padding: 0;
        margin: 5px;
        overflow: hidden;
        color: transparent;
        position: relative;
      }
      .toolbar button::after {
        content: "Search";
        color: #fff;
        position: absolute;
        inset: 0;
        display: grid;
        place-items: center;
        font-size: 0;
      }
      .toolbar button::before {
        content: "";
        width: 14px;
        height: 14px;
        border: 2px solid #fff;
        border-radius: 50%;
        position: absolute;
        left: 14px;
        top: 13px;
      }
      .toolbar button::after {
        width: 8px;
        height: 2px;
        background: #fff;
        transform: rotate(45deg);
        left: 27px;
        top: 28px;
        inset: auto;
      }
      .cv-actions { justify-content: start; }
      .results-head {
        padding-left: 18px;
        padding-right: 18px;
      }
      .summary-strip {
        display: flex;
        overflow-x: auto;
        padding-left: 18px;
        padding-right: 18px;
      }
      .summary-item {
        min-width: 116px;
      }
      .filters {
        padding: 10px 18px;
      }
      .filter-panel {
        margin-left: 18px;
        margin-right: 18px;
      }
      .job-list {
        padding-left: 18px;
        padding-right: 18px;
        grid-template-columns: repeat(auto-fill, minmax(238px, 1fr));
      }
      .detail-story, .highlight-row {
        grid-template-columns: 1fr;
      }
      section.detail {
        margin-left: 18px;
        margin-right: 18px;
      }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand-row">
      <div class="brand">
        <div class="brand-mark">RF</div>
        <div>
          <h1>Job Finger</h1>
          <div class="brand-subtitle">Find, rank, and track Portugal-ready roles.</div>
        </div>
      </div>
      <button id="refresh">Refresh</button>
    </div>
    <div class="toolbar">
      <label>What <input id="searchKeywords" placeholder="python, fastapi"></label>
      <label>Related to <input id="searchRelated" placeholder="backend, ai"></label>
      <label>Where <input id="searchLocation" value="Portugal"></label>
      <label>How many <input id="searchResults" type="number" min="1" max="500" value="50"></label>
      <label>Site
        <select id="searchSite">
          <option value="indeed,linkedin">Indeed + LinkedIn</option>
          <option value="indeed">Indeed</option>
          <option value="linkedin">LinkedIn</option>
          <option value="google">Google</option>
        </select>
      </label>
      <button class="primary" id="runSearch">Search</button>
    </div>
    <div class="category-bar">
      <button class="category-chip" data-chip="remote">Remote</button>
      <button class="category-chip" data-chip="hybrid">Hybrid</button>
      <button class="category-chip" data-chip="salary">Salary shown</button>
      <button class="category-chip" data-chip="cv">CV match</button>
      <button class="category-chip" data-chip="fresh">Fresh</button>
      <button class="category-chip" data-chip="senior">Senior</button>
      <button class="category-chip" data-chip="clean">No negatives</button>
    </div>
    <div id="searchProgress" class="search-progress hidden"></div>
    <div id="cvPanel" class="cv-panel"></div>
  </header>
  <main>
    <aside>
      <div class="results-head">
        <div class="results-title">Best matches</div>
        <div class="small">Ranked by fit, freshness, salary, work mode, and CV evidence.</div>
      </div>
      <details class="filter-panel">
        <summary>Filters</summary>
        <div class="filters">
          <label>Search saved jobs <input id="localQuery" placeholder="company, skill, title"></label>
          <label>Status
            <select id="statusFilter">
              <option value="">Any</option>
              <option value="new">New</option>
              <option value="saved">Saved</option>
              <option value="applied">Applied</option>
              <option value="follow_up">Follow up</option>
              <option value="interview">Interview</option>
              <option value="offer">Offer</option>
              <option value="rejected">Rejected</option>
              <option value="ignored">Ignored</option>
            </select>
          </label>
          <label>Sort
            <select id="sortBy">
              <option value="score">Best match</option>
              <option value="newest">Recency</option>
              <option value="role">Role type</option>
              <option value="salary">Salary</option>
              <option value="company">Company</option>
            </select>
          </label>
        </div>
        <div class="exclude-filters">
          <label>Skills <input id="skillKeywords" placeholder="python, react"></label>
          <label>Min Score <input id="minScore" type="number" min="0" max="100" value="0"></label>
          <label>Mode
            <select id="workMode">
              <option value="">Any</option>
              <option value="remote">Remote</option>
              <option value="hybrid">Hybrid</option>
              <option value="office">Office</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Schedule
            <select id="workSchedule">
              <option value="">Any</option>
              <option value="full_time">Full-time</option>
              <option value="part_time">Part-time</option>
              <option value="flexible">Flexible</option>
              <option value="shift">Shift</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
          <label>Seniority
            <select id="seniorityFilter">
              <option value="">Any</option>
              <option value="intern">Intern</option>
              <option value="junior">Junior</option>
              <option value="mid">Mid</option>
              <option value="senior">Senior</option>
              <option value="unknown">Unknown</option>
            </select>
          </label>
        </div>
        <div class="exclude-filters">
          <label>Min Salary <input id="minSalary" type="number" min="0" step="1000"></label>
          <label>Published From <input id="publishedFrom" type="date"></label>
          <label>Published To <input id="publishedTo" type="date"></label>
          <label>Recommendation
            <select id="recommendationFilter">
              <option value="">Any</option>
              <option value="priority">Priority</option>
              <option value="strong">Strong</option>
              <option value="review">Review</option>
              <option value="low">Low</option>
            </select>
          </label>
          <label>No Negative <input id="noNegative" type="checkbox"></label>
        </div>
        <div class="exclude-filters">
          <label>Exclude Keywords <input id="excludeKeywords" placeholder="senior, sap, recruiter"></label>
          <label>Exclude In
            <select id="excludeScope">
              <option value="all">All text</option>
              <option value="title">Title</option>
              <option value="content">Content</option>
            </select>
          </label>
          <label>Min CV Matches <input id="minCvMatches" type="number" min="0" max="20"></label>
          <label>Max Gaps <input id="maxCvGaps" type="number" min="0" max="30"></label>
        </div>
      </details>
      <div id="summaryStrip" class="summary-strip"></div>
      <div id="jobList" class="job-list"></div>
    </aside>
    <section class="detail" id="detail">
      <div class="empty">No job selected.</div>
    </section>
  </main>
  <div id="toast" class="toast hidden" role="status" aria-live="polite"></div>
  <script>
    const state = {
      jobs: [],
      selectedId: null,
      detail: null,
      activeTab: "post",
      summary: null,
      profile: null,
      search: null,
      searchTimer: null,
      toastTimer: null,
    };
    const $ = (id) => document.getElementById(id);
    const JOB_VISUALS = {
      software: "/assets/job-visuals/software.webp",
      data: "/assets/job-visuals/data-ai.webp",
      product: "/assets/job-visuals/product-design.webp",
      growth: "/assets/job-visuals/sales-marketing.webp",
      operations: "/assets/job-visuals/operations-finance.webp",
      support: "/assets/job-visuals/support-success.webp",
    };
    const VISUAL_CATEGORIES = [
      {
        key: "data",
        terms: ["data", "dados", "analytics", "analyst", "analista", "bi", "machine learning", "ml", "ai", "ia", "artificial intelligence", "scientist", "etl", "pipeline"],
      },
      {
        key: "product",
        terms: ["product", "produto", "designer", "design", "ux", "ui", "research", "figma", "user experience", "service design"],
      },
      {
        key: "growth",
        terms: ["sales", "vendas", "comercial", "business development", "marketing", "growth", "account executive", "customer acquisition", "seo", "performance"],
      },
      {
        key: "support",
        terms: ["support", "suporte", "customer success", "cliente", "helpdesk", "implementation", "onboarding", "service desk", "technical support"],
      },
      {
        key: "operations",
        terms: ["operations", "operacoes", "operações", "finance", "financas", "finanças", "project", "projeto", "program", "hr", "people", "recruiter", "talent", "business analyst"],
      },
      {
        key: "software",
        terms: ["software", "engineer", "engenheiro", "developer", "programador", "backend", "frontend", "fullstack", "full stack", "devops", "sre", "platform", "cloud", "python", "react", "java", "node"],
      },
    ];
    const ACTION_COPY = {
      saved: {
        working: "Shortlisting...",
        done: "Added to shortlist",
        tone: "good",
      },
      ignored: {
        working: "Passing...",
        done: "Marked as passed",
        tone: "warn",
      },
      brief: {
        working: "Building kit...",
        done: "Application kit saved",
        doneButton: "Kit ready",
        tone: "good",
      },
    };

    function splitTerms(value) {
      return value.split(",").map(item => item.trim()).filter(Boolean);
    }

    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || response.statusText);
      return data;
    }

    async function loadProfile() {
      try {
        state.profile = await api("/api/profile");
        renderProfile();
      } catch (error) {
        $("cvPanel").innerHTML = `
          <div>
            <div class="cv-title">Profile unavailable</div>
            <div class="small">${escapeHtml(error.message)}</div>
          </div>`;
      }
    }

    function renderProfile() {
      const profile = state.profile || {};
      const cvState = profile.cv_profile_exists
        ? "CV profile ready"
        : (profile.cv_pdf_exists ? "CV PDF found" : "CV PDF missing");
      const cvClass = profile.cv_profile_exists
        ? "good"
        : (profile.cv_pdf_exists ? "warn" : "bad");
      const cvPath = profile.cv_pdf_path || "workspace/cv.pdf";
      const pathStatus = profile.cv_pdf_exists ? cvPath : `Missing ${cvPath}`;
      $("cvPanel").innerHTML = `
        <div>
          <div class="cv-title">${escapeHtml(profile.name || "Local profile")}</div>
          <div class="status-line">
            <span class="pill ${cvClass}">${escapeHtml(cvState)}</span>
            <span class="pill">${escapeHtml(profile.resume_keywords_count || 0)} CV keywords</span>
            <span class="pill">${escapeHtml(profile.evidence_terms_count || 0)} evidence terms</span>
            <span class="pill">${escapeHtml(profile.stored_jobs_count || 0)} stored jobs</span>
          </div>
          <div class="small">${escapeHtml(pathStatus)}</div>
        </div>
        <div class="cv-actions">
          <button class="primary" id="updateCv" ${profile.cv_pdf_exists ? "" : "disabled"}>Convert CV + Rescore</button>
          <button id="rescoreJobs">Rescore Jobs</button>
          <span class="small" id="cvStatus"></span>
        </div>`;
      $("updateCv").onclick = updateCv;
      $("rescoreJobs").onclick = rescoreJobs;
    }

    function setCvBusy(isBusy) {
      const update = $("updateCv");
      const rescore = $("rescoreJobs");
      if (update) update.disabled = isBusy || !(state.profile && state.profile.cv_pdf_exists);
      if (rescore) rescore.disabled = isBusy;
    }

    async function updateCv() {
      const status = $("cvStatus");
      setCvBusy(true);
      status.textContent = "Converting";
      try {
        const payload = await api("/api/cv", { method: "POST", body: "{}" });
        state.profile = payload.profile;
        renderProfile();
        $("cvStatus").textContent = `Re-scored ${payload.rescore_count} job(s)`;
        await loadJobs();
      } catch (error) {
        status.textContent = error.message;
      } finally {
        setCvBusy(false);
      }
    }

    async function rescoreJobs() {
      const status = $("cvStatus");
      setCvBusy(true);
      status.textContent = "Re-scoring";
      try {
        const payload = await api("/api/rescore", { method: "POST", body: "{}" });
        state.profile = payload.profile;
        renderProfile();
        $("cvStatus").textContent = `Re-scored ${payload.rescore_count} job(s)`;
        await loadJobs();
      } catch (error) {
        status.textContent = error.message;
      } finally {
        setCvBusy(false);
      }
    }

    async function refreshAll() {
      await loadProfile();
      await loadJobs();
    }

    async function loadJobs() {
      const params = new URLSearchParams();
      const query = $("localQuery").value.trim();
      const status = $("statusFilter").value;
      const minScore = $("minScore").value;
      const publishedFrom = $("publishedFrom").value;
      const publishedTo = $("publishedTo").value;
      const sortBy = $("sortBy").value;
      const skillKeywords = splitTerms($("skillKeywords").value);
      const excludeKeywords = splitTerms($("excludeKeywords").value);
      const excludeScope = $("excludeScope").value;
      const workMode = $("workMode").value;
      const workSchedule = $("workSchedule").value;
      const seniority = $("seniorityFilter").value;
      const minSalary = $("minSalary").value;
      const recommendation = $("recommendationFilter").value;
      const minCvMatches = $("minCvMatches").value;
      const maxCvGaps = $("maxCvGaps").value;
      const noNegative = $("noNegative").checked;
      if (query) params.set("query", query);
      if (status) params.set("status", status);
      if (minScore) params.set("min_score", minScore);
      if (publishedFrom) params.set("published_from", publishedFrom);
      if (publishedTo) params.set("published_to", publishedTo);
      if (sortBy) params.set("sort", sortBy);
      for (const term of skillKeywords) params.append("keyword", term);
      for (const term of excludeKeywords) params.append("exclude_keyword", term);
      if (excludeKeywords.length) params.set("exclude_scope", excludeScope);
      if (workMode) params.set("work_mode", workMode);
      if (workSchedule) params.set("work_schedule", workSchedule);
      if (seniority) params.set("seniority", seniority);
      if (minSalary) params.set("min_salary", minSalary);
      if (recommendation) params.set("recommendation", recommendation);
      if (minCvMatches) params.set("min_cv_matches", minCvMatches);
      if (maxCvGaps) params.set("max_cv_gaps", maxCvGaps);
      if (noNegative) params.set("no_negative", "true");
      params.set("limit", "250");
      const data = await api(`/api/jobs?${params.toString()}`);
      state.jobs = data.jobs;
      state.summary = data.summary || null;
      renderSummary();
      renderList();
      if (state.selectedId && state.jobs.some(job => job.job_id === state.selectedId)) {
        await selectJob(state.selectedId);
      } else {
        state.selectedId = null;
        $("detail").classList.remove("open");
        $("detail").innerHTML = state.jobs.length
          ? '<div class="empty">Select a role to open the listing.</div>'
          : '<div class="empty">No jobs found.</div>';
      }
    }

    function renderSummary() {
      const strip = $("summaryStrip");
      const summary = state.summary || {};
      const items = [
        [`${summary.total || 0}`, "listings"],
        [`${summary.priority || 0}/${summary.strong || 0}`, "priority / strong"],
        [`${summary.remote || 0}/${summary.hybrid || 0}`, "remote / hybrid"],
        [`${summary.with_salary || 0}/${summary.with_cv_evidence || 0}`, "salary / CV proof"],
      ];
      strip.innerHTML = items.map(([value, label]) => `
        <div class="summary-item">
          <div class="summary-value">${escapeHtml(value)}</div>
          <div class="summary-label">${escapeHtml(label)}</div>
        </div>`).join("");
    }

    function jobVisual(job) {
      const words = [
        job.title,
        job.role_family,
        job.company,
        job.location,
        job.seniority,
        ...(job.skills || []),
        ...(job.cv_matched_keywords || []),
        ...(job.cv_missing_keywords || []),
      ].filter(Boolean).join(" ").toLowerCase();
      const category = VISUAL_CATEGORIES.find(item =>
        item.terms.some(term => words.includes(term))
      );
      return JOB_VISUALS[category?.key || "operations"];
    }

    function renderList() {
      const list = $("jobList");
      list.innerHTML = "";
      if (!state.jobs.length) {
        list.innerHTML = '<div class="empty">No jobs.</div>';
        return;
      }
      for (const job of state.jobs) {
        const row = document.createElement("div");
        row.className = `job-row ${job.job_id === state.selectedId ? "active" : ""}`;
        row.setAttribute("role", "button");
        row.tabIndex = 0;
        row.onclick = () => selectJob(job.job_id);
        row.onkeydown = (event) => {
          if (event.target !== row) return;
          if (event.key === "Enter" || event.key === " ") selectJob(job.job_id);
        };
        const appStatus = String(job.status || job.application_status || "new").toLowerCase();
        const isSaved = appStatus === "saved";
        const isIgnored = appStatus === "ignored";
        const facts = [
          job.salary_label,
          formatWorkMode(job.work_mode),
          showSchedule(job) ? formatSchedule(job.work_schedule) : "",
        ].filter(Boolean).join(" · ");
        const visual = jobVisual(job);
        const recency = recencyInfo(job.published_at || job.date_posted);
        const companyName = companyLabel(job.company || "Company");
        const roleFamily = job.role_family || "Other";
        row.innerHTML = `
          <div class="card-visual">
            <img class="card-image" src="${escapeAttr(visual)}" alt="">
            <div class="visual-top">
              <div class="company-chip" title="${escapeAttr(job.company || "Company not shown")}">${escapeHtml(companyName)}</div>
              <div class="date-badge ${escapeAttr(recency.className)}" title="${escapeAttr(recency.title)}">
                <span class="date-age">${escapeHtml(recency.dateText)}</span>
                <span class="date-label">${escapeHtml(recency.ageText)}</span>
              </div>
            </div>
            <div class="visual-bottom">
              <div class="score-badge">${Math.round(job.score || 0)} match</div>
              <div class="role-type-badge" title="${escapeAttr(roleFamily)}">${escapeHtml(roleFamily)}</div>
            </div>
          </div>
          <div class="card-body">
            <div class="card-top">
              <div class="job-title">${escapeHtml(job.title || "Untitled")}</div>
              ${job.recommendation ? `<span class="pill good">${escapeHtml(job.recommendation)}</span>` : ""}
            </div>
            <div class="meta">${escapeHtml([job.company, job.location].filter(Boolean).join(" · ") || "Company not shown")}</div>
            <div class="card-facts">${escapeHtml(facts || "Salary and work setup not captured yet")}</div>
            <div class="status-line">
              <span class="pill">${escapeHtml(job.status || "new")}</span>
              <span class="pill">${escapeHtml(job.site || "")}</span>
              ${job.seniority ? `<span class="pill">${escapeHtml(job.seniority)}</span>` : ""}
              ${showCvStrength(job) ? `<span class="pill">CV ${escapeHtml(job.cv_match_strength)}</span>` : ""}
              ${job.last_applied_at ? `<span class="pill">Applied ${escapeHtml(formatDate(job.last_applied_at))}</span>` : ""}
            </div>
            <div class="skill-line">
              ${renderSkillPills(job.cv_matched_keywords || job.skills || [], 5)}
              ${(job.cv_missing_keywords || []).length ? `<span class="skill-pill">${escapeHtml(job.cv_missing_keywords.length)} gaps</span>` : ""}
            </div>
            <div class="decision-panel">
              <div class="decision-label">Next step</div>
              <div class="quick-actions">
                <button class="action-button action-save ${isSaved ? "active" : ""}" data-action="saved" data-job-id="${escapeAttr(job.job_id)}" title="Keep this role in your shortlist">${isSaved ? "Shortlisted" : "Shortlist"}</button>
                <button class="action-button action-brief" data-action="brief" data-job-id="${escapeAttr(job.job_id)}" title="Create the local brief and cover letter draft">Build kit</button>
                <button class="action-button action-ignore ${isIgnored ? "active" : ""}" data-action="ignored" data-job-id="${escapeAttr(job.job_id)}" title="Mark this role as passed">${isIgnored ? "Passed" : "Pass"}</button>
              </div>
              <div class="action-status" aria-live="polite"></div>
            </div>
          </div>`;
        row.querySelectorAll("[data-action]").forEach(button => {
          button.onclick = async (event) => {
            event.stopPropagation();
            await quickAction(button.dataset.jobId, button.dataset.action, button);
          };
        });
        list.appendChild(row);
      }
    }

    async function selectJob(jobId) {
      state.selectedId = jobId;
      state.detail = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      renderList();
      renderDetail();
      $("detail").scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function renderDetail() {
      const payload = state.detail;
      if (!payload) return;
      const job = payload.job;
      const raw = job.raw_job || {};
      const description = raw.description || job.description || "";
      $("detail").classList.add("open");
      $("detail").innerHTML = `
        <div class="detail-head">
          <div class="detail-story">
            <div>
              <h2 class="detail-title">${escapeHtml(job.title || "Untitled")}</h2>
              <div class="meta">${escapeHtml([job.company, job.location].filter(Boolean).join(" · ") || "Company not shown")}</div>
              <div class="status-line">
                <span class="pill">${escapeHtml(job.application_status || "new")}</span>
                ${job.role_family ? `<span class="pill">${escapeHtml(job.role_family)}</span>` : ""}
                ${job.date_posted ? `<span class="pill">Published ${escapeHtml(job.date_posted)}</span>` : ""}
                ${salaryLabel(job) ? `<span class="pill">${escapeHtml(salaryLabel(job))}</span>` : ""}
                ${job.work_mode ? `<span class="pill">${escapeHtml(formatWorkMode(job.work_mode))}</span>` : ""}
                ${showSchedule(job) ? `<span class="pill">${escapeHtml(formatSchedule(job.work_schedule))}</span>` : ""}
                ${job.work_hours_label ? `<span class="pill">${escapeHtml(job.work_hours_label)}</span>` : ""}
                ${job.seniority ? `<span class="pill">${escapeHtml(job.seniority)}</span>` : ""}
                ${showCvStrength(job) ? `<span class="pill">CV ${escapeHtml(job.cv_match_strength)}</span>` : ""}
                ${payload.last_applied_at ? `<span class="pill">Last applied ${escapeHtml(formatDate(payload.last_applied_at))}</span>` : ""}
                ${job.next_action_at ? `<span class="pill">Next ${escapeHtml(job.next_action_at)}</span>` : ""}
              </div>
            </div>
            <div class="detail-score">
              <div class="detail-score-value">${escapeHtml(Math.round(Number(job.score || 0)))}</div>
              <div class="small">match score</div>
              ${job.recommendation ? `<div class="pill good">${escapeHtml(job.recommendation)}</div>` : ""}
            </div>
          </div>
          <div class="links">
            ${job.job_url ? `<a href="${escapeAttr(job.job_url)}" target="_blank" rel="noreferrer">Job Post</a>` : ""}
            ${job.job_url_direct ? `<a href="${escapeAttr(job.job_url_direct)}" target="_blank" rel="noreferrer">Direct Apply</a>` : ""}
          </div>
        </div>
        <div class="tabs">
          ${tabButton("post", "Overview")}
          ${tabButton("match", "Match")}
          ${tabButton("application", "Apply")}
          ${tabButton("data", "Data")}
        </div>
        <div id="pane-post" class="pane ${state.activeTab === "post" ? "active" : ""}">
          ${detailHighlights(job)}
          <div class="description">${escapeHtml(description || "No description captured.")}</div>
        </div>
        <div id="pane-match" class="pane ${state.activeTab === "match" ? "active" : ""}">
          ${matchPane(job)}
        </div>
        <div id="pane-application" class="pane ${state.activeTab === "application" ? "active" : ""}">
          ${applicationPane(payload)}
        </div>
        <div id="pane-data" class="pane ${state.activeTab === "data" ? "active" : ""}">
          <pre>${escapeHtml(JSON.stringify(job, null, 2))}</pre>
        </div>`;
      document.querySelectorAll("[data-tab]").forEach(button => {
        button.onclick = () => {
          state.activeTab = button.dataset.tab;
          renderDetail();
        };
      });
      const save = $("saveApplication");
      if (save) save.onclick = saveApplication;
      const insert = $("insertTemplate");
      if (insert) insert.onclick = () => {
        const notes = $("notes");
        if (!notes.value.trim()) notes.value = payload.observation_template || "";
      };
      const saveBrief = $("saveBrief");
      if (saveBrief) saveBrief.onclick = saveApplicationBrief;
      const saveFeedback = $("saveFeedback");
      if (saveFeedback) saveFeedback.onclick = saveNegativeFeedback;
    }

    function tabButton(id, label) {
      return `<button data-tab="${id}" class="${state.activeTab === id ? "active" : ""}">${label}</button>`;
    }

    function detailHighlights(job) {
      const highlights = [
        ["Work setup", [formatWorkMode(job.work_mode), showSchedule(job) ? formatSchedule(job.work_schedule) : "", job.work_hours_label].filter(Boolean).join(" · ") || "Not captured yet"],
        ["Compensation", salaryLabel(job) || "Not shown"],
        ["Best evidence", (job.cv_matched_keywords || [])[0] || (job.skills || [])[0] || "No CV signal yet"],
      ];
      return `<div class="highlight-row">${highlights.map(([label, value]) => `
        <div class="highlight">
          <strong>${escapeHtml(label)}</strong>
          <div class="small">${escapeHtml(value)}</div>
        </div>`).join("")}</div>`;
    }

    function matchPane(job) {
      return `
        <div class="match-grid">
          <div class="match-panel">
            <h3>Fit Signals</h3>
            ${listItems(job.match_explanation || job.reasons || [], "No explanation recorded.")}
          </div>
          <div class="match-panel">
            <h3>CV Proof</h3>
            ${listItems(job.cv_matched_keywords || [], "No direct CV matches captured.")}
            ${evidenceItems(job.cv_evidence || [], "No CV evidence snippets captured.")}
          </div>
          <div class="match-panel">
            <h3>Watchouts</h3>
            ${listItems(job.cv_missing_keywords || [], "No CV gaps captured.")}
          </div>
          <div class="match-panel">
            <h3>Skills In Post</h3>
            ${listItems(job.skills || [], "No skills detected.")}
          </div>
          <div class="match-panel wide">
            <h3>Application Kit</h3>
            ${listItems(job.application_suggestions || [], "No suggestions recorded.")}
            <div class="cover-draft">${escapeHtml(job.cover_letter_draft || "No cover letter draft recorded.")}</div>
            <div class="links">
              <button class="primary" id="saveBrief">Save Brief</button>
              <span class="small" id="briefStatus"></span>
            </div>
          </div>
          <div class="match-panel wide">
            <h3>Teach The Recommender</h3>
            <div class="form-grid">
              <label class="wide">Negative Terms
                <input id="feedbackTerms" placeholder="sap, cold calling, unpaid">
              </label>
              <label class="wide">Reason
                <input id="feedbackNotes" placeholder="Why this recommendation was wrong">
              </label>
              <button id="saveFeedback">Learn + Ignore</button>
              <span class="small" id="feedbackStatus"></span>
            </div>
          </div>
        </div>`;
    }

    function applicationPane(payload) {
      const job = payload.job;
      const events = payload.events || [];
      return `
        <div class="form-grid">
          <label>Status
            <select id="appStatus">
              ${["new","saved","applied","follow_up","interview","offer","rejected","ignored"].map(status => `<option value="${status}" ${status === job.application_status ? "selected" : ""}>${status}</option>`).join("")}
            </select>
          </label>
          <label>Applied At <input id="appliedAt" value="${escapeAttr(payload.last_applied_at || "")}"></label>
          <label>Next Action <input id="nextAction" value="${escapeAttr(job.next_action_at || "")}"></label>
          <label>Contact <input id="contact" value=""></label>
          <label class="wide">Observations
            <textarea id="notes">${escapeHtml(job.application_notes || "")}</textarea>
          </label>
          <button id="insertTemplate">Insert Template</button>
          <button class="primary" id="saveApplication">Save</button>
        </div>
        <div class="events">
          ${events.length ? events.slice().reverse().map(event => `
            <div class="event">
              <strong>${escapeHtml(event.status || "event")}</strong>
              <div class="small">${escapeHtml(event.updated_at || "")}${event.applied_at ? ` - applied ${escapeHtml(event.applied_at)}` : ""}</div>
              ${event.notes ? `<div>${escapeHtml(event.notes)}</div>` : ""}
            </div>`).join("") : '<div class="empty">No application events.</div>'}
        </div>`;
    }

    async function saveApplication() {
      const jobId = state.selectedId;
      await api("/api/applications", {
        method: "POST",
        body: JSON.stringify({
          job_id: jobId,
          status: $("appStatus").value,
          notes: $("notes").value,
          applied_at: $("appliedAt").value || null,
          next_action_at: $("nextAction").value || null,
          contact_name: $("contact").value || null,
        }),
      });
      await selectJob(jobId);
      await loadJobs();
    }

    async function saveApplicationBrief() {
      const status = $("briefStatus");
      const button = $("saveBrief");
      button.disabled = true;
      status.textContent = "Saving";
      try {
        const payload = await api("/api/briefs", {
          method: "POST",
          body: JSON.stringify({ job_id: state.selectedId }),
        });
        status.textContent = `Saved ${payload.path} and ${payload.cover_letter_path}`;
      } finally {
        button.disabled = false;
      }
    }

    async function saveNegativeFeedback() {
      const status = $("feedbackStatus");
      const button = $("saveFeedback");
      button.disabled = true;
      status.textContent = "Saving";
      try {
        const terms = splitTerms($("feedbackTerms").value);
        const notes = $("feedbackNotes").value;
        const payload = await api("/api/feedback", {
          method: "POST",
          body: JSON.stringify({
            job_id: state.selectedId,
            negative_terms: terms,
            notes,
            status: "ignored",
            apply_globally: true,
          }),
        });
        status.textContent = `Learned ${payload.learned_negative_terms.length} term(s)`;
        await loadJobs();
      } finally {
        button.disabled = false;
      }
    }

    function showToast(message, tone = "good") {
      const toast = $("toast");
      if (!toast) return;
      toast.textContent = message;
      toast.className = `toast ${tone}`;
      clearTimeout(state.toastTimer);
      state.toastTimer = setTimeout(() => {
        toast.className = "toast hidden";
        toast.textContent = "";
      }, 3200);
    }

    async function quickAction(jobId, action, button) {
      const copy = ACTION_COPY[action] || {
        working: "Working...",
        done: "Done",
        tone: "good",
      };
      const panel = button.closest(".decision-panel");
      const status = panel ? panel.querySelector(".action-status") : null;
      const originalText = button.textContent;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.textContent = copy.working;
      if (status) status.textContent = copy.working;
      try {
        if (action === "brief") {
          const payload = await api("/api/briefs", {
            method: "POST",
            body: JSON.stringify({ job_id: jobId }),
          });
          const folder = payload.path ? "workspace/output" : "workspace";
          if (status) status.textContent = `${copy.done} in ${folder}`;
          button.textContent = copy.doneButton || copy.done;
          showToast(`${copy.done} in ${folder}`, copy.tone);
          return;
        } else {
          await api("/api/applications", {
            method: "POST",
            body: JSON.stringify({ job_id: jobId, status: action }),
          });
        }
        if (status) status.textContent = copy.done;
        showToast(copy.done, copy.tone);
        await loadJobs();
      } catch (error) {
        const message = error.message || "Action failed";
        if (status) status.textContent = message;
        showToast(message, "warn");
      } finally {
        button.removeAttribute("aria-busy");
        button.disabled = false;
        if (action !== "brief") button.textContent = originalText;
      }
    }

    function renderSearchProgress(record) {
      const panel = $("searchProgress");
      if (!record) {
        panel.classList.add("hidden");
        panel.innerHTML = "";
        return;
      }
      const percent = Math.max(0, Math.min(100, Number(record.percent || 0)));
      const statusLabel = record.status === "error"
        ? "Search failed"
        : (record.status === "complete" ? "Search complete" : "Search running");
      const events = (record.events || []).slice(-6);
      const steps = [
        ["prepare", "Prepare"],
        ["scrape", "Boards"],
        ["scraped", "Received"],
        ["scoring", "Score"],
        ["saving", "Save"],
        ["complete", "Done"],
      ];
      panel.classList.remove("hidden");
      panel.innerHTML = `
        <div class="progress-head">
          <div>
            <div class="progress-title">${escapeHtml(statusLabel)}</div>
            <div class="small">${escapeHtml(record.message || "Working")}</div>
          </div>
          <div class="small">${escapeHtml(Math.round(percent))}%</div>
        </div>
        <div class="progress-track"><div class="progress-fill" style="width:${percent}%"></div></div>
        <div class="progress-steps">
          ${steps.map(([stage, label]) => `<span class="progress-step ${isProgressStageActive(stage, record.stage) ? "active" : ""}">${escapeHtml(label)}</span>`).join("")}
        </div>
        <div class="small">${events.map(event => `${event.percent || 0}% ${event.message || event.stage || ""}`).map(escapeHtml).join(" · ")}</div>`;
    }

    function isProgressStageActive(stage, current) {
      const order = ["queued", "starting", "prepare", "scrape", "scraped", "scoring", "saving", "complete"];
      return order.indexOf(stage) <= order.indexOf(current) || stage === current;
    }

    async function pollSearch(searchId) {
      const record = await api(`/api/searches/${encodeURIComponent(searchId)}`);
      state.search = record;
      renderSearchProgress(record);
      if (record.status === "complete") {
        clearInterval(state.searchTimer);
        state.searchTimer = null;
        $("runSearch").disabled = false;
        $("runSearch").textContent = "Search";
        await refreshAll();
      } else if (record.status === "error") {
        clearInterval(state.searchTimer);
        state.searchTimer = null;
        $("runSearch").disabled = false;
        $("runSearch").textContent = "Search";
      }
    }

    async function runSearch() {
      const payload = {
        name: "ui-search",
        keywords: splitTerms($("searchKeywords").value),
        related_to: splitTerms($("searchRelated").value),
        location: $("searchLocation").value || "Portugal",
        results: Number($("searchResults").value || 50),
        sites: $("searchSite").value.split(",").filter(Boolean),
      };
      $("runSearch").disabled = true;
      $("runSearch").textContent = "Searching...";
      if (state.searchTimer) clearInterval(state.searchTimer);
      state.search = {
        status: "queued",
        stage: "queued",
        message: "Queued search",
        percent: 0,
        events: [{ percent: 0, message: "Queued search" }],
      };
      renderSearchProgress(state.search);
      try {
        const started = await api("/api/search/start", { method: "POST", body: JSON.stringify(payload) });
        state.search = started;
        renderSearchProgress(started);
        await pollSearch(started.search_id);
        if (state.search && ["complete", "error"].includes(state.search.status)) {
          return;
        }
        state.searchTimer = setInterval(() => {
          pollSearch(started.search_id).catch(error => {
            clearInterval(state.searchTimer);
            state.searchTimer = null;
            state.search = {
              status: "error",
              stage: "error",
              message: error.message,
              percent: 100,
              events: [{ percent: 100, message: error.message }],
            };
            renderSearchProgress(state.search);
            $("runSearch").disabled = false;
            $("runSearch").textContent = "Search";
          });
        }, 1200);
      } catch (error) {
        state.search = {
          status: "error",
          stage: "error",
          message: error.message,
          percent: 100,
          events: [{ percent: 100, message: error.message }],
        };
        renderSearchProgress(state.search);
        $("runSearch").disabled = false;
        $("runSearch").textContent = "Search";
      }
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, char => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
      }[char]));
    }
    function escapeAttr(value) { return escapeHtml(value); }
    function formatDate(value) { return String(value || "").replace("T", " ").replace("+00:00", ""); }

    function companyLabel(value) {
      const clean = String(value || "Company").trim() || "Company";
      return clean.length > 12 ? `${clean.slice(0, 12)}...` : clean;
    }

    function recencyInfo(value) {
      const datePart = publishedDatePart(value);
      const date = parsePublishedDate(datePart);
      if (!date) {
        return {
          className: "recency-unknown",
          dateText: "No date",
          ageText: "posted",
          title: "Publish date not captured",
        };
      }
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      const published = new Date(date);
      published.setHours(0, 0, 0, 0);
      const days = Math.max(0, Math.floor((today - published) / 86400000));
      const className = days <= 1
        ? "recency-hot"
        : days <= 3
          ? "recency-fresh"
          : days <= 7
            ? "recency-warm"
            : days <= 14
              ? "recency-cool"
              : "recency-old";
      const dateText = published.toLocaleDateString("en-GB", {
        day: "2-digit",
        month: "short",
      });
      const ageText = days === 0 ? "today" : `${days}d old`;
      return {
        className,
        dateText,
        ageText,
        title: `Published ${datePart} (${ageText})`,
      };
    }

    function publishedDatePart(value) {
      const raw = String(value || "").trim();
      return raw.match(/^\d{4}-\d{2}-\d{2}/)?.[0] || raw;
    }

    function parsePublishedDate(value) {
      if (!value) return null;
      const datePart = String(value || "").trim();
      if (!datePart) return null;
      const date = new Date(`${datePart}T00:00:00`);
      return Number.isNaN(date.getTime()) ? null : date;
    }

    function listItems(items, emptyText) {
      const values = (items || []).filter(item => String(item || "").trim());
      if (!values.length) return `<div class="small">${escapeHtml(emptyText)}</div>`;
      return `<ul>${values.slice(0, 20).map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
    }

    function evidenceItems(items, emptyText) {
      const values = (items || []).filter(item => (item.snippets || []).length);
      if (!values.length) return `<div class="small">${escapeHtml(emptyText)}</div>`;
      return `<ul>${values.slice(0, 8).map(item => {
        const snippets = (item.snippets || []).slice(0, 2).map(snippet => `<li>${escapeHtml(snippet)}</li>`).join("");
        return `<li><strong>${escapeHtml(item.keyword || "evidence")}</strong><ul>${snippets}</ul></li>`;
      }).join("")}</ul>`;
    }

    function renderSkillPills(items, limit) {
      return (items || [])
        .filter(item => String(item || "").trim())
        .slice(0, limit)
        .map(item => `<span class="skill-pill">${escapeHtml(item)}</span>`)
        .join("");
    }

    function applyCategoryChip(chip) {
      document.querySelectorAll(".category-chip").forEach(button => button.classList.remove("active"));
      chip.classList.add("active");
      const value = chip.dataset.chip;
      if (value === "remote" || value === "hybrid") {
        $("workMode").value = value;
      }
      if (value === "salary") {
        $("minSalary").value = $("minSalary").value || "1";
        $("sortBy").value = "salary";
      }
      if (value === "cv") {
        $("minCvMatches").value = $("minCvMatches").value || "1";
      }
      if (value === "fresh") {
        $("sortBy").value = "newest";
      }
      if (value === "senior") {
        $("seniorityFilter").value = "senior";
      }
      if (value === "clean") {
        $("noNegative").checked = true;
      }
      loadJobs();
    }

    $("runSearch").onclick = runSearch;
    $("refresh").onclick = refreshAll;
    document.querySelectorAll(".category-chip").forEach(button => {
      button.onclick = () => applyCategoryChip(button);
    });
    function salaryLabel(job) {
      if (job.salary_label) return job.salary_label;
      const min = job.min_amount;
      const max = job.max_amount;
      const currency = job.currency || "";
      const interval = job.interval || "";
      if (min && max && min !== max) return `${compactMoney(min)}-${compactMoney(max)} ${currency} ${interval}`.trim();
      if (max || min) return `${compactMoney(max || min)} ${currency} ${interval}`.trim();
      return "";
    }

    function showCvStrength(job) {
      return job.cv_match_strength && job.cv_match_strength !== "unknown";
    }

    function showSchedule(job) {
      return job.work_schedule && job.work_schedule !== "unknown";
    }

    function initials(value) {
      const parts = String(value || "")
        .replace(/[^a-zA-Z0-9 ]/g, " ")
        .split(/\s+/)
        .filter(Boolean);
      const picked = parts.length > 1 ? [parts[0], parts[1]] : [parts[0] || "R"];
      return picked.map(part => part[0]).join("").slice(0, 2).toUpperCase();
    }

    function formatWorkMode(value) {
      const raw = String(value || "");
      if (!raw || raw === "unknown") return "";
      return raw.replace("_", "-");
    }

    function formatSchedule(value) {
      return String(value || "").replaceAll("_", "-");
    }

    function compactMoney(value) {
      const number = Number(value);
      if (!Number.isFinite(number)) return String(value || "");
      if (number >= 1000) return `${number / 1000}k`;
      return String(number);
    }

    ["localQuery", "statusFilter", "minScore", "publishedFrom", "publishedTo", "sortBy", "skillKeywords", "excludeKeywords", "excludeScope", "workMode", "workSchedule", "seniorityFilter", "minSalary", "recommendationFilter", "minCvMatches", "maxCvGaps", "noNegative"].forEach(id => {
      $(id).addEventListener("change", loadJobs);
      $(id).addEventListener("keyup", event => { if (event.key === "Enter") loadJobs(); });
    });
    refreshAll();
  </script>
</body>
</html>"""
