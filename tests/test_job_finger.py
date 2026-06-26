from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from job_finger.config import UserProfile, load_config
from job_finger.matching import analyze_job_match
from job_finger.pipeline import RankedJob
from job_finger.resume import analyze_resume_text, extract_resume_keywords, write_resume_profile
from job_finger.scoring import score_job
from job_finger.search_terms import (
    build_keyword_query,
    filter_rows_excluding_terms,
    filter_rows_by_terms,
)
from job_finger.storage import (
    JobLake,
    list_application_events,
    list_ranked_jobs,
    update_application,
)
from job_finger.ui_server import DEFAULT_OBSERVATION_TEMPLATE, read_observation_template


class ScoringTests(unittest.TestCase):
    def test_matching_job_scores_above_weak_job(self) -> None:
        profile = UserProfile(
            target_titles=["backend engineer"],
            target_seniority=["senior"],
            must_have_keywords=["python", "sql"],
            nice_to_have_keywords=["fastapi", "postgres"],
            preferred_locations=["Portugal", "Remote"],
            remote_preference="remote_or_hybrid",
            minimum_salary_eur=35000,
            languages=["English"],
        )
        strong_job = {
            "title": "Senior Backend Engineer",
            "company": "Example",
            "location": "Lisbon, Portugal",
            "description": "Python, SQL, FastAPI, Postgres. English required.",
            "is_remote": True,
            "min_amount": 40000,
            "currency": "EUR",
            "date_posted": "2026-06-25",
        }
        weak_job = {
            "title": "Door to Door Sales Representative",
            "company": "Example",
            "location": "Madrid, Spain",
            "description": "Commission only sales role.",
            "is_remote": False,
            "date_posted": "2026-06-01",
        }

        strong = score_job(strong_job, profile, today=date(2026, 6, 26))
        weak = score_job(weak_job, profile, today=date(2026, 6, 26))

        self.assertGreater(strong.score, weak.score)
        self.assertEqual(strong.recommendation, "priority")
        self.assertIn("python", [item.lower() for item in strong.matched_keywords])

    def test_search_keywords_influence_score(self) -> None:
        profile = UserProfile(
            target_titles=["software engineer"],
            preferred_locations=["Portugal"],
        )
        job = {
            "title": "AI Engineer",
            "company": "Example",
            "location": "Portugal",
            "description": "LLM and RAG systems with Python.",
            "date_posted": "2026-06-26",
        }

        score = score_job(
            job,
            profile,
            today=date(2026, 6, 26),
            search_focus_keywords=["llm", "rag"],
        )

        self.assertIn("search_focus_keywords", score.components)
        self.assertIn("llm", [item.lower() for item in score.matched_keywords])

    def test_resume_keywords_influence_score(self) -> None:
        profile = UserProfile(
            target_titles=["software engineer"],
            preferred_locations=["Portugal"],
            resume_keywords=["fastapi", "postgres", "docker"],
        )
        job = {
            "title": "Backend Software Engineer",
            "company": "Example",
            "location": "Portugal",
            "description": "Build APIs with FastAPI, Postgres, and Docker.",
        }

        score = score_job(job, profile, today=date(2026, 6, 26))

        self.assertIn("resume_keywords", score.components)
        self.assertIn("fastapi", [item.lower() for item in score.matched_keywords])
        self.assertIn("cv_matched_keywords", score.analysis)

    def test_job_match_analysis_finds_cv_matches_and_gaps(self) -> None:
        profile = UserProfile(
            resume_keywords=["python", "fastapi", "docker"],
            must_have_keywords=["python"],
            avoid_keywords=["commission only"],
        )
        job = {
            "title": "Senior Backend Engineer",
            "location": "Lisbon hybrid",
            "description": "Python, FastAPI, Kubernetes and Postgres.",
            "min_amount": 45000,
            "currency": "EUR",
            "interval": "yearly",
        }

        analysis = analyze_job_match(job, profile)

        self.assertEqual(analysis["normalized"]["work_mode"], "hybrid")
        self.assertEqual(analysis["normalized"]["seniority"], "senior")
        self.assertIn("python", [item.lower() for item in analysis["cv_matched_keywords"]])
        self.assertIn("kubernetes", [item.lower() for item in analysis["cv_missing_keywords"]])
        self.assertTrue(analysis["cover_letter_draft"])


