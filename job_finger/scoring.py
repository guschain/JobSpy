from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping

from job_finger.config import UserProfile
from job_finger.matching import analyze_job_match, normalize_job_fields


SENIORITY_TERMS = {
    "intern": ["intern", "internship", "trainee", "estagio"],
    "junior": ["junior", "jr", "entry level", "graduate"],
    "mid": ["mid", "mid level", "pleno"],
    "senior": ["senior", "sr"],
    "lead": ["lead", "staff", "principal", "manager", "head of"],
}


@dataclass(frozen=True)
class ScoreBreakdown:
    score: float
    estimated_fit_probability: int
    recommendation: str
    components: dict[str, float] = field(default_factory=dict)
    matched_keywords: list[str] = field(default_factory=list)
    missing_must_haves: list[str] = field(default_factory=list)
    penalties: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    analysis: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "estimated_fit_probability": self.estimated_fit_probability,
            "recommendation": self.recommendation,
            "components": self.components,
            "matched_keywords": self.matched_keywords,
            "missing_must_haves": self.missing_must_haves,
            "penalties": self.penalties,
            "reasons": self.reasons,
            "analysis": self.analysis,
        }


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9+#.\s-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _field(job: Mapping[str, Any], name: str) -> str:
    value = job.get(name)
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value)


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
    ]
    return normalize_text(" ".join(_field(job, field) for field in fields))


def _matches(text: str, keywords: list[str]) -> list[str]:
    found: list[str] = []
    for keyword in keywords:
        normalized = normalize_text(keyword)
        if normalized and normalized in text:
            found.append(keyword)
    return found


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value)
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


def _recency_score(value: Any, today: date | None = None) -> float:
    posted = _parse_date(value)
    if not posted:
        return 0.5
    today = today or date.today()
    age_days = max((today - posted).days, 0)
    if age_days <= 3:
        return 1.0
    if age_days <= 7:
        return 0.85
    if age_days <= 14:
        return 0.65
    if age_days <= 30:
        return 0.45
    return 0.2


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return normalize_text(value) in {"true", "yes", "1", "remote"}


def _salary_score(job: Mapping[str, Any], minimum_salary_eur: int | None) -> float | None:
    if not minimum_salary_eur:
        return None
    normalized = normalize_job_fields(job)
    currency = normalize_text(normalized.get("salary_currency"))
    min_amount = _safe_float(normalized.get("salary_annual_min"))
    max_amount = _safe_float(normalized.get("salary_annual_max"))
    best_amount = max(amount for amount in [min_amount, max_amount] if amount is not None) if any(
        amount is not None for amount in [min_amount, max_amount]
    ) else None
    if best_amount is None:
        return 0.45
    if currency and currency not in {"eur", "euro", ""}:
        return 0.5
    if best_amount >= minimum_salary_eur:
        return 1.0
    return max(0.0, min(best_amount / minimum_salary_eur, 0.9))


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


def _title_score(title: str, text: str, target_titles: list[str]) -> float | None:
    if not target_titles:
        return None
    title_matches = _matches(title, target_titles)
    if title_matches:
        return 1.0
    text_matches = _matches(text, target_titles)
    if text_matches:
        return 0.6
    title_tokens = set(title.split())
    best_overlap = 0.0
    for target in target_titles:
        target_tokens = set(normalize_text(target).split())
        if not target_tokens:
            continue
        best_overlap = max(
            best_overlap, len(title_tokens & target_tokens) / len(target_tokens)
        )
    return min(best_overlap, 0.75)


def _location_score(
    job: Mapping[str, Any],
    text: str,
    preferred_locations: list[str],
    remote_preference: str,
) -> float | None:
    wants_remote = remote_preference in {"remote", "remote_or_hybrid", "hybrid"}
    is_remote = _truthy(job.get("is_remote")) or " remote " in f" {text} "
    has_hybrid = "hybrid" in text or "hibrido" in text
    location_matches = _matches(normalize_text(job.get("location")), preferred_locations)
    if wants_remote and is_remote:
        return 1.0
    if wants_remote and remote_preference != "remote" and has_hybrid:
        return 0.9
    if preferred_locations and location_matches:
        return 1.0
    if wants_remote and "remote" in [normalize_text(item) for item in preferred_locations]:
        return 0.6
    return 0.35 if preferred_locations else None


def _seniority_score(title: str, text: str, targets: list[str]) -> float | None:
    if not targets:
        return None
    target_terms: list[str] = []
    for target in targets:
        normalized = normalize_text(target)
        target_terms.extend(SENIORITY_TERMS.get(normalized, [normalized]))
    if _matches(f"{title} {text}", target_terms):
        return 1.0
    all_terms = [term for terms in SENIORITY_TERMS.values() for term in terms]
    if _matches(title, all_terms):
        return 0.15
    return 0.55


def _recommendation(score: float) -> str:
    if score >= 82:
        return "priority"
    if score >= 70:
        return "strong"
    if score >= 55:
        return "review"
    return "low"


