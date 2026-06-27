from __future__ import annotations

import math
from typing import Any, Mapping

from job_finger.resume import COMMON_SKILLS, extract_resume_keywords, normalize_text
from job_finger.search_terms import unique_terms


SENIORITY_TERMS: dict[str, list[str]] = {
    "intern": ["intern", "internship", "trainee", "estagio"],
    "junior": ["junior", "jr", "entry level", "graduate"],
    "mid": ["mid", "mid level", "pleno"],
    "senior": ["senior", "sr", "lead", "staff", "principal", "manager"],
}


def analyze_job_match(
    job: Mapping[str, Any],
    profile: Any,
    *,
    matched_keywords: list[str] | None = None,
    missing_must_haves: list[str] | None = None,
    penalties: list[str] | None = None,
) -> dict[str, Any]:
    text = _job_text(job)
    normalized = normalize_job_fields(job)
    profile_terms = _profile_terms(profile)
    resume_keywords = unique_terms(getattr(profile, "resume_keywords", []))
    job_skills = extract_resume_keywords(
        text,
        extra_terms=[*COMMON_SKILLS, *profile_terms, *resume_keywords],
    )
    cv_matches = _matching_terms(text, resume_keywords)
    profile_matches = _matching_terms(text, profile_terms)
    avoid_matches = _matching_terms(text, getattr(profile, "avoid_keywords", []))
    cv_gaps = _cv_skill_gaps(job_skills, resume_keywords)
    cv_evidence = _cv_evidence(cv_matches, _resume_evidence(profile))
    cv_match_strength = _match_strength(cv_matches, cv_gaps)
    positive_keywords = unique_terms(
        [
            *(matched_keywords or []),
            *cv_matches,
            *profile_matches,
        ]
    )
    negative_keywords = unique_terms([*avoid_matches, *(penalties or [])])
    cover_letter_keywords = unique_terms(
        [
            *cv_matches[:8],
            *profile_matches[:6],
            *job_skills[:8],
        ]
    )[:12]
    explanation = _match_explanation(
        normalized=normalized,
        cv_matches=cv_matches,
        cv_gaps=cv_gaps,
        cv_evidence=cv_evidence,
        job_skills=job_skills,
        missing_must_haves=missing_must_haves or [],
        penalties=penalties or [],
    )
    suggestions = _application_suggestions(
        normalized=normalized,
        cv_matches=cv_matches,
        cv_gaps=cv_gaps,
        cv_evidence=cv_evidence,
        cover_letter_keywords=cover_letter_keywords,
        negative_keywords=negative_keywords,
    )
    return {
        "normalized": normalized,
        "job_skills": job_skills,
        "cv_matched_keywords": cv_matches,
        "cv_missing_keywords": cv_gaps,
        "cv_evidence": cv_evidence,
        "cv_match_strength": cv_match_strength,
        "positive_keywords": positive_keywords,
        "negative_keywords": negative_keywords,
        "cover_letter_keywords": cover_letter_keywords,
        "match_explanation": explanation,
        "application_suggestions": suggestions,
        "cover_letter_draft": draft_cover_letter(
            job,
            normalized=normalized,
            cv_matches=cv_matches,
            cover_letter_keywords=cover_letter_keywords,
            cv_gaps=cv_gaps,
            cv_evidence=cv_evidence,
        ),
    }


def normalize_job_fields(job: Mapping[str, Any]) -> dict[str, Any]:
    min_salary = _safe_float(job.get("min_amount"))
    max_salary = _safe_float(job.get("max_amount"))
    return {
        "salary_min": min_salary,
        "salary_max": max_salary,
        "salary_currency": job.get("currency"),
        "salary_interval": job.get("interval"),
        "salary_label": salary_label(job),
        "salary_source": job.get("salary_source"),
        "work_mode": infer_work_mode(job),
        "seniority": infer_seniority(job),
        "employment_type": normalize_text(job.get("job_type")) or None,
        "published_at": job.get("date_posted"),
    }


def infer_work_mode(job: Mapping[str, Any]) -> str:
    if _truthy(job.get("is_remote")):
        return "remote"
    text = _job_text(job)
    if "remote" in text or "teletrabalho" in text or "work from home" in text:
        return "remote"
    if "hybrid" in text or "hibrido" in text or "hibrida" in text:
        return "hybrid"
    if "presencial" in text or "in office" in text or "on site" in text or "onsite" in text:
        return "office"
    return "unknown"


def infer_seniority(job: Mapping[str, Any]) -> str:
    text = _job_text(job)
    title = normalize_text(job.get("title"))
    for level, terms in SENIORITY_TERMS.items():
        if any(_term_in_text(term, title) for term in terms):
            return level
    for level, terms in SENIORITY_TERMS.items():
        if any(_term_in_text(term, text) for term in terms):
            return level
    return "unknown"