class SearchTermTests(unittest.TestCase):
    def test_related_topic_expands_to_job_board_query(self) -> None:
        query, terms = build_keyword_query(
            keywords=["python"],
            related_to=["backend"],
            match="any",
        )

        self.assertIn("python", terms)
        self.assertIn("backend engineer", terms)
        self.assertIn(" OR ", query)

    def test_filter_rows_by_keywords(self) -> None:
        rows = [
            {"title": "Backend Engineer", "description": "Python and FastAPI"},
            {"title": "Product Manager", "description": "Roadmap and discovery"},
        ]

        filtered = filter_rows_by_terms(rows, ["fastapi"], match="any")

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["title"], "Backend Engineer")

    def test_filter_rows_can_exclude_terms_by_scope(self) -> None:
        rows = [
            {"title": "Senior Backend Engineer", "description": "Python"},
            {"title": "Backend Engineer", "description": "SAP integration"},
            {"title": "Backend Engineer", "description": "FastAPI"},
        ]

        no_senior_titles = filter_rows_excluding_terms(
            rows, ["senior"], scope="title"
        )
        no_sap_content = filter_rows_excluding_terms(
            rows, ["sap"], scope="content"
        )

        self.assertEqual(len(no_senior_titles), 2)
        self.assertEqual(len(no_sap_content), 2)
        self.assertEqual(no_sap_content[-1]["description"], "FastAPI")

    def test_extract_resume_keywords(self) -> None:
        text = "Built FastAPI services on PostgreSQL with Docker and GitHub Actions."

        keywords = extract_resume_keywords(text)

        self.assertIn("fastapi", [item.lower() for item in keywords])
        self.assertIn("docker", [item.lower() for item in keywords])

    def test_resume_profile_can_be_written_and_loaded_by_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "cv.md").write_text(
                "Senior Backend Engineer with Python, FastAPI and English.",
                encoding="utf-8",
            )
            write_resume_profile(root / "cv.md", root / "cv_profile.json")
            (root / "config.json").write_text(
                """
{
  "storage_path": "data",
  "profile": {
    "name": "Tester",
    "resume_path": "cv.md",
    "resume_profile_path": "cv_profile.json"
  },
  "searches": [
    {"name": "test", "search_term": "python", "location": "Portugal"}
  ]
}
""".strip()
                + "\n",
                encoding="utf-8",
            )

            config = load_config(root / "config.json")

        self.assertIn("fastapi", [item.lower() for item in config.profile.resume_keywords])
        self.assertIn("backend engineer", [item.lower() for item in config.profile.target_titles])
        self.assertIn("english", [item.lower() for item in config.profile.languages])

    def test_analyze_resume_text_extracts_structured_signals(self) -> None:
        profile = analyze_resume_text(
            "Senior Backend Engineer. Python, FastAPI, Docker. English and Portuguese."
        )

        self.assertIn("backend engineer", [item.lower() for item in profile["titles"]])
        self.assertIn("python", [item.lower() for item in profile["keywords"]])
        self.assertIn("english", [item.lower() for item in profile["languages"]])


