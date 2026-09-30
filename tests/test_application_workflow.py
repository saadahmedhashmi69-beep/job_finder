"""Qualification-gated application workflow: gate, fixed CV, method detection,
DRY_RUN/LIVE submission, verification and duplicate protection.

Run: python -m unittest discover -s tests -v
Uses the local (gitignored) profile.yaml for the real hard rules; skips if absent.
No browser, SMTP or network is used: pages and senders are fakes, and the real
jobs.db is never touched (temp DB).
"""

import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import application_prep  # noqa: E402
import cv_customizer  # noqa: E402
import matcher  # noqa: E402
import storage  # noqa: E402
import submitter  # noqa: E402
from qualification import (  # noqa: E402
    MANUAL_REQUIRED, NEEDS_REVIEW, QUALIFIED, READY_TO_SUBMIT, REJECTED,
    SMTP_ACCEPTED, SUBMISSION_FAILED, SUBMITTED, UNVERIFIED_LEGACY, qualify_job,
)

FIXED_CV = (PROJECT_ROOT / "cv" / "Raheel Tahir Resume Updated.pdf").resolve()
PROFILE_PATH = PROJECT_ROOT / "profile.yaml"

GH = "https://job-boards.greenhouse.io/acme/jobs/"
WOMENSWEAR = {"url": GH + "1", "title": "Womenswear Fashion Designer", "company": "Moda BCN",
              "location": "Barcelona, Spain", "board": "linkedin", "match_score": 0.81,
              "description": "Design womenswear collections: dresses and knitwear for women. "
                             "Fashion sketching, fabric selection, sampling."}
DRIVER = {"url": GH + "2", "title": "Conductor/a de furgoneta", "company": "Reparto Rapido",
          "location": "Madrid, MD, ES", "board": "indeed", "match_score": 0.75,
          "description": "Conduccion de furgoneta de reparto por Madrid con vehiculo de empresa."}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _forbid(*a, **k):
    raise AssertionError("cv_customizer must not be called in the application flow")


class FakeButton:
    def __init__(self, page):
        self.page = page

    def click(self):
        self.page.clicked = True
        self.page._html, self.page.url = self.page.after_html, self.page.after_url


class FakePage:
    """Minimal Playwright-like page."""

    def __init__(self, fields, html="<form></form>", url="https://job-boards.greenhouse.io/acme/jobs/1",
                 after_html="<p>Error</p>", after_url=None):
        self.fields, self._html, self.url = fields, html, url
        self.after_html, self.after_url = after_html, after_url or url
        self.filled, self.uploads, self.clicked = {}, [], False

    def goto(self, url):
        pass

    def content(self):
        return self._html

    def evaluate(self, js):
        return [dict(f, idx=i) for i, f in enumerate(self.fields)]

    def fill(self, selector, value):
        self.filled[selector] = value

    def set_input_files(self, selector, path):
        self.uploads.append(path)

    def query_selector(self, sel):
        return FakeButton(self)

    def wait_for_load_state(self, *a, **k):
        pass


def _field(name, type_="text", tag="input", required=False, label=""):
    return {"tag": tag, "type": type_, "name": name, "id": name, "placeholder": "",
            "aria": "", "label": label or name, "required": required, "value": "", "hidden": False}


SIMPLE_FORM = [_field("first_name", required=True), _field("last_name", required=True),
               _field("email", "email", required=True), _field("phone", "tel"),
               _field("resume", "file", required=True), _field("cover_letter", "", "textarea")]


class WorkflowBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not PROFILE_PATH.exists():
            raise unittest.SkipTest("profile.yaml not present (local-only file)")
        if not FIXED_CV.is_file():
            raise unittest.SkipTest(f"Fixed CV missing: {FIXED_CV}")
        cls.cv_hash = _sha(FIXED_CV)
        with open(PROFILE_PATH, encoding="utf-8") as f:
            cls.base_profile = yaml.safe_load(f)

    @classmethod
    def tearDownClass(cls):
        # 15. The original fixed CV must never change.
        assert _sha(FIXED_CV) == cls.cv_hash, "Fixed CV PDF was modified!"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = self.tmp / "jobs.db"
        self.profile = copy.deepcopy(self.base_profile)
        self.profile["pipeline"].update({"fixed_cv_path": str(FIXED_CV), "cv_dir": str(self.tmp / "cv")})
        self.profile["pipeline"].pop("submission_mode", None)
        self.profile["pipeline"].pop("allow_live_submission", None)
        for p in (mock.patch.object(matcher.JobMatcher, "_semantic_score", return_value=0.5),
                  mock.patch.object(matcher, "load_life_story", return_value=""),
                  # 9. Normal flow never touches CV customisation.
                  mock.patch.object(cv_customizer, "customize_cv_for_job", side_effect=_forbid),
                  mock.patch.object(cv_customizer, "compile_latex", side_effect=_forbid),
                  mock.patch.object(cv_customizer, "analyze_job", side_effect=_forbid),
                  mock.patch("smtplib.SMTP_SSL", side_effect=AssertionError("real SMTP used"))):
            p.start()
            self.addCleanup(p.stop)
        self.m = matcher.JobMatcher(self.profile)

    def insert(self, job):
        conn = storage.get_db(self.db)
        conn.execute("INSERT INTO jobs (url, title, company, location, board, description, match_score) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (job["url"], job["title"], job["company"], job["location"], job["board"],
                      job["description"], job["match_score"]))
        conn.commit()
        conn.close()
        return job

    def prepare(self, job):
        self.insert(job)
        return application_prep.prepare_application(job, self.profile, matcher=self.m, db_path=self.db)

    def app_row(self, app_id):
        conn = storage.get_db(self.db)
        row = dict(conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone())
        conn.close()
        return row

    def count_apps(self):
        conn = storage.get_db(self.db)
        n = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
        conn.close()
        return n


class TestQualificationGate(WorkflowBase):
    def test_high_score_unqualified_jobs_get_no_application(self):
        cases = [
            ({"title": "Graphic Designer", "description": "Branding and packaging for women's fashion."}, REJECTED),
            ({"title": "Chofer repartidor", "description": "Reparto de comida en moto por la ciudad."}, REJECTED),
            ({"title": "Womenswear Designer", "location": "Lahore, Pakistan",
              "description": "Design womenswear collections."}, REJECTED),
            ({"title": "Fashion Designer", "description": "Design collections for our brand."}, NEEDS_REVIEW),
        ]
        for i, (over, expected) in enumerate(cases):
            job = dict(WOMENSWEAR, url=f"https://example.org/j/{i}", company=f"Co{i}", match_score=0.99, **over)
            r = self.prepare(job)
            self.assertEqual(r["status"], expected, job["title"])
            self.assertIsNone(r["app_id"])
        self.assertEqual(self.count_apps(), 0)

    def test_qualified_womenswear_job_prepares_application(self):
        r = self.prepare(WOMENSWEAR)
        self.assertEqual(r["status"], READY_TO_SUBMIT)
        app = self.app_row(r["app_id"])
        self.assertEqual(Path(app["cv_pdf_path"]), FIXED_CV)  # 7
        self.assertEqual(app["cv_sha256"], self.cv_hash)
        self.assertEqual(app["application_method"], "WEB")
        letter = app["email_body"]
        self.assertIn("Womenswear Fashion Designer", letter)
        self.assertIn("Moda BCN", letter)
        self.assertIn(self.profile["name"], letter)
        answers = json.loads(app["form_answers_json"])
        self.assertEqual(answers["email"], self.profile["email"])
        for unknown in ("salary", "visa", "sponsorship", "years", "start_date"):
            self.assertNotIn(unknown, " ".join(answers).lower())

    def test_qualified_driver_job_prepares_application(self):
        self.assertEqual(qualify_job(DRIVER, self.profile, self.m).status, QUALIFIED)
        r = self.prepare(DRIVER)
        self.assertEqual(r["status"], READY_TO_SUBMIT)
        self.assertEqual(Path(self.app_row(r["app_id"])["cv_pdf_path"]), FIXED_CV)

    def test_professional_licence_driver_jobs_need_review(self):
        for i, (title, desc) in enumerate([
            ("Conductor/a de taxi", "Conduccion de taxi en Barcelona. Carnet B y permiso municipal BTP."),
            ("Conductor/a camion C+E", "Conduccion de camion, carnet C+E y CAP vigente, tarjeta de tacografo."),
            ("Conductor/a transport adaptat", "Conduccion de vehiculo de transporte adaptado, carnet B."),
        ]):
            job = dict(DRIVER, url=f"https://example.org/d/{i}", company=f"Tr{i}", title=title, description=desc)
            q = qualify_job(job, self.profile, self.m)
            self.assertIn(q.status, (NEEDS_REVIEW, REJECTED), title)
            self.assertIsNone(self.prepare(job)["app_id"], title)


