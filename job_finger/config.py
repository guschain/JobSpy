from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from job_finger.resume import analyze_resume_text, extract_resume_keywords
from job_finger.search_terms import DEFAULT_RELATED_KEYWORD_GROUPS, unique_terms


DEFAULT_WORKSPACE_PATH = Path("workspace")
DEFAULT_CONFIG_PATH = DEFAULT_WORKSPACE_PATH / "config.json"


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


@dataclass(frozen=True)
class UserProfile:
    name: str = "Job seeker"
    base_location: str = "Portugal"
    target_titles: list[str] = field(default_factory=list)
    target_seniority: list[str] = field(default_factory=list)
    must_have_keywords: list[str] = field(default_factory=list)
    nice_to_have_keywords: list[str] = field(default_factory=list)
    avoid_keywords: list[str] = field(default_factory=list)
    company_blacklist: list[str] = field(default_factory=list)
    preferred_locations: list[str] = field(default_factory=lambda: ["Portugal"])
    remote_preference: str = "remote_or_hybrid"
    minimum_salary_eur: int | None = None
    languages: list[str] = field(default_factory=list)
    resume_path: str | None = None
    resume_keywords: list[str] = field(default_factory=list)
    resume_profile_path: str | None = None
    resume_profile: dict[str, Any] = field(default_factory=dict)
    cover_letter_template_path: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "UserProfile":
        data = data or {}
        return cls(
            name=str(data.get("name", "Job seeker")),
            base_location=str(data.get("base_location", "Portugal")),
            target_titles=_string_list(data.get("target_titles")),
            target_seniority=_string_list(data.get("target_seniority")),
            must_have_keywords=_string_list(data.get("must_have_keywords")),
            nice_to_have_keywords=_string_list(data.get("nice_to_have_keywords")),
            avoid_keywords=_string_list(data.get("avoid_keywords")),
            company_blacklist=_string_list(data.get("company_blacklist")),
            preferred_locations=_string_list(
                data.get("preferred_locations", ["Portugal"])
            ),
            remote_preference=str(data.get("remote_preference", "remote_or_hybrid")),
            minimum_salary_eur=_optional_int(data.get("minimum_salary_eur")),
            languages=_string_list(data.get("languages")),
            resume_path=data.get("resume_path"),
            resume_keywords=_string_list(data.get("resume_keywords")),
            resume_profile_path=data.get("resume_profile_path"),
            resume_profile=dict(data.get("resume_profile", {})),
            cover_letter_template_path=data.get("cover_letter_template_path"),
        )


@dataclass(frozen=True)
class SearchSpec:
    name: str
    search_term: str
    location: str = "Portugal"
    site_name: list[str] = field(default_factory=lambda: ["indeed", "linkedin"])
    google_search_term: str | None = None
    results_wanted: int = 50
    hours_old: int | None = 168
    distance: int | None = 50
    country_indeed: str = "Portugal"
    is_remote: bool = False
    job_type: str | None = None
    easy_apply: bool | None = None
    description_format: str = "plain"
    linkedin_fetch_description: bool = True
    focus_keywords: list[str] = field(default_factory=list)
    required_keywords: list[str] = field(default_factory=list)
    related_to: list[str] = field(default_factory=list)
    extra_scrape_kwargs: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SearchSpec":
        return cls(
            name=str(data["name"]),
            search_term=str(data["search_term"]),
            location=str(data.get("location", "Portugal")),
            site_name=_string_list(data.get("site_name", ["indeed", "linkedin"])),
            google_search_term=data.get("google_search_term"),
            results_wanted=int(data.get("results_wanted", 50)),
            hours_old=_optional_int(data.get("hours_old", 168)),
            distance=_optional_int(data.get("distance", 50)),
            country_indeed=str(data.get("country_indeed", "Portugal")),
            is_remote=bool(data.get("is_remote", False)),
            job_type=data.get("job_type"),
            easy_apply=data.get("easy_apply"),
            description_format=str(data.get("description_format", "plain")),
            linkedin_fetch_description=bool(
                data.get("linkedin_fetch_description", True)
            ),
            focus_keywords=_string_list(data.get("focus_keywords")),
            required_keywords=_string_list(data.get("required_keywords")),
            related_to=_string_list(data.get("related_to")),
            extra_scrape_kwargs=dict(data.get("extra_scrape_kwargs", {})),
        )

    def to_scrape_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "site_name": self.site_name,
            "search_term": self.search_term,
            "location": self.location,
            "results_wanted": self.results_wanted,
            "country_indeed": self.country_indeed,
            "description_format": self.description_format,
            "linkedin_fetch_description": self.linkedin_fetch_description,
            "is_remote": self.is_remote,
            "verbose": 1,
        }
        optional_values = {
            "google_search_term": self.google_search_term,
            "hours_old": self.hours_old,
            "distance": self.distance,
            "job_type": self.job_type,
            "easy_apply": self.easy_apply,
        }
        kwargs.update(
            {key: value for key, value in optional_values.items() if value is not None}
        )
        kwargs.update(self.extra_scrape_kwargs)
        return kwargs


