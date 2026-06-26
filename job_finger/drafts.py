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
    matched = _json_list(job.get("matched_keywords_json"))
    missing = _json_list(job.get("missing_must_haves_json"))
    reasons = _json_list(job.get("reasons_json"))

    resume_focus = matched[:8] or profile.must_have_keywords[:8]
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
        f"- URL: {job.get('job_url_direct') or job.get('job_url') or ''}",
        "",
        "## Why It Ranked This Way",
        "",
    ]
    lines.extend(f"- {reason}" for reason in reasons[:10])
    if not reasons:
        lines.append("- No detailed ranking reasons were recorded.")

    lines.extend(["", "## Resume Emphasis", ""])
    lines.extend(f"- {item}" for item in resume_focus)
    if not resume_focus:
        lines.append("- Add profile keywords in the config to generate stronger guidance.")

    lines.extend(["", "## Cover Letter Angle", ""])
    lines.extend(f"- {item}" for item in cover_letter_angles)

    if missing:
        lines.extend(["", "## Missing Or Weak Signals", ""])
        lines.extend(f"- {item}" for item in missing)

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