class TestApplicationMethod(WorkflowBase):
    def test_no_recruiter_email_does_not_block_web(self):
        r = self.prepare(WOMENSWEAR)
        app = self.app_row(r["app_id"])
        self.assertEqual(app["recruiter_email"], "")
        self.assertEqual((app["status"], app["application_method"]), (READY_TO_SUBMIT, "WEB"))

    def test_email_route_needs_legitimate_email(self):
        job = dict(DRIVER, url="https://www.linkedin.com/jobs/view/1",
                   description=DRIVER["description"] + " Envia tu CV a empleo@repartorapido.es")
        r = self.prepare(job)
        self.assertEqual(self.app_row(r["app_id"])["application_method"], "EMAIL")
        # Email removed -> no email route; sender never called.
        storage.update_application(r["app_id"], db_path=self.db, recruiter_email="")
        conn = storage.get_db(self.db)
        conn.execute("UPDATE jobs SET description = ? WHERE url = ?", (DRIVER["description"], job["url"]))
        conn.commit()
        conn.close()
        sender = mock.Mock(return_value=True)
        self.profile["pipeline"].update({"submission_mode": "LIVE", "allow_live_submission": True})
        res = submitter.submit_application(r["app_id"], self.profile, email_sender=sender, db_path=self.db)
        self.assertEqual(res["status"], MANUAL_REQUIRED)
        sender.assert_not_called()

    def test_login_walled_board_without_email_is_manual(self):
        job = dict(DRIVER, url="https://es.indeed.com/viewjob?jk=abc")
        self.assertEqual(application_prep.detect_application_method(job)["method"], MANUAL_REQUIRED)
        self.assertEqual(self.prepare(job)["status"], MANUAL_REQUIRED)


class TestSubmission(WorkflowBase):
    def ready_app(self):
        return self.prepare(WOMENSWEAR)["app_id"]

    def submit(self, app_id, page, mode=None):
        return submitter.submit_application(app_id, self.profile, mode=mode,
                                            page_factory=lambda: (page, lambda: None), db_path=self.db)

    def test_default_mode_is_dry_run(self):
        self.assertEqual(submitter.get_submission_mode({}), submitter.DRY_RUN)
        self.assertEqual(submitter.get_submission_mode(self.profile), submitter.DRY_RUN)
        self.assertEqual(submitter.get_submission_mode({"pipeline": {"submission_mode": "LIVE"}}),
                         submitter.DRY_RUN)

    def test_dry_run_fills_uploads_fixed_cv_and_never_submits(self):
        app_id, page = self.ready_app(), FakePage(SIMPLE_FORM)
        res = self.submit(app_id, page, mode=submitter.LIVE)  # LIVE not enabled -> DRY_RUN
        self.assertEqual(res["status"], submitter.DRY_RUN_VALIDATED)
        self.assertFalse(page.clicked)
        self.assertEqual([Path(p) for p in page.uploads], [FIXED_CV])
        self.assertIn(self.profile["email"], page.filled.values())
        self.assertEqual(self.app_row(app_id)["status"], READY_TO_SUBMIT)

    def enable_live(self):
        self.profile["pipeline"].update({"submission_mode": "LIVE", "allow_live_submission": True})

    def test_submitted_requires_observable_confirmation(self):
        self.enable_live()
        app_id = self.ready_app()
        res = self.submit(app_id, FakePage(SIMPLE_FORM, after_html="<form>still here</form>"))
        self.assertEqual(res["status"], SUBMISSION_FAILED)
        self.assertEqual(self.app_row(app_id)["status"], SUBMISSION_FAILED)

        storage.update_application(app_id, db_path=self.db, status=READY_TO_SUBMIT)
        res = self.submit(app_id, FakePage(SIMPLE_FORM, after_html="<h1>Thank you for applying!</h1>"))
        self.assertEqual(res["status"], SUBMITTED)
        self.assertTrue(self.app_row(app_id)["submitted_at"])
        # A confirmed WEB submission satisfies the contract and is never demoted.
        self.assertEqual(submitter.demote_unverified_submissions(self.db), 0)
        self.assertEqual(self.app_row(app_id)["status"], SUBMITTED)

    def test_captcha_login_and_unknown_required_fields_are_manual(self):
        self.enable_live()
        pages = [
            FakePage(SIMPLE_FORM, html='<div class="g-recaptcha"></div>'),
            FakePage(SIMPLE_FORM, url="https://acme.com/login?next=apply"),
            FakePage(SIMPLE_FORM + [_field("salary_expectation", required=True)]),
            FakePage([_field("email", "email")]),  # no CV upload field
        ]
        for page in pages:
            app_id = self.ready_app() if not self.count_apps() else self.reset(app_id)
            res = self.submit(app_id, page)
            self.assertEqual(res["status"], MANUAL_REQUIRED, res["reason"])
            self.assertFalse(page.clicked)

    def reset(self, app_id):
        storage.update_application(app_id, db_path=self.db, status=READY_TO_SUBMIT)
        return app_id

    def test_arbitrary_cv_cannot_replace_fixed_cv(self):
        app_id = self.ready_app()
        other = self.tmp / "generated-cv.pdf"
        other.write_bytes(b"%PDF-1.4 generated")
        storage.update_application(app_id, db_path=self.db, cv_pdf_path=str(other))
        page = FakePage(SIMPLE_FORM)
        res = self.submit(app_id, page)
        self.assertEqual(res["status"], MANUAL_REQUIRED)
        self.assertEqual(page.uploads, [])

    def test_duplicates_are_prevented(self):
        first = self.prepare(WOMENSWEAR)
        again = application_prep.prepare_application(WOMENSWEAR, self.profile, matcher=self.m, db_path=self.db)
        self.assertEqual(again["status"], "DUPLICATE")
        same_job_other_url = self.prepare(dict(WOMENSWEAR, url="https://example.org/mirror"))
        self.assertEqual(same_job_other_url["status"], "DUPLICATE")
        self.assertEqual(self.count_apps(), 1)

        self.enable_live()
        self.submit(first["app_id"], FakePage(SIMPLE_FORM, after_html="<p>Application received</p>"))
        page = FakePage(SIMPLE_FORM, after_html="<p>Application received</p>")
        res = self.submit(first["app_id"], page)
        self.assertIn("duplicate", res["reason"].lower())
        self.assertFalse(page.clicked)


