from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from job_finger.config import UserProfile
from job_finger.scoring import score_job
from job_finger.storage import connect, list_ranked_jobs, save_score, upsert_job


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


class StorageTests(unittest.TestCase):
    def test_upsert_score_and_rank(self) -> None:
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
            db_path = Path(temp_dir) / "jobs.sqlite3"
            connection = connect(db_path)
            try:
                job_id = upsert_job(connection, job)
                save_score(connection, job_id, None, breakdown)
                rows = list_ranked_jobs(connection, limit=10)
            finally:
                connection.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_id"], "test-1")
        self.assertGreater(rows[0]["score"], 0)


if __name__ == "__main__":
    unittest.main()
