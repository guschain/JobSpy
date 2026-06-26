from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from job_finger.config import UserProfile
from job_finger.pipeline import RankedJob
from job_finger.scoring import score_job
from job_finger.search_terms import build_keyword_query, filter_rows_by_terms
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
        self.assertTrue(rows[0]["applied_at"])
        self.assertEqual(len(events), 1)
        self.assertGreater(rows[0]["score"], 0)

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
