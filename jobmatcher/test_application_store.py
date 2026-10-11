import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import application_store as store


class ApplicationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tempdir.name, "pipeline.db")
        store.init_schema(self.db)
        self.app = store.ensure_application(
            db_path=self.db,
            job_url="https://example.test/jobs/REQ-1234",
            candidate="candidate_a",
            title="Security Analyst",
            company="Example, Inc.",
            status="applied",
            applied_at=datetime.now(timezone.utc).isoformat(),
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_resolves_by_stable_identifiers(self):
        store.link_application(
            self.app["application_id"], db_path=self.db,
            ats_requisition_id="REQ-1234", email_thread_id="thread-1",
        )
        resolved = store.resolve_application(
            db_path=self.db, ats_requisition_id="REQ-1234",
        )
        self.assertEqual(self.app["application_id"], resolved["application_id"])

    def test_classification_requires_review_decision(self):
        store.upsert_email_message(
            db_path=self.db, message_id="m1", subject="Maybe a job email",
            classification="None", confidence=0.3, review_status="pending",
        )
        store.queue_classification_review(
            db_path=self.db, message_id="m1", proposed_category="None",
            confidence=0.3, reason="ambiguous",
        )
        review = store.list_classification_reviews(db_path=self.db)[0]
        store.decide_classification(review["review_id"], "Interviewing", db_path=self.db)
        decision = store.classification_decision("m1", db_path=self.db)
        self.assertEqual("Interviewing", decision["classification"])
        self.assertEqual("approved", decision["review_status"])

    def test_followup_state_machine(self):
        due_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        followup = store.schedule_followup(self.app["application_id"], due_at, db_path=self.db)
        followup = store.update_followup(
            followup["followup_id"], "draft_ready", db_path=self.db, draft_text="Draft",
        )
        self.assertEqual("draft_ready", followup["state"])
        followup = store.update_followup(followup["followup_id"], "approved", db_path=self.db)
        followup = store.update_followup(followup["followup_id"], "sent", db_path=self.db)
        self.assertIsNotNone(followup["sent_at"])
        with self.assertRaises(ValueError):
            store.update_followup(followup["followup_id"], "approved", db_path=self.db)

    def test_artifact_approval_and_weekly_report(self):
        artifact_id = store.save_artifact(
            self.app["application_id"], "resume_tailoring", {"bullets": []}, db_path=self.db,
        )
        store.approve_artifact(artifact_id, db_path=self.db)
        artifact = store.list_artifacts(self.app["application_id"], db_path=self.db)[0]
        self.assertEqual("approved", artifact["state"])
        report = store.build_weekly_report(db_path=self.db)
        self.assertIn("response_rate", report)
        self.assertIn("by_score_band", report)

    def test_next_action_tracks_followup_approval_without_sending(self):
        due_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        followup = store.schedule_followup(self.app["application_id"], due_at, db_path=self.db)
        actions = store.list_applications_with_next_actions(
            db_path=self.db, statuses=["applied"],
        )
        self.assertEqual("Draft overdue follow-up", actions[0]["next_action"]["label"])
        followup = store.update_followup(
            followup["followup_id"], "draft_ready", draft_text="Please let me know.",
            db_path=self.db,
        )
        actions = store.list_applications_with_next_actions(db_path=self.db, statuses=["applied"])
        self.assertEqual("Review follow-up draft", actions[0]["next_action"]["label"])
        store.update_followup(followup["followup_id"], "approved", db_path=self.db)
        actions = store.list_applications_with_next_actions(db_path=self.db, statuses=["applied"])
        self.assertEqual("Send approved follow-up", actions[0]["next_action"]["label"])
        self.assertIsNone(store.list_followups(db_path=self.db)[0]["sent_at"])

    def test_interview_preparation_precedes_future_followup(self):
        store.update_application_status(self.app["application_id"], "interviewing", db_path=self.db)
        store.schedule_followup(
            self.app["application_id"],
            (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
            db_path=self.db,
        )
        actions = store.list_applications_with_next_actions(
            db_path=self.db, statuses=["interviewing"],
        )
        self.assertEqual("Prepare interview", actions[0]["next_action"]["label"])
        self.assertEqual("interview", actions[0]["next_action"]["target"])
        store.save_artifact(
            self.app["application_id"], "interview_prep", {"summary": "Draft"},
            db_path=self.db,
        )
        actions = store.list_applications_with_next_actions(
            db_path=self.db, statuses=["interviewing"],
        )
        self.assertEqual("Review interview prep draft", actions[0]["next_action"]["label"])

    def test_interview_without_role_details_requests_them(self):
        app = store.ensure_application(
            db_path=self.db, email_message_id="interview-email",
            candidate="candidate_a", status="interviewing",
        )
        action = store.next_application_action(app)
        self.assertEqual("Add role details for interview prep", action["label"])
        self.assertEqual("interview", action["target"])


if __name__ == "__main__":
    unittest.main()