@dataclass(frozen=True)
class JobFingerConfig:
    profile: UserProfile
    searches: list[SearchSpec]
    storage_path: str = "data"
    related_keyword_groups: dict[str, list[str]] = field(default_factory=dict)
    source_path: Path | None = None

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], source_path: Path | None = None
    ) -> "JobFingerConfig":
        searches = [SearchSpec.from_dict(item) for item in data.get("searches", [])]
        if not searches:
            raise ValueError("Config must contain at least one search in 'searches'.")
        profile = UserProfile.from_dict(data.get("profile"))
        profile = _load_resume_keywords(profile, source_path)
        return cls(
            profile=profile,
            searches=searches,
            storage_path=str(data.get("storage_path", "data")),
            related_keyword_groups={
                str(key): unique_terms(value)
                for key, value in data.get("related_keyword_groups", {}).items()
            },
            source_path=source_path,
        )

    def resolve_storage_path(self, override: str | None = None) -> Path:
        raw_path = Path(override or self.storage_path)
        if raw_path.is_absolute() or not self.source_path:
            return raw_path
        return self.source_path.parent / raw_path

    def selected_searches(self, names: list[str] | None = None) -> list[SearchSpec]:
        if not names:
            return self.searches
        wanted = set(names)
        selected = [search for search in self.searches if search.name in wanted]
        missing = sorted(wanted - {search.name for search in selected})
        if missing:
            raise ValueError(f"Unknown search name(s): {', '.join(missing)}")
        return selected


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> JobFingerConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)
    return JobFingerConfig.from_dict(data, source_path=config_path)


def _load_resume_keywords(
    profile: UserProfile, source_path: Path | None
) -> UserProfile:
    resume_profile = _load_resume_profile(profile, source_path)
    if not profile.resume_path:
        return replace(profile, resume_profile=resume_profile)
    resume_path = _resolve_user_file(profile.resume_path, source_path)
    if not resume_path.exists() or resume_path.suffix.lower() == ".pdf":
        return replace(profile, resume_profile=resume_profile)
    text = resume_path.read_text(encoding="utf-8-sig")
    generated_profile = analyze_resume_text(
        text,
        extra_terms=[
            *profile.must_have_keywords,
            *profile.nice_to_have_keywords,
            *profile.target_titles,
            *_string_list(resume_profile.get("titles")),
        ],
    )
    resume_profile = _merge_resume_profiles(resume_profile, generated_profile)
    keywords = unique_terms(
        [
            *profile.resume_keywords,
            *_string_list(resume_profile.get("keywords")),
            *extract_resume_keywords(
                text,
                extra_terms=[
                    *profile.must_have_keywords,
                    *profile.nice_to_have_keywords,
                    *profile.target_titles,
                    *_string_list(resume_profile.get("titles")),
                ],
            ),
        ]
    )
    target_titles = unique_terms(
        [
            *profile.target_titles,
            *_string_list(resume_profile.get("titles")),
        ]
    )
    languages = unique_terms(
        [
            *profile.languages,
            *_string_list(resume_profile.get("languages")),
        ]
    )
    return replace(
        profile,
        resume_keywords=keywords,
        target_titles=target_titles,
        languages=languages,
        resume_profile=resume_profile,
    )


def _merge_resume_profiles(
    configured: dict[str, Any], generated: dict[str, Any]
) -> dict[str, Any]:
    merged = {**generated, **configured}
    for field_name in ("keywords", "titles", "languages", "seniority"):
        merged[field_name] = unique_terms(
            [
                *_string_list(generated.get(field_name)),
                *_string_list(configured.get(field_name)),
            ]
        )
    merged["evidence"] = _merge_evidence_maps(
        dict(generated.get("evidence") or {}),
        dict(configured.get("evidence") or {}),
    )
    generated_signals = _string_list(generated.get("summary_signals"))
    configured_signals = _string_list(configured.get("summary_signals"))
    merged["summary_signals"] = unique_terms([*generated_signals, *configured_signals])
    return merged


