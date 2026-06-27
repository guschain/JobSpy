from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any


COMMON_SKILLS = [
    "python",
    "typescript",
    "javascript",
    "java",
    "go",
    "golang",
    "c#",
    ".net",
    "php",
    "ruby",
    "kotlin",
    "swift",
    "sql",
    "postgres",
    "postgresql",
    "mysql",
    "sqlite",
    "mongodb",
    "redis",
    "elasticsearch",
    "django",
    "fastapi",
    "flask",
    "node",
    "node.js",
    "express",
    "react",
    "next.js",
    "vue",
    "angular",
    "svelte",
    "html",
    "css",
    "tailwind",
    "aws",
    "azure",
    "gcp",
    "docker",
    "kubernetes",
    "terraform",
    "ansible",
    "linux",
    "git",
    "github actions",
    "gitlab ci",
    "ci/cd",
    "airflow",
    "dbt",
    "spark",
    "pandas",
    "numpy",
    "machine learning",
    "llm",
    "rag",
    "openai",
    "langchain",
    "pytorch",
    "tensorflow",
    "playwright",
    "selenium",
    "pytest",
    "rest api",
    "graphql",
    "microservices",
    "saas",
    "agile",
    "scrum",
    "product management",
    "analytics",
]

COMMON_TITLES = [
    "software engineer",
    "backend engineer",
    "frontend engineer",
    "full stack developer",
    "full-stack engineer",
    "data engineer",
    "analytics engineer",
    "machine learning engineer",
    "ai engineer",
    "devops engineer",
    "platform engineer",
    "sre",
    "qa engineer",
    "test automation engineer",
    "product manager",
    "product owner",
    "mobile engineer",
]

COMMON_LANGUAGES = [
    "english",
    "portuguese",
    "spanish",
    "french",
    "german",
    "italian",
]

SENIORITY_SIGNALS = [
    "intern",
    "trainee",
    "junior",
    "mid",
    "senior",
    "lead",
    "staff",
    "principal",
    "manager",
]


def convert_resume_to_markdown(input_path: str | Path, output_path: str | Path) -> Path:
    source = Path(input_path)
    target = Path(output_path)
    if not source.exists():
        raise FileNotFoundError(f"CV file not found: {source}")
    try:
        from markitdown import MarkItDown
    except ImportError as exc:
        raise RuntimeError(
            "MarkItDown is not installed. Run: uv sync"
        ) from exc

    result = MarkItDown().convert(str(source))
    text = str(getattr(result, "text_content", "") or "").strip()
    if not text:
        raise ValueError(f"MarkItDown did not extract text from {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n", encoding="utf-8")
    return target


def write_resume_profile(
    markdown_path: str | Path,
    output_path: str | Path,
    extra_terms: list[str] | None = None,
) -> Path:
    source = Path(markdown_path)
    if not source.exists():
        raise FileNotFoundError(f"CV markdown file not found: {source}")
    text = source.read_text(encoding="utf-8-sig")
    profile = analyze_resume_text(text, extra_terms=extra_terms)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def analyze_resume_text(
    text: str,
    extra_terms: list[str] | None = None,
) -> dict[str, Any]:
    normalized = normalize_text(text)
    keywords = extract_resume_keywords(text, extra_terms=extra_terms)
    titles = _extract_terms(normalized, COMMON_TITLES)
    languages = _extract_terms(normalized, COMMON_LANGUAGES)
    seniority = _extract_terms(normalized, SENIORITY_SIGNALS)
    evidence_terms = _unique_terms([*keywords, *titles, *languages, *seniority])
    return {
        "keywords": keywords,
        "titles": titles,
        "languages": languages,
        "seniority": seniority,
        "evidence": extract_term_evidence(text, evidence_terms),
        "source_chars": len(text),
        "summary_signals": _summary_signals(
            keywords=keywords,
            titles=titles,
            languages=languages,
            seniority=seniority,
        ),
    }


def extract_resume_keywords(text: str, extra_terms: list[str] | None = None) -> list[str]:
    normalized = normalize_text(text)
    terms = [*COMMON_SKILLS, *(extra_terms or [])]
    found: list[str] = []
    seen: set[str] = set()
    for term in terms:
        clean = normalize_text(term)
        if not clean or clean in seen:
            continue
        if _term_in_text(clean, normalized):
            seen.add(clean)
            found.append(term)
    return found


def extract_term_evidence(
    text: str,
    terms: list[str],
    *,
    max_snippets_per_term: int = 2,
    max_snippet_chars: int = 220,
) -> dict[str, list[str]]:
    evidence: dict[str, list[str]] = {}
    normalized_terms = []
    for term in _unique_terms(terms):
        clean_term = normalize_text(term)
        if clean_term:
            normalized_terms.append((term, clean_term))
    for line in _resume_lines(text):
        normalized_line = normalize_text(line)
        if not normalized_line:
            continue
        for term, clean_term in normalized_terms:
            snippets = evidence.setdefault(term, [])
            if len(snippets) >= max_snippets_per_term:
                continue
            if _term_in_text(clean_term, normalized_line):
                snippet = _trim_snippet(line, max_snippet_chars)
                if snippet not in snippets:
                    snippets.append(snippet)
    return {term: snippets for term, snippets in evidence.items() if snippets}


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9+#./\s-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _term_in_text(term: str, text: str) -> bool:
    if any(separator in term for separator in (" ", "/", ".", "#", "+")):
        return term in text
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


def _extract_terms(text: str, terms: list[str]) -> list[str]:
    found = []
    seen = set()
    for term in terms:
        clean = normalize_text(term)
        if clean and clean not in seen and _term_in_text(clean, text):
            seen.add(clean)
            found.append(term)
    return found


def _resume_lines(text: str) -> list[str]:
    lines = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        line = line.strip(" -*#\t")
        if len(line) >= 4:
            lines.append(line)
    if lines:
        return lines
    return [text.strip()] if text.strip() else []


def _trim_snippet(value: str, max_chars: int) -> str:
    snippet = re.sub(r"\s+", " ", value).strip()
    if len(snippet) <= max_chars:
        return snippet
    return snippet[: max_chars - 3].rstrip() + "..."


def _unique_terms(terms: list[str]) -> list[str]:
    found = []
    seen = set()
    for term in terms:
        clean = normalize_text(term)
        if clean and clean not in seen:
            seen.add(clean)
            found.append(term)
    return found


def _summary_signals(
    *,
    keywords: list[str],
    titles: list[str],
    languages: list[str],
    seniority: list[str],
) -> list[str]:
    signals = []
    if titles:
        signals.append(f"Titles detected: {', '.join(titles[:5])}")
    if keywords:
        signals.append(f"Skills detected: {', '.join(keywords[:10])}")
    if languages:
        signals.append(f"Languages detected: {', '.join(languages[:5])}")
    if seniority:
        signals.append(f"Seniority signals: {', '.join(seniority[:5])}")
    return signals