def score_job(
    job: Mapping[str, Any],
    profile: UserProfile,
    today: date | None = None,
    search_focus_keywords: list[str] | None = None,
    search_required_keywords: list[str] | None = None,
) -> ScoreBreakdown:
    text = _job_text(job)
    title = normalize_text(job.get("title"))
    company = normalize_text(job.get("company"))
    components: dict[str, float] = {}
    reasons: list[str] = []
    penalties: list[str] = []
    weighted_score = 0.0
    total_weight = 0.0

    def add_component(name: str, value: float | None, weight: float, reason: str) -> None:
        nonlocal weighted_score, total_weight
        if value is None:
            return
        clamped = max(0.0, min(value, 1.0))
        components[name] = round(clamped * 100, 1)
        weighted_score += clamped * weight
        total_weight += weight
        if clamped >= 0.75:
            reasons.append(reason)

    must_matches = _matches(text, profile.must_have_keywords)
    missing_must = [
        keyword for keyword in profile.must_have_keywords if keyword not in must_matches
    ]
    add_component(
        "must_have_keywords",
        len(must_matches) / len(profile.must_have_keywords)
        if profile.must_have_keywords
        else None,
        30,
        f"Matched must-have keywords: {', '.join(must_matches)}",
    )

    nice_matches = _matches(text, profile.nice_to_have_keywords)
    add_component(
        "nice_to_have_keywords",
        len(nice_matches) / len(profile.nice_to_have_keywords)
        if profile.nice_to_have_keywords
        else None,
        12,
        f"Matched nice-to-have keywords: {', '.join(nice_matches)}",
    )

    resume_matches = _matches(text, profile.resume_keywords)
    add_component(
        "resume_keywords",
        min(len(resume_matches) / 8, 1.0) if profile.resume_keywords else None,
        16,
        f"Matched CV keywords: {', '.join(resume_matches[:12])}",
    )

    add_component(
        "target_title",
        _title_score(title, text, profile.target_titles),
        18,
        "Role title aligns with target titles",
    )
    search_matches = _matches(text, search_focus_keywords or [])
    add_component(
        "search_focus_keywords",
        len(search_matches) / len(search_focus_keywords)
        if search_focus_keywords
        else None,
        12,
        f"Matched search focus keywords: {', '.join(search_matches)}",
    )
    required_matches = _matches(text, search_required_keywords or [])
    missing_required = [
        keyword
        for keyword in search_required_keywords or []
        if keyword not in required_matches
    ]
    add_component(
        "required_search_keywords",
        len(required_matches) / len(search_required_keywords)
        if search_required_keywords
        else None,
        14,
        f"Matched required search keywords: {', '.join(required_matches)}",
    )
    add_component(
        "location",
        _location_score(
            job, text, profile.preferred_locations, profile.remote_preference
        ),
        12,
        "Location or remote setup aligns with preferences",
    )
    add_component(
        "seniority",
        _seniority_score(title, text, profile.target_seniority),
        10,
        "Seniority appears aligned",
    )
    add_component(
        "salary",
        _salary_score(job, profile.minimum_salary_eur),
        8,
        "Salary appears compatible with target",
    )
    add_component(
        "languages",
        len(_matches(text, profile.languages)) / len(profile.languages)
        if profile.languages
        else None,
        5,
        "Language requirements look compatible",
    )
    add_component(
        "recency",
        _recency_score(job.get("date_posted"), today=today),
        5,
        "Posting is recent",
    )

    raw_score = (weighted_score / total_weight * 100) if total_weight else 0.0

    avoid_matches = _matches(text, profile.avoid_keywords)
    if avoid_matches:
        penalties.append(f"Avoid keywords found: {', '.join(avoid_matches)}")
    blacklisted = _matches(company, profile.company_blacklist)
    if blacklisted:
        penalties.append(f"Blacklisted company match: {', '.join(blacklisted)}")

    if missing_required:
        penalties.append(
            f"Missing required search keywords: {', '.join(missing_required)}"
        )

    penalty_points = (
        min(30, len(avoid_matches) * 12)
        + (35 if blacklisted else 0)
        + min(20, len(missing_required) * 8)
    )
    final_score = max(0.0, min(100.0, raw_score - penalty_points))
    estimated_probability = max(5, min(95, round(final_score)))

    matched_keywords = sorted(
        set(
            must_matches
            + nice_matches
            + resume_matches
            + search_matches
            + required_matches
        )
    )
    if missing_must:
        reasons.append(f"Missing must-have keywords: {', '.join(missing_must)}")
    if penalties:
        reasons.extend(penalties)
    analysis = analyze_job_match(
        job,
        profile,
        matched_keywords=matched_keywords,
        missing_must_haves=missing_must,
        penalties=penalties,
    )

    return ScoreBreakdown(
        score=round(final_score, 1),
        estimated_fit_probability=estimated_probability,
        recommendation=_recommendation(final_score),
        components=components,
        matched_keywords=matched_keywords,
        missing_must_haves=missing_must,
        penalties=penalties,
        reasons=reasons,
        analysis=analysis,
    )