def _merge_evidence_maps(
    generated: dict[str, Any], configured: dict[str, Any]
) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for source in (generated, configured):
        for term, snippets in source.items():
            values = _string_list(snippets)
            if not values:
                continue
            existing = merged.setdefault(str(term), [])
            for value in values:
                if value not in existing:
                    existing.append(value)
    return merged


def _load_resume_profile(
    profile: UserProfile, source_path: Path | None
) -> dict[str, Any]:
    explicit_path = profile.resume_profile_path
    if explicit_path:
        profile_path = _resolve_user_file(explicit_path, source_path)
    elif profile.resume_path:
        profile_path = _resolve_user_file(profile.resume_path, source_path).with_name(
            "cv_profile.json"
        )
    elif source_path:
        profile_path = source_path.parent / "cv_profile.json"
    else:
        return dict(profile.resume_profile)
    if not profile_path.exists():
        return dict(profile.resume_profile)
    with profile_path.open("r", encoding="utf-8-sig") as file:
        loaded = json.load(file)
    if not isinstance(loaded, dict):
        return dict(profile.resume_profile)
    return {**profile.resume_profile, **loaded}


def _resolve_user_file(path: str, source_path: Path | None) -> Path:
    user_path = Path(path)
    if user_path.is_absolute() or not source_path:
        return user_path
    return source_path.parent / user_path


def example_config() -> dict[str, Any]:
    return {
        "storage_path": "data",
        "related_keyword_groups": DEFAULT_RELATED_KEYWORD_GROUPS,
        "profile": {
            "name": "Your Name",
            "base_location": "Portugal",
            "target_titles": [
                "software engineer",
                "backend engineer",
                "full stack developer",
            ],
            "target_seniority": ["mid", "senior"],
            "must_have_keywords": ["python", "typescript", "sql"],
            "nice_to_have_keywords": [
                "django",
                "fastapi",
                "react",
                "aws",
                "docker",
                "postgres",
            ],
            "avoid_keywords": ["unpaid", "commission only", "door to door"],
            "company_blacklist": [],
            "preferred_locations": ["Portugal", "Lisbon", "Porto", "Remote"],
            "remote_preference": "remote_or_hybrid",
            "minimum_salary_eur": 35000,
            "languages": ["English", "Portuguese"],
            "resume_path": "cv.md",
            "resume_keywords": [],
            "resume_profile_path": "cv_profile.json",
            "cover_letter_template_path": "cover_letter_template.md",
        },
        "searches": [
            {
                "name": "software-portugal",
                "site_name": ["indeed", "linkedin"],
                "search_term": '"software engineer" OR "backend engineer"',
                "location": "Portugal",
                "country_indeed": "Portugal",
                "results_wanted": 50,
                "hours_old": 168,
                "description_format": "plain",
                "linkedin_fetch_description": True,
            },
            {
                "name": "remote-europe",
                "site_name": ["indeed", "linkedin"],
                "search_term": '"python developer" OR "full stack developer"',
                "location": "European Union",
                "country_indeed": "Portugal",
                "results_wanted": 50,
                "hours_old": 168,
                "description_format": "plain",
                "linkedin_fetch_description": True,
                "is_remote": True,
            },
        ],
    }


def write_example_config(
    path: str | Path = DEFAULT_CONFIG_PATH, force: bool = False
) -> Path:
    config_path = Path(path)
    if config_path.exists() and not force:
        raise FileExistsError(f"{config_path} already exists. Pass --force to replace it.")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as file:
        json.dump(example_config(), file, indent=2)
        file.write("\n")
    ensure_workspace_files(config_path)
    return config_path


def ensure_workspace_files(config_path: str | Path = DEFAULT_CONFIG_PATH) -> Path:
    config_path = Path(config_path)
    workspace = config_path.parent
    for folder in ("data", "output/briefs", "output/cover_letters", "output/exports"):
        (workspace / folder).mkdir(parents=True, exist_ok=True)
    _write_if_missing(
        workspace / "observation_template.md",
        "\n".join(
            [
                "Status:",
                "Last applied:",
                "Next action:",
                "Notes:",
                "",
            ]
        ),
    )
    _write_if_missing(
        workspace / "cover_letter_template.md",
        "\n".join(
            [
                "Use one short paragraph for why this company/role.",
                "Use one concrete achievement that matches the job requirements.",
                "Close with availability and location/work-mode fit.",
                "",
            ]
        ),
    )
    return workspace


def _write_if_missing(path: Path, text: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
