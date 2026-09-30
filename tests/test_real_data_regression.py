"""Regression against the real local jobs.db / profile.yaml, used read-only.

The real database is never opened for writing: read-only checks use a
`mode=ro` connection, and anything that has to call code which writes runs on
a temporary COPY. Nothing is sent (senders/browsers are mocks). The file's
SHA-256 is compared before and after. Skipped when the local data is absent.

Covers: existing application records and their duplicate gates, accent-safe
duplicate detection, qualification of the audit's false positives, candidate
selection, fixed-CV hashes, stored route validation and cover-letter grounding.
"""

import copy
import hashlib
import shutil
import sqlite3
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import application_prep  # noqa: E402
import fingerprint  # noqa: E402
import matcher  # noqa: E402
import safety  # noqa: E402
import storage  # noqa: E402
import submitter  # noqa: E402
from qualification import QUALIFIED, READY_TO_SUBMIT, SMTP_ACCEPTED, qualify_job  # noqa: E402
from test_application_workflow import FIXED_CV, PROFILE_PATH, PROJECT_ROOT  # noqa: E402

REAL_DB = PROJECT_ROOT / "jobs.db"


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@unittest.skipUnless(REAL_DB.exists() and PROFILE_PATH.exists() and FIXED_CV.is_file(),
                     "local jobs.db / profile.yaml / fixed CV not present")
class TestRealDataRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_sha, cls.cv_sha = _sha(REAL_DB), _sha(FIXED_CV)
        with open(PROFILE_PATH, encoding="utf-8") as f:
            cls.profile = yaml.safe_load(f)
        ro = sqlite3.connect(f"file:{REAL_DB.as_posix()}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        cls.jobs = {r["url"]: dict(r) for r in ro.execute("SELECT * FROM jobs")}
        cls.apps = [dict(r) for r in ro.execute("SELECT * FROM applications ORDER BY id")]
        ro.close()
        if not cls.apps:
            raise unittest.SkipTest("no application records in the local database")
        cls.tmp = Path(tempfile.mkdtemp())
        cls.copy = cls.tmp / "jobs.db"
        shutil.copy(REAL_DB, cls.copy)
        cls.patches = [mock.patch.object(matcher.JobMatcher, "_semantic_score", return_value=0.5),
                       mock.patch.object(matcher, "load_life_story", return_value=""),
                       mock.patch.object(application_prep, "_http_fetch", return_value=None),
                       mock.patch.object(application_prep, "_web_search", return_value=[]),
                       mock.patch("smtplib.SMTP_SSL", side_effect=AssertionError("real SMTP used"))]
        for p in cls.patches:
            p.start()
        cls.m = matcher.JobMatcher(cls.profile)

    @classmethod
    def tearDownClass(cls):
        for p in reversed(getattr(cls, "patches", [])):
            p.stop()
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)
        assert _sha(REAL_DB) == cls.db_sha, "The real jobs.db was modified by the regression tests!"
        assert _sha(FIXED_CV) == cls.cv_sha, "The fixed CV was modified!"

    def live_profile(self):
        profile = copy.deepcopy(self.profile)
        profile["pipeline"].update({"submission_mode": "LIVE", "allow_live_submission": True})
        return profile

    def job_of(self, app):
        return self.jobs[app["job_url"]]

    def current(self):
        """Applications created by the qualification-gated workflow (they carry a CV checksum)."""
        return [a for a in self.apps if a["cv_sha256"]]

    # -- existing application records --------------------------------------------
    def test_every_application_with_send_evidence_is_permanently_blocked(self):
        sent = [a for a in self.apps if a["sent_at"] or a["submitted_at"] or a["status"] == SMTP_ACCEPTED]
        if not sent:
            self.skipTest("no sent application in the local database")
        profile, sender, browser = self.live_profile(), mock.Mock(return_value=True), mock.Mock()
        for app in sent:
            for forced_status in (app["status"], READY_TO_SUBMIT):  # as recorded, and after a manual reset
                conn = sqlite3.connect(self.copy)
                conn.execute("UPDATE applications SET status = ? WHERE id = ?", (forced_status, app["id"]))
                conn.commit()
                conn.close()
                res = submitter.submit_application(app["id"], profile, email_sender=sender, page_factory=browser,
                                                   db_path=self.copy, matcher=self.m)
                self.assertIn("duplicate", res["reason"].lower(), (app["id"], forced_status, res))
                self.assertFalse(storage.claim_application(app["id"], self.copy), app["id"])
            counts = submitter.process_ready_applications(profile, db_path=self.copy, email_sender=sender,
                                                          page_factory=browser, mode=submitter.LIVE)
            self.assertNotIn(SMTP_ACCEPTED, counts)
        sender.assert_not_called()
        browser.assert_not_called()

    def test_no_stored_application_can_be_sent_under_todays_rules_without_review(self):
        # Whatever state the local records are in, a bulk explicit LIVE run on a copy
        # must not send anything that today's qualification/route/recipient rules reject.
        profile, sender, browser = self.live_profile(), mock.Mock(return_value=True), mock.Mock()
        conn = sqlite3.connect(self.copy)
        conn.execute("UPDATE applications SET status = ? WHERE COALESCE(sent_at, '') = '' AND cv_sha256 <> ''",
                     (READY_TO_SUBMIT,))
        conn.commit()
        conn.close()
        with safety.no_live_sends():  # exactly what the pipeline/daemon does
            counts = submitter.process_ready_applications(profile, db_path=self.copy, email_sender=sender,
                                                          page_factory=browser, mode=submitter.LIVE)
        sender.assert_not_called()
        self.assertNotIn(SMTP_ACCEPTED, counts)
        self.assertNotIn("SUBMITTED", counts)

    # -- duplicate detection --------------------------------------------------------
    def test_accented_and_reposted_titles_find_their_existing_application(self):
        checked = 0
        for app in self.apps:
            job = self.job_of(app)
            if not (job["title"] and job["company"]):
                continue
            for variant in ({"title": job["title"].upper()}, {"title": job["title"].lower()},
                            {"company": job["company"].upper()},
                            {"title": fingerprint.normalize_text(job["title"])}):
                probe = dict(job, **variant)
                found = storage.find_existing_application(
                    "https://es.indeed.com/viewjob?jk=brandnewrepost", probe["title"], probe["company"],
                    location=probe["location"] or "", description=probe["description"] or "", db_path=self.copy)
                self.assertIsNotNone(found, (app["id"], variant))
                checked += 1
            # Tracking-parameter variant of the same posting URL.
            sep = "&" if "?" in job["url"] else "?"
            found = storage.find_existing_application(job["url"] + sep + "utm_source=x&from=serp", db_path=self.copy)
            self.assertEqual(found["id"], app["id"])
        self.assertGreater(checked, 0)

    def test_same_title_and_employer_in_different_cities_stay_separate_vacancies(self):
        groups = {}
        for job in self.jobs.values():
            if fingerprint.is_unnamed_company(job["company"]):
                continue
            key = (fingerprint.normalize_title(job["title"], job["location"]), fingerprint.normalize_company(job["company"]))
            groups.setdefault(key, []).append(job)
        for key, jobs in groups.items():
            cities = {fingerprint.normalize_city(j["location"]) for j in jobs}
            prints = {fingerprint.job_fingerprint(j["title"], j["company"], j["location"], j["description"]) for j in jobs}
            self.assertEqual(len(prints), len(cities), key)  # one vacancy per city, none merged across cities
        # Every stored job keeps its own identity under the canonical URL.
        self.assertEqual(len({fingerprint.canonical_url(u) for u in self.jobs}), len(self.jobs))

    # -- qualification ------------------------------------------------------------------
    def test_audit_false_positives_in_the_real_data_are_no_longer_qualified(self):
        suspects = {
            "sales role asking for a carnet B (#16)": lambda j: "vendedor autoventa" in j["title"].lower(),
            "C+E truck licence (#27)": lambda j: "carnet ce" in j["title"].lower(),
            "self-employed with own van (#32)": lambda j: "autónomo" in j["title"].lower(),
            "police civil-service post (#23)": lambda j: "regió policial" in j["title"].lower(),
            "UK job listed in Spain": lambda j: j["company"] == "Age UK",
            "internship (#34)": lambda j: "prácticas" in j["title"].lower(),
            "car washer (#18)": lambda j: "mozo lavador" in j["title"].lower(),
            "warehouse hand (#26)": lambda j: j["title"].lower().startswith("mozo/a de almacén"),
        }
        found = 0
        for name, match in suspects.items():
            for job in [j for j in self.jobs.values() if match(j) and j["description"]]:
                result = qualify_job(job, self.profile, self.m)
                self.assertNotEqual(result.status, QUALIFIED, (name, job["title"], result.reasons))
                found += 1
        if not found:
            self.skipTest("none of the audit's false-positive jobs are in the local database any more")

    def test_driver_postings_that_state_a_licence_requirement_are_never_auto_qualified(self):
        import re
        licence = re.compile(r"carn[eé]t? de conducir|permiso de conducir|carn[eé]t? b\b|permiso b\b|\(b\)", re.I)
        qualified = held = 0
        for job in self.jobs.values():
            if not job["description"] or (job["match_score"] or 0) < 0.3:
                continue
            result = qualify_job(job, self.profile, self.m)
            if result.category != "driver" and result.status == QUALIFIED:
                continue
            if licence.search(f"{job['title']} {job['description']}"):
                self.assertNotEqual(result.status, QUALIFIED, (job["title"], result.reasons))
                held += 1
            elif result.status == QUALIFIED:
                qualified += 1
        self.assertGreater(held, 0)

    # -- candidate selection ----------------------------------------------------------------
    def test_candidate_window_holds_only_jobs_that_can_progress(self):
        threshold = self.profile["pipeline"].get("auto_apply_threshold", 0.5)
        candidates = storage.get_pipeline_candidates(min_score=threshold, limit=50, db_path=self.copy)
        with_app = {a["job_url"] for a in self.apps}
        for job in candidates:
            self.assertNotIn(job["url"], with_app)
            self.assertTrue((job["description"] or "").strip())
            self.assertGreaterEqual(job["match_score"], threshold)
            self.assertNotIn(job["qualification_status"] or "", storage.DECIDED_JOB_STATES)
        eligible = {j["url"] for j in self.jobs.values() if (j["match_score"] or 0) >= threshold and not j["hidden"]
                    and (j["description"] or "").strip() and j["url"] not in with_app
                    and (j["qualification_status"] or "") not in storage.DECIDED_JOB_STATES}
        self.assertTrue({j["url"] for j in candidates} <= eligible)
        self.assertEqual(len(candidates), min(len(eligible), 50))
        # Description-less rows and rows that already have an application never take a slot.
        blocked = [j for j in self.jobs.values() if (j["match_score"] or 0) >= threshold
                   and (not (j["description"] or "").strip() or j["url"] in with_app)]
        self.assertFalse({j["url"] for j in blocked} & {j["url"] for j in candidates})

    # -- CV hash --------------------------------------------------------------------------------
    def test_recorded_cv_checksums_match_the_fixed_cv(self):
        current = self.current()
        self.assertTrue(current)
        for app in current:
            self.assertEqual(app["cv_sha256"], self.cv_sha, app["id"])
            self.assertEqual(Path(app["cv_pdf_path"]).resolve(), FIXED_CV, app["id"])
        self.assertEqual(application_prep.sha256_file(FIXED_CV), self.cv_sha)

    # -- routes ---------------------------------------------------------------------------------
    def test_stored_web_routes_are_safe_and_only_owned_ones_stay_usable(self):
        web = [a for a in self.apps if a["application_method"] == "WEB" and a["application_url"]]
        if not web:
            self.skipTest("no stored WEB route in the local database")
        for app in web:
            job, url = self.job_of(app), app["application_url"]
            self.assertTrue(safety.is_safe_public_url(url), url)
            problem = application_prep.verify_route(job, {"url": url, "source": app["route_source"]})
            named = application_prep.url_names_employer(url, job["company"])
            self.assertEqual(problem == "", named, (app["id"], url, job["company"]))
            if "corporacionjimenezmana" in url:  # the audit's unrelated-host route (company "AD Málaga")
                self.assertIn("ownership", problem)
            if "join.com/companies/" in url or "todaymobility" in url:
                self.assertEqual(problem, "", url)
        # No stored recipient is a placeholder or a role mailbox without explicit wording.
        for app in self.apps:
            if app["application_method"] == "EMAIL" and app["recruiter_email"]:
                self.assertFalse(application_prep.is_placeholder_email(app["recruiter_email"]), app["id"])
                self.assertEqual(application_prep.find_application_email(self.job_of(app), app["recruiter_email"]),
                                 app["recruiter_email"], app["id"])

    # -- letters ----------------------------------------------------------------------------------
    def test_letters_regenerated_for_real_applications_are_grounded(self):
        facts = application_prep.candidate_facts(self.profile)
        self.assertTrue(facts["phone"].startswith("+"))
        for app in self.current():
            job = self.job_of(app)
            category = job["qualification_category"] or ("fashion" if "design" in job["title"].lower() else "driver")
            letter = application_prep.generate_cover_letter(job, self.profile, category)
            self.assertEqual(application_prep.letter_problems(letter, self.profile), [], app["id"])
            self.assertNotIn("Unknown", letter, app["id"])
            self.assertNotIn("based in Barcelona", letter, app["id"])
            self.assertNotIn(str(self.profile["phone"]), letter, app["id"])
            self.assertIn(self.profile["name"], letter)
            answers = application_prep.generate_form_answers(job, self.profile, category, letter)
            self.assertNotIn("Unknown", answers["why_interested"])
        # Letters stored before the fix are recognised as ungrounded, so they can never be sent as-is.
        for app in self.current():
            body = app["email_body"] or ""
            if "Hiring Team at Unknown" in body or "based in Barcelona" in body or str(self.profile["phone"]) in body:
                self.assertTrue(application_prep.letter_problems(body, self.profile), app["id"])


if __name__ == "__main__":
    unittest.main()
