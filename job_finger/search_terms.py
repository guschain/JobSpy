from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Mapping


DEFAULT_RELATED_KEYWORD_GROUPS: dict[str, list[str]] = {
    "backend": [
        "backend engineer",
        "software engineer",
        "api",
        "python",
        "java",
        "go",
        "node",
        "django",
        "fastapi",
        "spring",
        "microservices",
        "postgres",
    ],
    "frontend": [
        "frontend engineer",
        "react",
        "typescript",
        "javascript",
        "vue",
        "angular",
        "next.js",
        "css",
        "web performance",
    ],
    "fullstack": [
        "full stack developer",
        "full-stack engineer",
        "react",
        "typescript",
        "python",
        "node",
        "api",
        "postgres",
    ],
    "data": [
        "data engineer",
        "analytics engineer",
        "sql",
        "python",
        "dbt",
        "airflow",
        "spark",
        "snowflake",
        "bigquery",
    ],
    "ai": [
        "machine learning engineer",
        "ai engineer",
        "llm",
        "rag",
        "python",
        "pytorch",
        "tensorflow",
        "langchain",
        "openai",
    ],
    "devops": [
        "devops engineer",
        "platform engineer",
        "sre",
        "aws",
        "azure",
        "gcp",
        "kubernetes",
        "docker",
        "terraform",
        "ci/cd",
    ],
    "security": [
        "security engineer",
        "cybersecurity",
        "application security",
        "cloud security",
        "soc",
        "siem",
        "penetration testing",
    ],
    "qa": [
        "qa engineer",
        "test automation",
        "playwright",
        "selenium",
        "cypress",
        "pytest",
        "quality assurance",
    ],
    "product": [
        "product manager",
        "product owner",
        "roadmap",
        "discovery",
        "analytics",
        "stakeholders",
        "saas",
    ],
    "mobile": [
        "mobile engineer",
        "android",
        "ios",
        "react native",
        "flutter",
        "kotlin",
        "swift",
    ],
}


def merge_related_groups(
    custom_groups: Mapping[str, Iterable[str]] | None,
) -> dict[str, list[str]]:
    groups = {key: list(value) for key, value in DEFAULT_RELATED_KEYWORD_GROUPS.items()}
    for key, value in (custom_groups or {}).items():
        groups[normalize_text(key)] = unique_terms(value)
    return groups


def expand_related_topics(
    related_to: Iterable[str] | None,
    groups: Mapping[str, Iterable[str]] | None = None,
) -> list[str]:
    merged_groups = merge_related_groups(groups)
    expanded: list[str] = []
    for topic in related_to or []:
        normalized = normalize_text(topic)
        expanded.append(topic)
        expanded.extend(merged_groups.get(normalized, []))
    return unique_terms(expanded)


def build_keyword_query(
    *,
    keywords: Iterable[str] | None = None,
    related_to: Iterable[str] | None = None,
    groups: Mapping[str, Iterable[str]] | None = None,
    match: str = "any",
) -> tuple[str, list[str]]:
    terms = unique_terms(
        [*(keywords or []), *expand_related_topics(related_to, groups)]
    )
    quoted_terms = [_quote_for_job_board(term) for term in terms if term.strip()]
    if not quoted_terms:
        raise ValueError("At least one keyword or related topic is required.")
    if match == "all":
        return " ".join(quoted_terms), terms
    return " OR ".join(quoted_terms), terms


def filter_rows_by_terms(
    rows: Iterable[Mapping[str, Any]],
    terms: Iterable[str],
    match: str = "any",
) -> list[dict[str, Any]]:
    normalized_terms = [normalize_text(term) for term in terms if normalize_text(term)]
    if not normalized_terms:
        return [dict(row) for row in rows]
    filtered = []
    for row in rows:
        text = _row_text(row)
        if match == "all":
            keep = all(term in text for term in normalized_terms)
        else:
            keep = any(term in text for term in normalized_terms)
        if keep:
            filtered.append(dict(row))
    return filtered


def unique_terms(values: Iterable[str] | None) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    for value in values or []:
        value = str(value).strip()
        normalized = normalize_text(value)
        if not value or not normalized or normalized in seen:
            continue
        seen.add(normalized)
        terms.append(value)
    return terms


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9+#./\s-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _quote_for_job_board(term: str) -> str:
    cleaned = str(term).strip().replace('"', '\\"')
    if not cleaned:
        return ""
    if _looks_like_query_expression(cleaned):
        return cleaned
    if " " in cleaned or "." in cleaned or "/" in cleaned:
        return f'"{cleaned}"'
    return cleaned


def _looks_like_query_expression(value: str) -> bool:
    return any(operator in value for operator in (" OR ", " AND ", "(", ")", '"'))


def _row_text(row: Mapping[str, Any]) -> str:
    fields = [
        "title",
        "company",
        "location",
        "description",
        "company_industry",
        "job_type",
        "recommendation",
        "matched_keywords",
        "missing_must_haves",
        "reasons",
        "raw_job",
    ]
    return normalize_text(" ".join(str(row.get(field, "")) for field in fields))
