from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from job_finger.config import UserProfile


def build_application_brief(job: Mapping[str, Any], profile: UserProfile) -> str:
    title = job.get("title") or "Unknown role"
    company = job.get("company") or "Unknown company"
    score = job.get("score")
    probability = job.get("estimated_fit_probability")
    matched = _json_list(job.get("matched_keywords", job.get("matched_keywords_json")))
    missing = _json_list(
        job.get("missing_must_haves", job.get("missing_must_haves_json"))
    )
    reasons = _json_list(job.get("reasons", job.get("reasons_json")))
    skills = _json_list(job.get("skills"))
    cv_matches = _json_list(job.get("cv_matched_keywords"))
    cv_gaps = _json_list(job.get("cv_missing_keywords"))
    suggestions = _json_list(job.get("application_suggestions"))
    explanation = _json_list(job.get("match_explanation"))
    cover_letter = str(job.get("cover_letter_draft") or "").strip()

    resume_focus = cv_matches[:8] or matched[:8] or profile.must_have_keywords[:8]
    cover_letter_angles = [
        f"Open with direct interest in {title} at {company}.",
        "Use one concrete project or metric that proves the strongest matched skill.",
        "Keep it short and specific to the role requirements.",
    ]
    if missing:
        cover_letter_angles.append(
            "Address weak or missing requirements only if you have adjacent evidence."
        )

    lines = [
        f"# Application Brief: {title}",
        "",
        f"- Company: {company}",
        f"- Fit score: {score}",
        f"- Estimated fit probability: {probability}%",
        f"- Status: {job.get('application_status', 'new')}",
        f"- Work mode: {job.get('work_mode') or 'unknown'}",
        f"- Seniority: {job.get('seniority') or 'unknown'}",
        f"- Salary: {job.get('salary_label') or 'not shown'}",
        f"- URL: {job.get('job_url_direct') or job.get('job_url') or ''}",
        "",
        "## Match Explanation",
        "",
    ]
    lines.extend(f"- {item}" for item in explanation[:10])
    if not explanation:
        lines.append("- No explicit CV/job match explanation was recorded.")

    lines.extend(["", "## Why It Ranked This Way", ""])
    lines.extend(f"- {reason}" for reason in reasons[:10])
    if not reasons:
        lines.append("- No detailed ranking reasons were recorded.")

    if skills:
        lines.extend(["", "## Skills Detected In Job", ""])
        lines.extend(f"- {item}" for item in skills[:15])

    if cv_matches:
        lines.extend(["", "## CV Matches", ""])
        lines.extend(f"- {item}" for item in cv_matches[:15])

    lines.extend(["", "## Resume Emphasis", ""])
    lines.extend(f"- {item}" for item in resume_focus)
    if not resume_focus:
        lines.append("- Add profile keywords in the config to generate stronger guidance.")

    lines.extend(["", "## Cover Letter Angle", ""])
    lines.extend(f"- {item}" for item in cover_letter_angles)

    if missing:
        lines.extend(["", "## Missing Or Weak Signals", ""])
        lines.extend(f"- {item}" for item in missing)

    if cv_gaps:
        lines.extend(["", "## CV Gaps To Handle Carefully", ""])
        lines.extend(f"- {item}" for item in cv_gaps[:12])

    if suggestions:
        lines.extend(["", "## Application Suggestions", ""])
        lines.extend(f"- {item}" for item in suggestions[:10])

    if cover_letter:
        lines.extend(["", "## Cover Letter Draft", "", cover_letter])

    return "\n".join(lines).strip() + "\n"


def write_application_brief(
    job: Mapping[str, Any], profile: UserProfile, output_path: str | Path
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_application_brief(job, profile), encoding="utf-8")
    return path


def _json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return [str(value)]
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return [str(parsed)]
