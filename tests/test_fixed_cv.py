"""Fixed-CV tests: every application must attach the one configured CV PDF.

Run: python -m unittest discover -s tests -v
No email is sent (SMTP senders are mocked) and the real jobs.db is not touched.
"""

import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import application_prep  # noqa: E402
import applier  # noqa: E402
import cv_customizer  # noqa: E402
import fixed_cv  # noqa: E402
import pipeline  # noqa: E402
import storage  # noqa: E402
from qualification import QualificationResult, QUALIFIED  # noqa: E402


def _qualified(job, profile, matcher=None):
    # The hard gate itself is covered in test_application_workflow.py.
    cat = "fashion" if "design" in job["title"].lower() else "driver"
    return QualificationResult(QUALIFIED, cat)

FIXED_CV = (PROJECT_ROOT / "cv" / "Raheel Tahir Resume Updated.pdf").resolve()

FASHION_JOB = {
    "url": "https://example.com/jobs/fashion-designer",
    "title": "Fashion Designer",
    "company": "Atelier Co",
    "location": "Lahore, Pakistan",
    "description": "Design womenswear collections, sketching, pattern making. Send your CV to careers@acme-jobs.es",
    "match_score": 0.9,
}
DRIVER_JOB = {
    "url": "https://example.com/jobs/delivery-driver",
    "title": "Delivery Driver",
    "company": "FastMove Logistics",
    "location": "Karachi, Pakistan",
    "description": "Drive company van, valid driving licence required. Send your CV to careers@acme-jobs.es",
    "match_score": 0.8,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _forbid_customize(*args, **kwargs):
    raise AssertionError("customize_cv_for_job must not be called in the fixed-CV flow")


class FixedCVTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not FIXED_CV.is_file():
            raise unittest.SkipTest(f"Fixed CV missing: {FIXED_CV}")
        cls._cv_hash = _sha256(FIXED_CV)

    @classmethod
    def tearDownClass(cls):
        # The source CV must never be modified.
        assert _sha256(FIXED_CV) == cls._cv_hash, "Fixed CV PDF was modified!"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.profile = {
            "name": "Raheel Tahir",
            "email": "raheeltahir01@gmail.com",
            "pipeline": {
                "fixed_cv_path": str(FIXED_CV),
                "cv_dir": str(self.tmp / "cv"),
                "auto_apply_threshold": 0.5,
                "max_applications_per_run": 10,
                "email_recipient": "",
                # Enabled only so mocked approve-send tests can reach the sender.
                "submission_mode": "LIVE",
                "allow_live_submission": True,
            }
        }
        # Never let any code path reach real SMTP.
        smtp = mock.patch("smtplib.SMTP_SSL", side_effect=AssertionError("real SMTP used"))
        smtp.start()
        self.addCleanup(smtp.stop)


class TestResolveFixedCV(FixedCVTestBase):
    def test_resolves_configured_path(self):
        self.assertEqual(fixed_cv.resolve_fixed_cv_path(self.profile), FIXED_CV)

    def test_default_path_is_raheel_cv(self):
        self.assertEqual(fixed_cv.resolve_fixed_cv_path({}), FIXED_CV)

    def test_missing_file_fails_clearly(self):
        profile = {"pipeline": {"fixed_cv_path": str(self.tmp / "nope.pdf")}}
        with self.assertRaises(fixed_cv.FixedCVMissingError) as ctx:
            fixed_cv.resolve_fixed_cv_path(profile)
        self.assertIn("nope.pdf", str(ctx.exception))


class TestPipelineUsesFixedCV(FixedCVTestBase):
    def _run_pipeline(self, jobs):
        created = {}
        updates = []

        def fake_create_application(job_url, slug, db_path=None):
            created[job_url] = len(created) + 1
            return created[job_url]

        def fake_update_application(app_id, db_path=None, **kwargs):
            updates.append((app_id, kwargs))

        patches = [
            mock.patch.object(pipeline, "_scrape_all", return_value=[]),
            mock.patch.object(pipeline, "JobMatcher"),
            mock.patch.object(pipeline, "save_jobs", return_value=0),
            mock.patch.object(pipeline, "get_top_jobs", return_value=jobs),
            mock.patch.object(pipeline, "get_pipeline_candidates", return_value=jobs),
            # Never touch the real jobs.db / project lock from the pipeline's housekeeping.
            mock.patch.object(pipeline, "recover_stale_runs", return_value=0),
            mock.patch.object(pipeline, "recover_stale_submissions", return_value=0),
            mock.patch.object(pipeline, "LOCK_PATH", self.tmp / ".pipeline.lock"),
            mock.patch.object(pipeline, "find_existing_application", return_value=None),
            mock.patch.object(storage, "find_existing_application", return_value=None),
            mock.patch.object(storage, "set_job_qualification"),
            mock.patch.object(application_prep, "qualify_job", side_effect=_qualified),
            mock.patch.object(storage, "create_application", side_effect=fake_create_application),
            mock.patch.object(storage, "update_application", side_effect=fake_update_application),
            mock.patch.object(pipeline, "start_pipeline_run", return_value=1),
            mock.patch.object(pipeline, "finish_pipeline_run"),
            mock.patch.object(pipeline, "should_send_digest", return_value=False),
            # No real jobs.db reads/writes and no network from route re-discovery.
            mock.patch.object(application_prep, "reroute_manual_applications", return_value=0),
            mock.patch.object(application_prep, "_http_fetch", return_value=None),
            mock.patch.object(application_prep, "_web_search", return_value=[]),
            mock.patch("submitter.process_ready_applications", return_value={}),
            mock.patch.object(cv_customizer, "analyze_job", return_value={}),
            mock.patch.object(cv_customizer, "customize_cv_for_job", side_effect=_forbid_customize),
            mock.patch.object(cv_customizer, "compile_latex", side_effect=_forbid_customize),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        stats = pipeline.run_pipeline(profile=self.profile)
        return stats, created, updates

    def _cv_paths_for(self, updates, app_id):
        return [kw["cv_pdf_path"] for aid, kw in updates if aid == app_id and "cv_pdf_path" in kw]

    def test_fashion_application_uses_fixed_cv(self):
        stats, created, updates = self._run_pipeline([FASHION_JOB])
        self.assertEqual(stats["applications_created"], 1)
        self.assertEqual(self._cv_paths_for(updates, created[FASHION_JOB["url"]]), [str(FIXED_CV)])

    def test_driver_application_uses_fixed_cv(self):
        stats, created, updates = self._run_pipeline([DRIVER_JOB])
        self.assertEqual(stats["applications_created"], 1)
        self.assertEqual(self._cv_paths_for(updates, created[DRIVER_JOB["url"]]), [str(FIXED_CV)])

    def test_missing_fixed_cv_creates_no_applications(self):
        self.profile["pipeline"]["fixed_cv_path"] = str(self.tmp / "missing.pdf")
        stats, created, updates = self._run_pipeline([FASHION_JOB, DRIVER_JOB])
        self.assertEqual(stats["applications_created"], 0)
        self.assertEqual(created, {})


class TestApplierPackage(FixedCVTestBase):
    def test_other_pdf_in_app_dir_cannot_replace_fixed_cv(self):
        app_dir = self.tmp / "applications" / "atelier-co-fashion-designer"
        app_dir.mkdir(parents=True)
        (app_dir / "cv-llt.pdf").write_bytes(b"%PDF-1.4 generated")
        (app_dir / "aaa-other.pdf").write_bytes(b"%PDF-1.4 other")

        package = applier.prepare_application_package(app_dir, FIXED_CV)
        self.assertEqual(Path(package["cv"]), FIXED_CV)

    def test_missing_fixed_cv_raises(self):
        with self.assertRaises(FileNotFoundError):
            applier.prepare_application_package(self.tmp, self.tmp / "missing.pdf")


class TestAppFlows(FixedCVTestBase):
    def setUp(self):
        super().setUp()
        import app as app_module
        self.app_module = app_module
        self.db_path = self.tmp / "jobs.db"
        self.get_db = lambda *a, **k: storage.get_db(self.db_path)

        conn = self.get_db()
        for job in (FASHION_JOB, DRIVER_JOB):
            conn.execute(
                "INSERT INTO jobs (url, title, company, location, description, match_score) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job["url"], job["title"], job["company"], job["location"],
                 job["description"], job["match_score"]),
            )
        conn.commit()
        conn.close()

        patches = [
            mock.patch.object(app_module, "get_db", side_effect=self.get_db),
            # Writes must hit the temp DB too, never the real jobs.db.
            mock.patch.object(app_module, "update_application",
                              side_effect=lambda app_id, **kw: storage.update_application(
                                  app_id, db_path=self.db_path, **kw)),
            mock.patch("submitter.demote_unverified_submissions"),
            mock.patch.object(app_module, "load_profile", return_value=self.profile),
            # The explicit submit path reads/writes through storage.DB_PATH.
            mock.patch.object(storage, "DB_PATH", self.db_path),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = app_module.create_app().test_client()

    def _insert_application(self, job, cv_pdf_path, cover_letter_pdf_path="", cv_sha256=None):
        conn = self.get_db()
        slug = cv_customizer._slugify(f"{job['company']}-{job['title']}")
        cur = conn.execute(
            "INSERT INTO applications (job_url, slug, status, cv_pdf_path, cover_letter_pdf_path, "
            "recruiter_email, application_method, cv_sha256) "
            "VALUES (?, ?, 'READY_TO_SUBMIT', ?, ?, 'careers@acme-jobs.es', 'EMAIL', ?)",
            (job["url"], slug, cv_pdf_path, cover_letter_pdf_path,
             self._cv_hash if cv_sha256 is None else cv_sha256),
        )
        conn.commit()
        app_id = cur.lastrowid
        conn.close()
        return app_id

    def _generate(self, job):
        calls = []

        class SyncThread:
            def __init__(self, target):
                self.target = target

            def start(self):
                self.target()

        patches = [
            mock.patch.object(self.app_module.threading, "Thread", SyncThread),
            mock.patch.object(storage, "create_application", return_value=42),
            mock.patch.object(storage, "find_existing_application", return_value=None),
            mock.patch.object(storage, "set_job_qualification"),
            mock.patch.object(application_prep, "qualify_job", side_effect=_qualified),
            mock.patch.object(storage, "update_application",
                              side_effect=lambda app_id, **kw: calls.append(kw)),
            mock.patch.object(cv_customizer, "analyze_job", return_value={}),
            mock.patch.object(cv_customizer, "customize_cv_for_job", side_effect=_forbid_customize),
            mock.patch("cover_letter.create_cover_letter", return_value=None),
            mock.patch("form_answers.generate_form_answers", return_value={}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        resp = self.client.post("/api/generate-application", json={"url": job["url"]})
        self.assertEqual(resp.get_json()["status"], "ok")
        return [kw["cv_pdf_path"] for kw in calls if "cv_pdf_path" in kw]

    def test_generate_application_fashion_uses_fixed_cv(self):
        self.assertEqual(self._generate(FASHION_JOB), [str(FIXED_CV)])

    def test_generate_application_driver_uses_fixed_cv(self):
        self.assertEqual(self._generate(DRIVER_JOB), [str(FIXED_CV)])

    def _approve_send(self, job, expect_status=200, **insert_kwargs):
        """LIVE approve-send = the one explicit submit path (submitter.submit_application)."""
        import submitter
        app_id = self._insert_application(job, **insert_kwargs)
        with mock.patch.object(applier, "send_application_email_detailed",
                               return_value={"ok": True, "error_class": "", "sent_possible": True,
                                             "detail": ""}) as send, \
                mock.patch.object(submitter, "qualify_job", side_effect=_qualified):
            resp = self.client.post("/api/application/approve-send", json={"app_id": app_id, "confirm": True})
        self.assertEqual(resp.status_code, expect_status, resp.get_json())
        return app_id, send, resp

    def test_approve_send_receives_fixed_cv_for_fashion(self):
        app_id, send, _ = self._approve_send(FASHION_JOB, cv_pdf_path=str(FIXED_CV))
        send.assert_called_once()
        kwargs = send.call_args.kwargs
        self.assertEqual(Path(kwargs["cv_path"]), FIXED_CV)
        self.assertEqual(kwargs["to_email"], "careers@acme-jobs.es")
        self.assertIn("Raheel Tahir", kwargs["subject"])
        self.assertIn("Raheel Tahir", kwargs["body"])
        self.assertNotIn("Ibrahim", kwargs["subject"] + kwargs["body"])
        conn = self.get_db()
        row = conn.execute("SELECT status, sent_at FROM applications WHERE id = ?", (app_id,)).fetchone()
        conn.close()
        self.assertEqual(row["status"], "SMTP_ACCEPTED")
        self.assertTrue(row["sent_at"])

    def test_approve_send_receives_fixed_cv_for_driver(self):
        _, send, _ = self._approve_send(DRIVER_JOB, cv_pdf_path=str(FIXED_CV))
        self.assertEqual(Path(send.call_args.kwargs["cv_path"]), FIXED_CV)

    def test_approve_send_needs_explicit_confirmation(self):
        app_id = self._insert_application(FASHION_JOB, cv_pdf_path=str(FIXED_CV))
        with mock.patch.object(applier, "send_application_email_detailed") as send:
            resp = self.client.post("/api/application/approve-send", json={"app_id": app_id})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("confirm", resp.get_json()["error"])
        send.assert_not_called()

    def test_approve_send_never_sends_a_stale_or_other_cv(self):
        # Legacy application: stored cv_pdf_path points at a generated CV and the
        # application directory contains other PDFs. Nothing is sent at all.
        app_dir = self.tmp / "cv" / "applications" / "fastmove-logistics-delivery-driver"
        app_dir.mkdir(parents=True)
        stale = app_dir / "cv-llt.pdf"
        stale.write_bytes(b"%PDF-1.4 generated")
        (app_dir / "aaa-other.pdf").write_bytes(b"%PDF-1.4 other")
        _, send, resp = self._approve_send(DRIVER_JOB, expect_status=400, cv_pdf_path=str(stale))
        send.assert_not_called()
        self.assertIn("Fixed CV path/checksum mismatch", resp.get_json()["error"])
        # Same for a record whose checksum was never stored.
        conn = self.get_db()
        conn.execute("DELETE FROM applications")
        conn.commit()
        conn.close()
        _, send, _ = self._approve_send(DRIVER_JOB, expect_status=400, cv_pdf_path=str(FIXED_CV), cv_sha256="")
        send.assert_not_called()

    def test_approve_send_fails_clearly_when_fixed_cv_missing(self):
        self.profile["pipeline"]["fixed_cv_path"] = str(self.tmp / "missing.pdf")
        app_id = self._insert_application(FASHION_JOB, cv_pdf_path="")
        with mock.patch.object(applier, "send_application_email_detailed") as send:
            resp = self.client.post(
                "/api/application/approve-send",
                json={"app_id": app_id, "confirm": True},
            )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Fixed CV PDF not found", resp.get_json()["error"])
        send.assert_not_called()


class TestFormFiller(FixedCVTestBase):
    def test_form_filler_reports_fixed_cv(self):
        import form_filler
        stale_app = {"cv_pdf_path": str(self.tmp / "cv-llt.pdf"), "form_answers_json": "{}"}
        with mock.patch.object(form_filler, "get_application_by_job", return_value=stale_app), \
                mock.patch.object(fixed_cv, "_load_profile", return_value=self.profile), \
                mock.patch.object(form_filler, "_static_field_mappings", return_value={}):
            instructions = form_filler.get_fill_instructions(FASHION_JOB["url"])
        self.assertEqual(Path(instructions["cv_pdf_path"]), FIXED_CV)


if __name__ == "__main__":
    unittest.main()
