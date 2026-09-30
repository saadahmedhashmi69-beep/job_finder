"""Submission state machine: eligibility, duplicate protection and the
EMAIL / WEB success semantics, exercised through the real submitter/storage
functions (never a copy of their eligibility logic).

Run: python -W default -m unittest tests.test_submission_state -v
SMTP, browser and network are fakes; databases are temporary.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import application_prep  # noqa: E402
import storage  # noqa: E402
import submitter  # noqa: E402
from qualification import (  # noqa: E402
    MANUAL_REQUIRED, READY_TO_SUBMIT, SMTP_ACCEPTED, SUBMISSION_FAILED, SUBMITTED, SUBMITTING,
)
from test_application_routes import APPLY_FORM, FakeSite  # noqa: E402
from test_application_workflow import DRIVER, FIXED_CV  # noqa: E402
from test_hardening import GH, SENT_OK, HardBase  # noqa: E402

UNKNOWN = {"ok": False, "error_class": "timeout", "sent_possible": True, "detail": "timed out during DATA"}
NOT_SENT = {"ok": False, "error_class": "connection", "sent_possible": False, "detail": "no connection"}
# The shape of the real application #16 after its LIVE email was accepted.
ID16_STATE = {"status": SMTP_ACCEPTED, "submission_status": SMTP_ACCEPTED, "submission_mode": "LIVE",
              "sent_at": "2026-09-30T18:09:41.831452", "submitted_at": "", "approved_at": "",
              "submission_evidence": "",
              "status_reason": "Email accepted by SMTP server for x; not a verified submission"}


class StateBase(HardBase):
    def setUp(self):
        super().setUp()
        self.live()

    def job_row(self, app_id):
        conn = storage.get_db(self.db)
        row = dict(conn.execute("SELECT j.* FROM jobs j JOIN applications a ON a.job_url = j.url WHERE a.id = ?",
                                (app_id,)).fetchone())
        conn.close()
        return row

    def ready_ids(self):
        """What the bulk selector (pipeline / `submit` without --app-id) would pick up."""
        return [a["id"] for a in storage.get_applications(status=READY_TO_SUBMIT, db_path=self.db)]

    def every_entry_point(self, app_id, sender, page=None):
        """Run every code path that can submit or re-route an application."""
        factory = (lambda: (page, lambda: None)) if page else None
        results = [self.submit(app_id, sender, page_factory=factory) for _ in range(3)]
        results.append(self.submit(app_id, sender, mode=submitter.DRY_RUN, page_factory=factory))
        submitter.process_ready_applications(self.profile, db_path=self.db, email_sender=sender,
                                             mode=submitter.LIVE, page_factory=factory)
        submitter.process_ready_applications(self.profile, db_path=self.db, email_sender=sender,
                                             mode=submitter.DRY_RUN, page_factory=factory)
        storage.recover_stale_submissions(max_age_minutes=-1, db_path=self.db)
        application_prep.reroute_manual_applications(db_path=self.db, profile=self.profile, matcher=self.m)
        again = application_prep.prepare_application(self.job_row(app_id), self.profile, matcher=self.m,
                                                     db_path=self.db)
        self.assertEqual(again["status"], "DUPLICATE", again)
        return results


class TestEligibility(StateBase):
    def test_a_ready_to_submit_is_eligible(self):
        app_id = self.email_app()
        self.assertIn(app_id, self.ready_ids())
        dry = self.submit(app_id, mock.Mock(), mode=submitter.DRY_RUN)
        self.assertEqual(dry["status"], submitter.DRY_RUN_VALIDATED)
        self.assertEqual(self.app_row(app_id)["status"], READY_TO_SUBMIT)  # DRY_RUN changes nothing
        sender = mock.Mock(return_value=SENT_OK)
        self.assertEqual(self.submit(app_id, sender)["status"], SMTP_ACCEPTED)
        self.assertEqual(sender.call_count, 1)

    def test_b_smtp_accepted_with_sent_at_is_never_sent_again(self):
        app_id = self.email_app()
        self.set_app(app_id, **ID16_STATE)
        before = self.app_row(app_id)
        sender = mock.Mock(return_value=SENT_OK)
        results = self.every_entry_point(app_id, sender)
        sender.assert_not_called()
        for r in results:
            self.assertIn("duplicate", r["reason"].lower())
        self.assertFalse(storage.claim_application(app_id, self.db))
        self.assertNotIn(app_id, self.ready_ids())
        self.assertEqual(submitter.demote_unverified_submissions(self.db), 0)
        self.assertEqual(self.app_row(app_id), before)  # not a single field changed

    def test_c_verified_submission_is_never_submitted_again(self):
        app_id, url = self.web_app()
        page = FakeSite({url: {"fields": APPLY_FORM}}, after_html="<h1>Thank you for applying!</h1>")
        self.assertEqual(self.submit(app_id, page_factory=lambda: (page, lambda: None))["status"], SUBMITTED)
        app = self.app_row(app_id)
        self.assertTrue(app["submitted_at"] and app["submission_evidence"])
        second = FakeSite({url: {"fields": APPLY_FORM}}, after_html="<h1>Thank you for applying!</h1>")
        results = self.every_entry_point(app_id, mock.Mock(), page=second)
        self.assertFalse(second.clicked)
        self.assertEqual(second.visited, [])  # the form is not even opened again
        self.assertTrue(all("duplicate" in r["reason"].lower() for r in results))
        self.assertEqual(self.app_row(app_id)["submitted_at"], app["submitted_at"])

    def test_d_failed_submission_follows_the_retry_policy(self):
        # Outcome unknown (may have been delivered): terminal, never retried by any path.
        terminal = self.email_app(1)
        self.submit(terminal, mock.Mock(return_value=UNKNOWN))
        self.assertEqual(self.app_row(terminal)["status"], SUBMISSION_FAILED)
        sender = mock.Mock(return_value=SENT_OK)
        self.every_entry_point(terminal, sender)
        sender.assert_not_called()
        self.assertEqual(self.app_row(terminal)["status"], SUBMISSION_FAILED)
        # Provably nothing left the machine: back to READY_TO_SUBMIT for an explicit retry.
        retryable = self.email_app(0)
        self.submit(retryable, mock.Mock(return_value=NOT_SENT))
        self.assertEqual((self.app_row(retryable)["status"], self.app_row(retryable)["sent_at"]),
                         (READY_TO_SUBMIT, ""))
        self.assertEqual(self.submit(retryable, sender)["status"], SMTP_ACCEPTED)
        self.assertEqual(sender.call_count, 1)

    def test_e_repeated_execution_sends_once(self):
        app_id, sender = self.email_app(), mock.Mock(return_value=SENT_OK)
        first = self.submit(app_id, sender)
        self.assertEqual(first["status"], SMTP_ACCEPTED)
        sent_at = self.app_row(app_id)["sent_at"]
        self.every_entry_point(app_id, sender)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.app_row(app_id)["sent_at"], sent_at)

    def test_interrupted_claim_is_never_resumed(self):
        app_id, sender = self.email_app(), mock.Mock(return_value=SENT_OK)
        self.assertTrue(storage.claim_application(app_id, self.db))  # a worker died after claiming
        self.assertEqual(self.submit(app_id, sender)["status"], SUBMITTING)
        self.every_entry_point(app_id, sender)
        sender.assert_not_called()
        self.assertEqual(self.app_row(app_id)["status"], SUBMISSION_FAILED)  # closed as outcome unknown


class TestSuccessSemantics(StateBase):
    def test_f_g_smtp_acceptance_is_not_a_verified_submission(self):
        app_id = self.email_app()
        self.submit(app_id, mock.Mock(return_value=SENT_OK))
        app = self.app_row(app_id)
        self.assertEqual((app["status"], app["submission_status"]), (SMTP_ACCEPTED, SMTP_ACCEPTED))
        self.assertTrue(app["sent_at"])
        self.assertEqual((app["submitted_at"], app["submission_evidence"]), ("", ""))
        self.assertIn("not a verified submission", app["status_reason"])
        self.assertEqual(self.job_row(app_id)["applied"], 0)
        self.assertFalse(submitter.is_verified_submission(dict(app, qualification_status="QUALIFIED")))
        # Even a row that claims SUBMITTED for an email is demoted back to SMTP_ACCEPTED.
        self.set_app(app_id, status=SUBMITTED, submission_status=SUBMITTED, submitted_at="2026-09-30T18:10")
        self.assertEqual(submitter.demote_unverified_submissions(self.db), 1)
        app = self.app_row(app_id)
        self.assertEqual((app["status"], app["submitted_at"]), (SMTP_ACCEPTED, ""))
        self.assertTrue(app["sent_at"])  # the send record survives the demotion

    def test_f_web_success_needs_confirmation_evidence(self):
        app_id, url = self.web_app()
        page = FakeSite({url: {"fields": APPLY_FORM}}, after_html="<p>Please wait...</p>")
        res = self.submit(app_id, page_factory=lambda: (page, lambda: None))
        self.assertTrue(page.clicked)
        self.assertEqual(res["status"], SUBMISSION_FAILED)
        app = self.app_row(app_id)
        self.assertEqual((app["status"], app["submitted_at"], app["sent_at"], app["submission_evidence"]),
                         (SUBMISSION_FAILED, "", "", ""))
        # Clicked without confirmation: never clicked again by any path.
        again = FakeSite({url: {"fields": APPLY_FORM}}, after_html="<h1>Thank you for applying!</h1>")
        self.every_entry_point(app_id, mock.Mock(), page=again)
        self.assertFalse(again.clicked)

    def test_page_load_alone_is_never_success(self):
        app_id, url = self.web_app()
        fields = [f for f in APPLY_FORM if f.get("type") != "file"]  # the form has no CV upload
        page = FakeSite({url: {"fields": fields}}, after_html="<h1>Thank you for applying!</h1>")
        res = self.submit(app_id, page_factory=lambda: (page, lambda: None))
        self.assertEqual(res["status"], MANUAL_REQUIRED)
        self.assertFalse(page.clicked)
        self.assertEqual(self.app_row(app_id)["submitted_at"], "")

    def test_click_that_raises_is_outcome_unknown_not_nothing_submitted(self):
        app_id, url = self.web_app()
        page = FakeSite({url: {"fields": APPLY_FORM}})

        class TimedOutButton:
            def click(self):
                page.clicked = True
                raise TimeoutError("click timed out after dispatch")

        page.query_selector = lambda sel: TimedOutButton()
        res = self.submit(app_id, page_factory=lambda: (page, lambda: None))
        self.assertEqual(res["status"], SUBMISSION_FAILED)
        self.assertIn("outcome unknown", res["reason"])
        self.assertTrue(res["report"]["submit_clicked"])
        self.assertEqual(self.app_row(app_id)["status"], SUBMISSION_FAILED)
        retry = FakeSite({url: {"fields": APPLY_FORM}}, after_html="<h1>Thank you for applying!</h1>")
        self.every_entry_point(app_id, mock.Mock(), page=retry)
        self.assertFalse(retry.clicked)


class TestRecipientGuards(StateBase):
    def second_app_same_recipient(self):
        job = dict(DRIVER, url="https://www.linkedin.com/jobs/view/881", company="Reparto Rapido 0",
                   location="Sevilla, AN, ES",
                   description=DRIVER["description"] + " Envia tu CV a empleo0@repartorapido0.es")
        r = self.prepare(job)
        self.assertEqual(r["status"], READY_TO_SUBMIT, r)
        return r["app_id"]

    def test_unknown_outcome_blocks_another_application_to_the_same_recipient(self):
        first = self.email_app(0)
        self.submit(first, mock.Mock(return_value=UNKNOWN))
        self.assertEqual(self.app_row(first)["status"], SUBMISSION_FAILED)
        second, sender = self.second_app_same_recipient(), mock.Mock(return_value=SENT_OK)
        res = self.submit(second, sender)
        sender.assert_not_called()
        self.assertEqual(res["status"], MANUAL_REQUIRED)
        self.assertIn("human decision", res["reason"])

    def test_provably_unsent_failure_does_not_block_the_recipient(self):
        first = self.email_app(0)
        self.submit(first, mock.Mock(return_value=NOT_SENT))
        second, sender = self.second_app_same_recipient(), mock.Mock(return_value=SENT_OK)
        self.assertEqual(self.submit(second, sender)["status"], SMTP_ACCEPTED)

    def client(self):
        import app as app_module
        for p in (mock.patch.object(storage, "DB_PATH", self.db),
                  mock.patch.object(app_module, "get_db", side_effect=lambda *a, **k: storage.get_db(self.db)),
                  mock.patch("submitter.demote_unverified_submissions"),
                  mock.patch.object(app_module, "load_profile", return_value=self.profile)):
            p.start()
            self.addCleanup(p.stop)
        return app_module.create_app().test_client()

    def test_ui_approve_send_never_reports_a_blocked_duplicate_as_sent(self):
        client = self.client()
        sent, ready = self.email_app(0), self.email_app(1)
        self.set_app(sent, **ID16_STATE)
        sender = mock.Mock(return_value=SENT_OK)
        with mock.patch("applier.send_application_email_detailed", sender):
            resp = client.post("/api/application/approve-send", json={"app_id": sent, "confirm": True})
            self.assertEqual(resp.status_code, 400)
            self.assertIs(resp.get_json()["sent"], False)
            self.assertIn("duplicate", resp.get_json()["error"].lower())
            sender.assert_not_called()
            resp = client.post("/api/application/approve-send", json={"app_id": ready, "confirm": True})
            self.assertEqual(resp.status_code, 200)
            self.assertIs(resp.get_json()["sent"], True)
            self.assertEqual(sender.call_count, 1)

    def test_ui_cannot_rewrite_the_recipient_of_a_sent_application(self):
        client = self.client()
        sent, ready = self.email_app(0), self.email_app(1)
        self.set_app(sent, recruiter_email="empleo0@repartorapido0.es", **ID16_STATE)
        before = self.app_row(sent)
        resp = client.post("/api/application/set-recruiter", json={"app_id": sent, "recruiter_email": "x@y.es"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.app_row(sent), before)
        resp = client.post("/api/application/set-recruiter", json={"app_id": ready, "recruiter_email": "x@y.es"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.app_row(ready)["recruiter_email"], "x@y.es")


if __name__ == "__main__":
    unittest.main()