def salary_label(job: Mapping[str, Any]) -> str:
    min_amount = _safe_float(job.get("min_amount"))
    max_amount = _safe_float(job.get("max_amount"))
    currency = str(job.get("currency") or "").strip()
    interval = str(job.get("interval") or "").strip()
    if min_amount is not None and max_amount is not None and min_amount != max_amount:
        label = f"{_compact_money(min_amount)}-{_compact_money(max_amount)} {currency}"
    elif max_amount is not None or min_amount is not None:
        label = f"{_compact_money(max_amount if max_amount is not None else min_amount)} {currency}"
    else:
        return ""
    return f"{label.strip()} {interval}".strip()


def draft_cover_letter(
    job: Mapping[str, Any],
    *,
    normalized: Mapping[str, Any],
    cv_matches: list[str],
    cover_letter_keywords: list[str],
    cv_gaps: list[str],
    cv_evidence: list[dict[str, Any]],
) -> str:
    title = str(job.get("title") or "this role")
    company = str(job.get("company") or "your team")
    strengths = _join_terms(cv_matches[:4] or cover_letter_keywords[:4])
    focus = _join_terms(cover_letter_keywords[:6])
    work_mode = normalized.get("work_mode")
    gap_note = ""
    if cv_gaps:
        gap_note = (
            " I would keep the note concise around adjacent experience for "
            f"{_join_terms(cv_gaps[:3])} rather than over-claiming it."
        )
    evidence_note = ""
    evidence = _first_evidence_snippet(cv_evidence)
    if evidence:
        evidence_note = f" A concrete CV proof point to cite is: {evidence}."
    if not strengths:
        strengths = "the requirements in the posting"
    if not focus:
        focus = "the role requirements"
    return (
        f"Dear Hiring Team,\n\n"
        f"I am interested in the {title} role at {company}. My background is a fit "
        f"for {strengths}, and I would emphasize practical delivery around {focus}. "
        f"The posting appears to be {work_mode or 'a'} work setup, which I can address "
        f"directly in the application.{evidence_note}{gap_note}\n\n"
        f"I would welcome the chance to discuss how my experience can help {company} "
        f"deliver on this role's priorities.\n"
    )


def _application_suggestions(
    *,
    normalized: Mapping[str, Any],
    cv_matches: list[str],
    cv_gaps: list[str],
    cv_evidence: list[dict[str, Any]],
    cover_letter_keywords: list[str],
    negative_keywords: list[str],
) -> list[str]:
    suggestions: list[str] = []
    if cv_matches:
        suggestions.append(f"Lead with CV evidence for {_join_terms(cv_matches[:5])}.")
    else:
        suggestions.append("Review manually: no direct CV keyword matches were found.")
    if cover_letter_keywords:
        suggestions.append(
            f"Use {_join_terms(cover_letter_keywords[:6])} as cover-letter anchors."
        )
    evidence = _first_evidence_snippet(cv_evidence)
    if evidence:
        suggestions.append(f"Cite this CV evidence: {evidence}")
    if cv_gaps:
        suggestions.append(f"Check or explain gaps around {_join_terms(cv_gaps[:5])}.")
    if normalized.get("salary_label"):
        suggestions.append(f"Salary signal captured: {normalized['salary_label']}.")
    else:
        suggestions.append("Salary is not explicit; ask or infer before prioritizing.")
    if normalized.get("work_mode") == "unknown":
        suggestions.append("Work mode is unclear; verify remote/hybrid/office setup.")
    if negative_keywords:
        suggestions.append(f"Negative signals found: {_join_terms(negative_keywords[:4])}.")
    return suggestions


def _match_explanation(
    *,
    normalized: Mapping[str, Any],
    cv_matches: list[str],
    cv_gaps: list[str],
    cv_evidence: list[dict[str, Any]],
    job_skills: list[str],
    missing_must_haves: list[str],
    penalties: list[str],
) -> list[str]:
    explanation: list[str] = []
    if cv_matches:
        explanation.append(
            f"CV matches {len(cv_matches)} signal(s): {_join_terms(cv_matches[:8])}."
        )
        evidence_terms = [str(item.get("keyword")) for item in cv_evidence]
        if evidence_terms:
            explanation.append(
                f"CV evidence is available for {_join_terms(evidence_terms[:6])}."
            )
    else:
        explanation.append(
            "No CV keyword matches yet; add/convert a CV or enrich resume_keywords."
        )
    if job_skills:
        explanation.append(
            f"Job appears to ask for {_join_terms(job_skills[:10])}."
        )
    if cv_gaps:
        explanation.append(f"Potential CV gaps: {_join_terms(cv_gaps[:8])}.")
    if missing_must_haves:
        explanation.append(
            f"Missing configured must-haves: {_join_terms(missing_must_haves[:8])}."
        )
    if penalties:
        explanation.append(f"Penalty signals: {_join_terms(penalties[:4])}.")
    explanation.append(
        "Normalized signals: "
        f"{normalized.get('seniority') or 'unknown'} seniority, "
        f"{normalized.get('work_mode') or 'unknown'} work mode, "
        f"{normalized.get('salary_label') or 'salary not shown'}."
    )
    return explanation


