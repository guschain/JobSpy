from __future__ import annotations

import json
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from job_finger.config import JobFingerConfig, SearchSpec, example_config, load_config
from job_finger.pipeline import run_searches
from job_finger.search_terms import (
    build_keyword_query,
    expand_related_topics,
    filter_rows_by_terms,
    unique_terms,
)
from job_finger.storage import (
    JobLake,
    get_job_with_latest_score,
    list_application_events,
    list_ranked_jobs,
    update_application,
)


DEFAULT_OBSERVATION_TEMPLATE = """Outcome:

Fit notes:

Concerns:

Next action:
"""


@dataclass(frozen=True)
class UIServerContext:
    config: JobFingerConfig
    data_path: Path


def run_ui_server(
    *,
    config_path: str | Path = "job_finger.config.json",
    data_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    config = _load_config_or_default(config_path)
    resolved_data_path = config.resolve_storage_path(str(data_path) if data_path else None)
    context = UIServerContext(config=config, data_path=resolved_data_path)

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
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/search":
            try:
                self._handle_search(self._read_json_body())
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
            return
        if parsed.path == "/api/applications":
            self._handle_application_update(self._read_json_body())
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _handle_jobs(self, query: dict[str, list[str]]) -> None:
        limit = _int_query(query, "limit", 100)
        min_score = _float_query(query, "min_score", 0)
        status = _first_query(query, "status") or None
        text_query = _first_query(query, "query")
        keyword_terms = unique_terms(query.get("keyword", []))
        related_terms = expand_related_topics(
            query.get("related_to", []), self.server_context.config.related_keyword_groups
        )
        rows = list_ranked_jobs(
            self.server_context.data_path,
            limit=100000 if text_query or keyword_terms or related_terms else limit,
            min_score=min_score,
            status=status,
        )
        terms = unique_terms([*(keyword_terms or []), *(related_terms or [])])
        if terms:
            rows = filter_rows_by_terms(rows, terms)
        if text_query:
            rows = _filter_rows_by_text(rows, text_query)
        rows = rows[:limit]
        self._send_json(
            {
                "jobs": [_list_row(row) for row in rows],
                "total": len(rows),
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
    path = Path(data_path) / "observation_template.md"
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


def _list_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": row.get("job_id"),
        "score": row.get("score"),
        "estimated_fit_probability": row.get("estimated_fit_probability"),
        "recommendation": row.get("recommendation"),
        "status": row.get("application_status"),
        "title": row.get("title"),
        "company": row.get("company"),
        "location": row.get("location"),
        "site": row.get("site"),
        "date_posted": row.get("date_posted"),
        "last_applied_at": row.get("applied_at"),
        "observations": row.get("application_notes"),
    }


def _ranked_row(item: Any) -> dict[str, Any]:
    return {
        "job_id": item.job_id,
        "score": item.score.score,
        "estimated_fit_probability": item.score.estimated_fit_probability,
        "recommendation": item.score.recommendation,
        "status": "new",
        "title": item.job.get("title"),
        "company": item.job.get("company"),
        "location": item.job.get("location"),
        "site": item.job.get("site"),
        "date_posted": item.job.get("date_posted"),
    }


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


def _float_query(query: dict[str, list[str]], name: str, default: float) -> float:
    try:
        return float(_first_query(query, name) or default)
    except ValueError:
        return default


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Job Finger</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d8dde6;
      --text: #1c2430;
      --muted: #667084;
      --accent: #136f63;
      --accent-dark: #0d4d45;
      --warn: #9f580a;
      --bad: #9a3412;
      --good: #0f766e;
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
      gap: 10px;
      padding: 14px 18px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 {
      margin: 0;
      font-size: 18px;
      line-height: 1.2;
      font-weight: 700;
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
      border-radius: 6px;
      padding: 8px 9px;
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
      border-radius: 6px;
      padding: 8px 11px;
      cursor: pointer;
      white-space: nowrap;
    }
    button.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }
    button.primary:hover { background: var(--accent-dark); }
    .toolbar {
      display: grid;
      grid-template-columns: minmax(180px, 1.2fr) minmax(150px, .8fr) 120px 90px 120px auto auto;
      gap: 10px;
      align-items: end;
    }
    main {
      display: grid;
      grid-template-columns: minmax(340px, 40%) minmax(420px, 1fr);
      min-height: calc(100vh - 114px);
    }
    aside {
      border-right: 1px solid var(--line);
      background: var(--panel);
      min-width: 0;
    }
    .filters {
      display: grid;
      grid-template-columns: 1fr 120px 92px;
      gap: 8px;
      padding: 12px;
      border-bottom: 1px solid var(--line);
      align-items: end;
    }
    .job-list {
      overflow: auto;
      max-height: calc(100vh - 190px);
    }
    .job-row {
      width: 100%;
      display: grid;
      grid-template-columns: 52px 1fr;
      gap: 10px;
      padding: 12px;
      border: 0;
      border-bottom: 1px solid var(--line);
      border-radius: 0;
      text-align: left;
      background: #fff;
    }
    .job-row:hover, .job-row.active {
      background: #eef7f5;
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
      font-size: 14px;
      line-height: 1.25;
      margin-bottom: 4px;
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
      padding: 2px 7px;
      background: #fff;
      color: var(--muted);
      font-size: 12px;
    }
    section.detail {
      min-width: 0;
      overflow: auto;
      max-height: calc(100vh - 114px);
    }
    .detail-head {
      padding: 18px;
      border-bottom: 1px solid var(--line);
      background: #fff;
    }
    .detail-title {
      margin: 0 0 6px;
      font-size: 22px;
      line-height: 1.2;
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
      padding: 12px 18px 0;
      background: #fff;
    }
    .tabs button.active {
      border-color: var(--accent);
      color: var(--accent-dark);
      font-weight: 700;
    }
    .pane {
      padding: 18px;
      display: none;
    }
    .pane.active { display: block; }
    .description {
      white-space: pre-wrap;
      line-height: 1.5;
      font-size: 14px;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 14px;
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
      .toolbar, main, .filters, .form-grid {
        grid-template-columns: 1fr;
      }
      section.detail, .job-list {
        max-height: none;
      }
    }
  </style>
</head>
<body>
  <header>
    <h1>Job Finger</h1>
    <div class="toolbar">
      <label>Keywords <input id="searchKeywords" placeholder="python, fastapi"></label>
      <label>Related <input id="searchRelated" placeholder="backend, ai"></label>
      <label>Location <input id="searchLocation" value="Portugal"></label>
      <label>Results <input id="searchResults" type="number" min="1" max="500" value="50"></label>
      <label>Site
        <select id="searchSite">
          <option value="indeed,linkedin">Indeed + LinkedIn</option>
          <option value="indeed">Indeed</option>
          <option value="linkedin">LinkedIn</option>
          <option value="google">Google</option>
        </select>
      </label>
      <button class="primary" id="runSearch">Search Boards</button>
      <button id="refresh">Refresh</button>
    </div>
  </header>
  <main>
    <aside>
      <div class="filters">
        <label>Local Search <input id="localQuery" placeholder="company, skill, title"></label>
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
        <label>Min Score <input id="minScore" type="number" min="0" max="100" value="0"></label>
      </div>
      <div id="jobList" class="job-list"></div>
    </aside>
    <section class="detail" id="detail">
      <div class="empty">No job selected.</div>
    </section>
  </main>
  <script>
    const state = { jobs: [], selectedId: null, detail: null, activeTab: "post" };
    const $ = (id) => document.getElementById(id);

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

    async function loadJobs() {
      const params = new URLSearchParams();
      const query = $("localQuery").value.trim();
      const status = $("statusFilter").value;
      const minScore = $("minScore").value;
      if (query) params.set("query", query);
      if (status) params.set("status", status);
      if (minScore) params.set("min_score", minScore);
      params.set("limit", "250");
      const data = await api(`/api/jobs?${params.toString()}`);
      state.jobs = data.jobs;
      renderList();
      if (state.selectedId && state.jobs.some(job => job.job_id === state.selectedId)) {
        await selectJob(state.selectedId);
      } else if (state.jobs[0]) {
        await selectJob(state.jobs[0].job_id);
      } else {
        state.selectedId = null;
        $("detail").innerHTML = '<div class="empty">No jobs found.</div>';
      }
    }

    function renderList() {
      const list = $("jobList");
      list.innerHTML = "";
      if (!state.jobs.length) {
        list.innerHTML = '<div class="empty">No jobs.</div>';
        return;
      }
      for (const job of state.jobs) {
        const row = document.createElement("button");
        row.className = `job-row ${job.job_id === state.selectedId ? "active" : ""}`;
        row.onclick = () => selectJob(job.job_id);
        row.innerHTML = `
          <div class="score">${Math.round(job.score || 0)}</div>
          <div>
            <div class="job-title">${escapeHtml(job.title || "Untitled")}</div>
            <div class="meta">${escapeHtml(job.company || "")} · ${escapeHtml(job.location || "")}</div>
            <div class="status-line">
              <span class="pill">${escapeHtml(job.status || "new")}</span>
              <span class="pill">${escapeHtml(job.site || "")}</span>
              ${job.last_applied_at ? `<span class="pill">Applied ${escapeHtml(formatDate(job.last_applied_at))}</span>` : ""}
            </div>
          </div>`;
        list.appendChild(row);
      }
    }

    async function selectJob(jobId) {
      state.selectedId = jobId;
      state.detail = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      renderList();
      renderDetail();
    }

    function renderDetail() {
      const payload = state.detail;
      if (!payload) return;
      const job = payload.job;
      const raw = job.raw_job || {};
      const description = raw.description || job.description || "";
      $("detail").innerHTML = `
        <div class="detail-head">
          <h2 class="detail-title">${escapeHtml(job.title || "Untitled")}</h2>
          <div class="meta">${escapeHtml(job.company || "")} · ${escapeHtml(job.location || "")} · Score ${escapeHtml(job.score ?? "")}</div>
          <div class="status-line">
            <span class="pill">${escapeHtml(job.application_status || "new")}</span>
            ${payload.last_applied_at ? `<span class="pill">Last applied ${escapeHtml(formatDate(payload.last_applied_at))}</span>` : ""}
            ${job.next_action_at ? `<span class="pill">Next ${escapeHtml(job.next_action_at)}</span>` : ""}
          </div>
          <div class="links">
            ${job.job_url ? `<a href="${escapeAttr(job.job_url)}" target="_blank" rel="noreferrer">Job Post</a>` : ""}
            ${job.job_url_direct ? `<a href="${escapeAttr(job.job_url_direct)}" target="_blank" rel="noreferrer">Direct Apply</a>` : ""}
          </div>
        </div>
        <div class="tabs">
          ${tabButton("post", "Post")}
          ${tabButton("application", "Application")}
          ${tabButton("data", "All Data")}
        </div>
        <div id="pane-post" class="pane ${state.activeTab === "post" ? "active" : ""}">
          <div class="description">${escapeHtml(description || "No description captured.")}</div>
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
    }

    function tabButton(id, label) {
      return `<button data-tab="${id}" class="${state.activeTab === id ? "active" : ""}">${label}</button>`;
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
              <div class="small">${escapeHtml(event.updated_at || "")}${event.applied_at ? ` · applied ${escapeHtml(event.applied_at)}` : ""}</div>
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
      $("runSearch").textContent = "Searching";
      try {
        await api("/api/search", { method: "POST", body: JSON.stringify(payload) });
        await loadJobs();
      } finally {
        $("runSearch").disabled = false;
        $("runSearch").textContent = "Search Boards";
      }
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, char => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
      }[char]));
    }
    function escapeAttr(value) { return escapeHtml(value); }
    function formatDate(value) { return String(value || "").replace("T", " ").replace("+00:00", ""); }

    $("runSearch").onclick = runSearch;
    $("refresh").onclick = loadJobs;
    ["localQuery", "statusFilter", "minScore"].forEach(id => {
      $(id).addEventListener("change", loadJobs);
      $(id).addEventListener("keyup", event => { if (event.key === "Enter") loadJobs(); });
    });
    loadJobs();
  </script>
</body>
</html>"""
