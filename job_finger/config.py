from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from job_finger.search_terms import DEFAULT_RELATED_KEYWORD_GROUPS, unique_terms


DEFAULT_CONFIG_PATH = Path("job_finger.config.json")


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
    storage_path: str = "job_finger_lake"
    related_keyword_groups: dict[str, list[str]] = field(default_factory=dict)
    source_path: Path | None = None

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], source_path: Path | None = None
    ) -> "JobFingerConfig":
        searches = [SearchSpec.from_dict(item) for item in data.get("searches", [])]
        if not searches:
            raise ValueError("Config must contain at least one search in 'searches'.")
        return cls(
            profile=UserProfile.from_dict(data.get("profile")),
            searches=searches,
            storage_path=str(data.get("storage_path", "job_finger_lake")),
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


def example_config() -> dict[str, Any]:
    return {
        "storage_path": "job_finger_lake",
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
            "resume_path": "resume.md",
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
    return config_path