def _profile_terms(profile: Any) -> list[str]:
    resume_profile = getattr(profile, "resume_profile", {}) or {}
    return unique_terms(
        [
            *getattr(profile, "must_have_keywords", []),
            *getattr(profile, "nice_to_have_keywords", []),
            *getattr(profile, "target_titles", []),
            *getattr(profile, "target_seniority", []),
            *getattr(profile, "languages", []),
            *_list_value(resume_profile.get("keywords")),
            *_list_value(resume_profile.get("titles")),
            *_list_value(resume_profile.get("languages")),
            *_list_value(resume_profile.get("seniority")),
        ]
    )


def _resume_evidence(profile: Any) -> dict[str, list[str]]:
    resume_profile = getattr(profile, "resume_profile", {}) or {}
    raw_evidence = dict(resume_profile.get("evidence") or {})
    evidence: dict[str, list[str]] = {}
    for term, snippets in raw_evidence.items():
        values = _list_value(snippets)
        if values:
            evidence[str(term)] = values
    return evidence


def _cv_evidence(
    cv_matches: list[str], resume_evidence: Mapping[str, list[str]]
) -> list[dict[str, Any]]:
    evidence_items: list[dict[str, Any]] = []
    for term in cv_matches:
        snippets = _lookup_evidence(term, resume_evidence)
        if snippets:
            evidence_items.append({"keyword": term, "snippets": snippets[:2]})
    return evidence_items


def _lookup_evidence(
    term: str, resume_evidence: Mapping[str, list[str]]
) -> list[str]:
    normalized_term = normalize_text(term)
    snippets: list[str] = []
    for evidence_term, evidence_snippets in resume_evidence.items():
        if normalize_text(evidence_term) != normalized_term:
            continue
        for snippet in evidence_snippets:
            if snippet not in snippets:
                snippets.append(str(snippet))
    return snippets


def _match_strength(cv_matches: list[str], cv_gaps: list[str]) -> str:
    total = len(cv_matches) + len(cv_gaps)
    if total == 0:
        return "unknown"
    ratio = len(cv_matches) / total
    if ratio >= 0.75:
        return "strong"
    if ratio >= 0.45:
        return "partial"
    return "weak"


def _first_evidence_snippet(cv_evidence: list[dict[str, Any]]) -> str:
    for item in cv_evidence:
        snippets = item.get("snippets") or []
        if snippets:
            return str(snippets[0])
    return ""


def _list_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _cv_skill_gaps(job_skills: list[str], resume_keywords: list[str]) -> list[str]:
    if not resume_keywords:
        return []
    resume_set = {normalize_text(term) for term in resume_keywords}
    gaps = []
    for skill in job_skills:
        if normalize_text(skill) not in resume_set:
            gaps.append(skill)
    return gaps[:15]


def _matching_terms(text: str, terms: list[str]) -> list[str]:
    normalized = normalize_text(text)
    found = []
    for term in terms:
        clean = normalize_text(term)
        if clean and _term_in_text(clean, normalized):
            found.append(term)
    return unique_terms(found)


def _job_text(job: Mapping[str, Any]) -> str:
    fields = [
        "title",
        "company",
        "location",
        "description",
        "job_function",
        "company_industry",
        "job_level",
        "skills",
        "work_from_home_type",
    ]
    return normalize_text(" ".join(str(job.get(field) or "") for field in fields))


def _term_in_text(term: str, text: str) -> bool:
    normalized_term = normalize_text(term)
    if not normalized_term:
        return False
    if any(separator in normalized_term for separator in (" ", "/", ".", "#", "+")):
        return normalized_term in text
    return f" {normalized_term} " in f" {text} "


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


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return normalize_text(value) in {"true", "yes", "1", "remote"}


def _compact_money(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number >= 1000:
        return f"{number / 1000:g}k"
    return f"{number:g}"


def _join_terms(terms: list[str]) -> str:
    return ", ".join(str(term) for term in terms if str(term).strip())