LEGIT_EMAIL_JOB = dict(DRIVER, url="https://www.linkedin.com/jobs/view/77",
                       description=DRIVER["description"] + " Envia tu CV a empleo@repartorapido.es")


class TestApplicationRecipients(WorkflowBase):
    def test_placeholder_emails_are_rejected(self):
        for bad in ("hr@example.com", "jobs@example.org", "rrhh@example.net", "hr@localhost",
                    "hr@mail.example.com", "jobs@acme.test", "test@repartorapido.es",
                    "placeholder@acme.es", "your.email@acme.es", "john.doe@acme.es"):
            self.assertTrue(application_prep.is_placeholder_email(bad), bad)
            job = dict(DRIVER, url="https://www.linkedin.com/jobs/view/9",
                       description=f"Conduccion de furgoneta. Envia tu CV a {bad}")
            self.assertEqual(application_prep.find_application_email(job, bad), "", bad)
            self.assertEqual(application_prep.detect_application_method(job)["method"], MANUAL_REQUIRED, bad)
        self.assertFalse(application_prep.is_placeholder_email("empleo@repartorapido.es"))

    def test_email_must_come_from_posting(self):
        # A recipient set on the application but absent from the posting is never used.
        self.assertEqual(application_prep.find_application_email(DRIVER, "hr@example.com"), "")
        self.assertEqual(application_prep.find_application_email(DRIVER, "rrhh@otra-empresa.es"), "")
        self.assertEqual(application_prep.find_application_email(LEGIT_EMAIL_JOB, "rrhh@otra-empresa.es"),
                         "empleo@repartorapido.es")

    def test_placeholder_recipient_on_application_is_manual_required(self):
        r = self.prepare(dict(DRIVER, url="https://www.linkedin.com/jobs/view/5"))
        self.assertEqual(r["status"], MANUAL_REQUIRED)
        storage.update_application(r["app_id"], db_path=self.db, status=READY_TO_SUBMIT,
                                   application_method="EMAIL", recruiter_email="hr@example.com")
        self.profile["pipeline"].update({"submission_mode": "LIVE", "allow_live_submission": True})
        sender = mock.Mock(return_value=True)
        res = submitter.submit_application(r["app_id"], self.profile, email_sender=sender, db_path=self.db)
        self.assertEqual(res["status"], MANUAL_REQUIRED)
        sender.assert_not_called()

    def test_legitimate_posting_email_reaches_ready_in_dry_run_without_sending(self):
        r = self.prepare(LEGIT_EMAIL_JOB)
        app = self.app_row(r["app_id"])
        self.assertEqual((app["status"], app["application_method"], app["recruiter_email"]),
                         (READY_TO_SUBMIT, "EMAIL", "empleo@repartorapido.es"))
        self.assertEqual(Path(app["cv_pdf_path"]).resolve(), FIXED_CV)
        sender = mock.Mock(return_value=True)
        with mock.patch("applier.send_application_email", side_effect=AssertionError("email sent")):
            res = submitter.submit_application(r["app_id"], self.profile, email_sender=sender, db_path=self.db)
        self.assertEqual(res["status"], submitter.DRY_RUN_VALIDATED)
        sender.assert_not_called()
        app = self.app_row(r["app_id"])
        self.assertEqual(app["status"], READY_TO_SUBMIT)
        self.assertEqual((app["sent_at"], app["submitted_at"]), ("", ""))

    def test_smtp_acceptance_is_not_a_verified_submission(self):
        r = self.prepare(LEGIT_EMAIL_JOB)
        self.profile["pipeline"].update({"submission_mode": "LIVE", "allow_live_submission": True})
        sender = mock.Mock(return_value=True)  # stands in for SMTP; nothing is sent
        res = submitter.submit_application(r["app_id"], self.profile, email_sender=sender, db_path=self.db)
        self.assertEqual(res["status"], SMTP_ACCEPTED)
        app = self.app_row(r["app_id"])
        self.assertEqual((app["status"], app["submission_status"], app["submitted_at"]),
                         (SMTP_ACCEPTED, SMTP_ACCEPTED, ""))
        self.assertTrue(app["sent_at"])
        self.assertFalse(submitter.is_verified_submission(dict(app, qualification_status=QUALIFIED)))
        conn = storage.get_db(self.db)
        self.assertEqual(conn.execute("SELECT applied FROM jobs WHERE url = ?",
                                      (LEGIT_EMAIL_JOB["url"],)).fetchone()[0], 0)
        conn.close()