class StorageTests(unittest.TestCase):
    def test_save_rank_and_track_in_file_lake(self) -> None:
        profile = UserProfile(
            target_titles=["software engineer"],
            must_have_keywords=["python"],
            preferred_locations=["Portugal"],
        )
        job = {
            "id": "test-1",
            "site": "indeed",
            "job_url": "https://example.com/job/1",
            "title": "Software Engineer",
            "company": "Example",
            "location": "Portugal",
            "description": "Python role",
            "date_posted": "2026-06-26",
        }
        breakdown = score_job(job, profile, today=date(2026, 6, 26))

        with tempfile.TemporaryDirectory() as temp_dir:
            data_path = Path(temp_dir) / "job_finger_data"
            lake = JobLake(data_path)
            run_id = lake.save_search_result(
                search_name="test-search",
                search_term="software engineer",
                location="Portugal",
                sites=["indeed"],
                ranked_jobs=[
                    RankedJob(job_id="test-1", job=job, score=breakdown),
                ],
            )
            update_application(
                data_path,
                job_id="test-1",
                status="applied",
                notes="Applied with backend CV",
            )
            rows = list_ranked_jobs(data_path, limit=10)
            events = list_application_events(data_path, "test-1")
            files = sorted(path.name for path in data_path.iterdir() if path.is_file())
            directories = [path for path in data_path.iterdir() if path.is_dir()]

        self.assertTrue(run_id)
        self.assertEqual(files, ["applications.jsonl", "jobs.jsonl", "scrapes.jsonl"])
        self.assertEqual(directories, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_id"], "test-1")
        self.assertEqual(rows[0]["application_status"], "applied")
        self.assertIn("skills", rows[0])
        self.assertIn("match_explanation", rows[0])
        self.assertIn("cover_letter_draft", rows[0])
        self.assertTrue(rows[0]["applied_at"])
        self.assertEqual(len(events), 1)
        self.assertGreater(rows[0]["score"], 0)

    def test_ranked_jobs_can_filter_by_publish_date(self) -> None:
        profile = UserProfile(target_titles=["software engineer"])
        old_job = {
            "id": "old-1",
            "site": "indeed",
            "job_url": "https://example.com/job/old",
            "title": "Software Engineer",
            "company": "OldCo",
            "location": "Portugal",
            "description": "Python role",
            "date_posted": "2026-06-01",
        }
        new_job = {
            "id": "new-1",
            "site": "indeed",
            "job_url": "https://example.com/job/new",
            "title": "Software Engineer",
            "company": "NewCo",
            "location": "Portugal",
            "description": "Python role",
            "date_posted": "2026-06-26",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            data_path = Path(temp_dir) / "job_finger_data"
            lake = JobLake(data_path)
            lake.save_search_result(
                search_name="test-search",
                search_term="software engineer",
                location="Portugal",
                sites=["indeed"],
                ranked_jobs=[
                    RankedJob("old-1", old_job, score_job(old_job, profile)),
                    RankedJob("new-1", new_job, score_job(new_job, profile)),
                ],
            )

            rows = list_ranked_jobs(
                data_path, limit=10, published_from="2026-06-15"
            )
            newest_first = list_ranked_jobs(data_path, limit=10, sort_by="newest")

        self.assertEqual([row["job_id"] for row in rows], ["new-1"])
        self.assertEqual([row["job_id"] for row in newest_first], ["new-1", "old-1"])

    def test_ranked_jobs_can_filter_by_normalized_fields(self) -> None:
        profile = UserProfile(
            target_titles=["backend engineer"],
            resume_keywords=["python", "fastapi"],
        )
        hybrid_job = {
            "id": "hybrid-1",
            "title": "Senior Backend Engineer",
            "location": "Lisbon",
            "description": "Hybrid Python and FastAPI role.",
            "min_amount": 50000,
            "currency": "EUR",
            "date_posted": "2026-06-26",
        }
        office_job = {
            "id": "office-1",
            "title": "Junior Backend Engineer",
            "location": "Porto",
            "description": "Office-based Python role.",
            "min_amount": 25000,
            "currency": "EUR",
            "date_posted": "2026-06-26",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            data_path = Path(temp_dir) / "job_finger_data"
            lake = JobLake(data_path)
            lake.save_search_result(
                search_name="test-search",
                search_term="backend engineer",
                location="Portugal",
                sites=["indeed"],
                ranked_jobs=[
                    RankedJob("hybrid-1", hybrid_job, score_job(hybrid_job, profile)),
                    RankedJob("office-1", office_job, score_job(office_job, profile)),
                ],
            )

            rows = list_ranked_jobs(
                data_path,
                limit=10,
                work_mode="hybrid",
                seniority="senior",
                min_salary=40000,
            )

        self.assertEqual([row["job_id"] for row in rows], ["hybrid-1"])

    def test_ranked_jobs_can_filter_by_match_quality_fields(self) -> None:
        profile = UserProfile(
            resume_keywords=["python", "fastapi"],
            avoid_keywords=["commission only"],
        )
        good_job = {
            "id": "good-1",
            "title": "Backend Engineer",
            "description": "Python and FastAPI role.",
            "date_posted": "2026-06-26",
        }
        gap_job = {
            "id": "gap-1",
            "title": "Platform Engineer",
            "description": "Kubernetes Terraform and Go role.",
            "date_posted": "2026-06-26",
        }
        bad_job = {
            "id": "bad-1",
            "title": "Backend Engineer",
            "description": "Python role, commission only.",
            "date_posted": "2026-06-26",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            data_path = Path(temp_dir) / "job_finger_data"
            lake = JobLake(data_path)
            lake.save_search_result(
                search_name="test-search",
                search_term="backend engineer",
                location="Portugal",
                sites=["indeed"],
                ranked_jobs=[
                    RankedJob("good-1", good_job, score_job(good_job, profile)),
                    RankedJob("gap-1", gap_job, score_job(gap_job, profile)),
                    RankedJob("bad-1", bad_job, score_job(bad_job, profile)),
                ],
            )

            rows = list_ranked_jobs(
                data_path,
                limit=10,
                min_cv_matches=2,
                max_cv_gaps=0,
                no_negative=True,
            )

        self.assertEqual([row["job_id"] for row in rows], ["good-1"])

    def test_observation_template_can_be_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_path = Path(temp_dir) / "job_finger_data"
            data_path.mkdir()
            self.assertEqual(
                read_observation_template(data_path), DEFAULT_OBSERVATION_TEMPLATE
            )
            (data_path / "observation_template.md").write_text(
                "Outcome:\n\nNext action:\n", encoding="utf-8"
            )

            template = read_observation_template(data_path)

        self.assertIn("Next action", template)


if __name__ == "__main__":
    unittest.main()