class TestLegacySubmissions(WorkflowBase):
    MINGA = {"url": "https://www.linkedin.com/jobs/view/minga", "title": "Senior Womenswear Fashion Designer",
             "company": "Minga London", "location": "London", "board": "linkedin", "match_score": 0.9,
             "description": "Womenswear designer."}

    def insert_minga_legacy(self):
        self.insert(self.MINGA)
        conn = storage.get_db(self.db)
        conn.execute("UPDATE jobs SET applied = 1 WHERE url = ?", (self.MINGA["url"],))
        conn.commit()
        conn.close()
        app_id = storage.create_application(self.MINGA["url"], "minga", db_path=self.db)
        storage.update_application(
            app_id, db_path=self.db, status="review_sent", submission_status=SUBMITTED, submission_mode="LIVE",
            recruiter_email="hr@example.com", status_reason="Email accepted by SMTP for hr@example.com",
            submitted_at="2026-09-30T07:58:27", sent_at="2026-09-30T07:58:27")
        return app_id

    def test_legacy_submitted_record_is_not_a_genuine_submission(self):
        app_id = self.insert_minga_legacy()
        self.assertEqual(submitter.demote_unverified_submissions(self.db), 1)
        app = self.app_row(app_id)
        self.assertEqual((app["status"], app["submission_status"], app["submitted_at"]),
                         (UNVERIFIED_LEGACY, UNVERIFIED_LEGACY, ""))
        self.assertEqual(app["submission_evidence"], "")  # nothing fabricated
        self.assertFalse(submitter.is_verified_submission(app))
        conn = storage.get_db(self.db)
        self.assertEqual(conn.execute("SELECT applied FROM jobs WHERE url = ?",
                                      (self.MINGA["url"],)).fetchone()[0], 0)
        conn.close()
        self.assertEqual(submitter.demote_unverified_submissions(self.db), 0)  # idempotent

    def test_applications_page_does_not_show_legacy_as_submitted(self):
        import app as app_module
        self.insert_minga_legacy()
        real_demote = submitter.demote_unverified_submissions
        with mock.patch.object(submitter, "demote_unverified_submissions", lambda: real_demote(self.db)), \
                mock.patch.object(app_module, "get_applications",
                                  lambda limit=50: storage.get_applications(limit=limit, db_path=self.db)), \
                mock.patch.object(storage, "get_jobs_by_qualification", return_value=[]):
            html = app_module.create_app().test_client().get("/applications").get_data(as_text=True)
        self.assertNotIn("Minga London", html)
        self.assertNotIn("hr@example.com", html)
        self.assertNotIn(">SUBMITTED", html)
        self.assertIn("1 legacy record(s)", html)


if __name__ == "__main__":
    unittest.main()
