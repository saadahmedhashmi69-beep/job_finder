"""Application route discovery + public-form browser automation.

Run: python -m unittest discover -s tests -p "test_*.py" -v
Everything is local: job pages and forms are fakes (no browser, network or
SMTP), databases are temporary, and the real jobs.db / fixed CV are checked
to be unchanged afterwards.
"""

import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import application_prep  # noqa: E402
import storage  # noqa: E402
import submitter  # noqa: E402
from qualification import MANUAL_REQUIRED, READY_TO_SUBMIT, SUBMISSION_FAILED, SUBMITTED  # noqa: E402
from test_application_workflow import DRIVER, FIXED_CV, WorkflowBase  # noqa: E402

REAL_DB = Path(storage.__file__).resolve().parent / "jobs.db"


def _file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).exists() else ""


def fld(name, type_="text", tag="input", required=False, label="", form=0, options=None, checked=False):
    return {"tag": tag, "type": type_, "name": name, "id": name, "placeholder": "", "aria": "",
            "label": label or name, "role": "", "required": required, "value": "", "checked": checked,
            "options": options or [], "form": form, "hidden": False}


APPLY_FORM = [fld("first_name", required=True, label="First Name"),
              fld("last_name", required=True, label="Last Name"),
              fld("email", "email", required=True, label="Email"),
              fld("phone", "tel", label="Phone"),
              fld("resume", "file", required=True, label="Resume/CV"),
              fld("cover_letter", "", "textarea", label="Cover Letter")]


class FakeButton:
    def __init__(self, page):
        self.page = page

    def click(self):
        self.page.clicked = True
        self.page._html = self.page.after_html
        self.page.url = self.page.after_url or self.page.url


class FakeSite:
    """Playwright-like page over local pages {url: {html, fields, links, submit}}."""

    def __init__(self, pages, after_html="<p>Error</p>", after_url=None):
        self.pages, self.after_html, self.after_url = pages, after_html, after_url
        self.url, self._html, self._p = "", "", {}
        self.visited, self.filled, self.uploads, self.clicked = [], {}, [], False

    def goto(self, url):
        self.url = url
        self.visited.append(url)
        self._p = self.pages.get(url, {"html": "<h1>Not found</h1>", "fields": []})
        self._html = self._p.get("html", "<form></form>")

    def content(self):
        return self._html

    def evaluate(self, js):
        if js == submitter._LINKS_JS:
            return self._p.get("links", [])
        return [dict(f, idx=i) for i, f in enumerate(self._p.get("fields", []))]

    def fill(self, selector, value):
        self.filled[selector] = value

    def select_option(self, selector, label=None):
        self.filled[selector] = label

    def set_input_files(self, selector, path):
        self.uploads.append(path)

    def query_selector(self, sel):
        return FakeButton(self) if self._p.get("submit", True) else None

    def wait_for_load_state(self, *a, **k):
        pass


class RouteBase(WorkflowBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.real_db_sha = _file_sha(REAL_DB)

    @classmethod
    def tearDownClass(cls):
        assert _file_sha(REAL_DB) == cls.real_db_sha, "Real jobs.db was modified by tests!"
        super().tearDownClass()

    def ready(self, job, fetch=None):
        # Unique company per job so duplicate protection does not merge test jobs.
        job = dict(job, company=f"{job['company']} {hashlib.md5(job['url'].encode()).hexdigest()[:6]}")
        self.insert(job)
        r = application_prep.prepare_application(job, self.profile, matcher=self.m, db_path=self.db,
                                                 fetch=fetch or (lambda url: None))
        self.assertEqual(r["status"], READY_TO_SUBMIT, r)
        return r["app_id"]

    def submit(self, app_id, page, mode=None):
        return submitter.submit_application(app_id, self.profile, mode=mode,
                                            page_factory=lambda: (page, lambda: None), db_path=self.db)

    def report(self, app_id):
        return json.loads(self.app_row(app_id)["submission_report_json"] or "{}")


class TestRouteDiscovery(RouteBase):
    def test_linkedin_job_with_ats_link_in_description(self):
        job = dict(DRIVER, url="https://www.linkedin.com/jobs/view/101",
                   description=DRIVER["description"] + " Apply: https://jobs.lever.co/reparto/abc-123")
        m = application_prep.detect_application_method(job)
        self.assertEqual((m["method"], m["route_type"]), ("WEB", "lever"))
        self.assertEqual(m["url"], "https://jobs.lever.co/reparto/abc-123/apply")

    def test_board_apply_url_is_used_instead_of_board(self):
        job = dict(DRIVER, url="https://es.indeed.com/viewjob?jk=1",
                   apply_url="https://careers.reparto.es/ofertas/conductor/aplicar")
        m = application_prep.detect_application_method(job)
        self.assertEqual((m["method"], m["route_type"], m["source"]), ("WEB", "employer", "apply_url"))
        self.assertEqual(m["url"], job["apply_url"])

    def test_linkedin_public_page_exposes_external_apply_url(self):
        page = ('<code id="applyUrl"><!--"https://www.linkedin.com/jobs/view/externalApply/1?url='
                'https%3A%2F%2Fjob-boards.greenhouse.io%2Freparto%2Fjobs%2F42&amp;urlHash=Ab"--></code>')
        job = dict(DRIVER, url="https://www.linkedin.com/jobs/view/102")
        m = application_prep.detect_application_method(job, fetch=lambda url: (url, page))
        self.assertEqual((m["route_type"], m["url"]), ("greenhouse", "https://job-boards.greenhouse.io/reparto/jobs/42"))

    def test_indeed_company_site_redirect_is_resolved(self):
        start = "https://es.indeed.com/applystart?jk=abc&from=vj"
        pages = {"https://es.indeed.com/viewjob?jk=abc": ("https://es.indeed.com/viewjob?jk=abc",
                                                         f'<a href="{start}">Postularse en la web</a>'),
                 start: ("https://apply.workable.com/reparto/j/ABC123/", "<html></html>")}
        job = dict(DRIVER, url="https://es.indeed.com/viewjob?jk=abc")
        m = application_prep.detect_application_method(job, fetch=pages.get)
        self.assertEqual((m["route_type"], m["source"]), ("workable", "apply_redirect"))
        self.assertEqual(m["url"], "https://apply.workable.com/reparto/j/ABC123/apply/")

    def test_supported_ats_hosts(self):
        for url, ats in [("https://boards.greenhouse.io/a/jobs/1", "greenhouse"),
                         ("https://jobs.eu.lever.co/a/1", "lever"),
                         ("https://apply.workable.com/a/j/1", "workable"),
                         ("https://jobs.ashbyhq.com/a/uuid", "ashby"),
                         ("https://jobs.smartrecruiters.com/A/1", "smartrecruiters"),
                         ("https://acme.taleo.net/careersection/2/jobdetail.ftl?job=1", "taleo"),
                         ("https://careers-acme.icims.com/jobs/1/job", "icims"),
                         ("https://www.greenhouse.io/careers", "")]:
            self.assertEqual(application_prep.ats_for_url(url), ats, url)

    def test_board_without_public_route_is_manual(self):
        job = dict(DRIVER, url="https://www.linkedin.com/jobs/view/103")
        m = application_prep.detect_application_method(job, fetch=lambda url: (url, "<p>Sign in</p>"))
        self.assertEqual(m["method"], MANUAL_REQUIRED)

    def test_prepare_stores_discovered_route(self):
        job = dict(DRIVER, url="https://www.linkedin.com/jobs/view/104",
                   description=DRIVER["description"] + " https://boards.greenhouse.io/reparto/jobs/7")
        app = self.app_row(self.ready(job))
        self.assertEqual((app["application_method"], app["route_type"], app["application_url"]),
                         ("WEB", "greenhouse", "https://boards.greenhouse.io/reparto/jobs/7"))

    def test_manual_applications_are_rerouted_when_a_route_appears(self):
        job = dict(DRIVER, url="https://www.linkedin.com/jobs/view/105")
        self.insert(job)
        r = application_prep.prepare_application(job, self.profile, matcher=self.m, db_path=self.db,
                                                 fetch=lambda url: None)
        self.assertEqual(r["status"], MANUAL_REQUIRED)
        page = '<a href="https://jobs.lever.co/reparto/xyz">Apply</a>'
        self.assertEqual(application_prep.reroute_manual_applications(self.db, fetch=lambda u: (u, page)), 1)
        app = self.app_row(r["app_id"])
        self.assertEqual((app["status"], app["route_type"]), (READY_TO_SUBMIT, "lever"))
        self.assertEqual(app["submission_status"], "")  # nothing submitted


class TestPublicForms(RouteBase):
    def test_greenhouse_public_form_dry_run(self):
        url = "https://job-boards.greenhouse.io/reparto/jobs/1"
        app_id = self.ready(dict(DRIVER, url=url))
        site = FakeSite({url: {"fields": APPLY_FORM}})
        res = self.submit(app_id, site)
        self.assertEqual(res["status"], submitter.DRY_RUN_VALIDATED, res)
        self.assertFalse(site.clicked)
        self.assertEqual(site.uploads, [str(FIXED_CV)])
        rep = self.report(app_id)
        self.assertEqual(rep["route_url"], url)
        self.assertEqual(rep["route_type"], "greenhouse")
        self.assertIn("Resume/CV", rep["fields_detected"])
        self.assertEqual(rep["fields_prepared"]["Email"], "email")
        self.assertEqual(rep["would_submit"]["Email"], self.profile["email"])
        self.assertEqual(rep["fields_manual"], [])
        self.assertEqual(self.app_row(app_id)["status"], READY_TO_SUBMIT)

    def test_lever_public_form_uses_apply_page(self):
        app_id = self.ready(dict(DRIVER, url="https://jobs.lever.co/reparto/abc"))
        site = FakeSite({"https://jobs.lever.co/reparto/abc/apply": {"fields": [
            fld("name", required=True, label="Full name"), fld("email", "email", required=True),
            fld("phone", "tel"), fld("location", label="Current location"),
            fld("resume", "file", required=True)]}})
        res = self.submit(app_id, site)
        self.assertEqual(res["status"], submitter.DRY_RUN_VALIDATED, res)
        self.assertEqual(site.visited, ["https://jobs.lever.co/reparto/abc/apply"])
        self.assertEqual(self.report(app_id)["fields_prepared"]["Full name"], "full_name")
        self.assertFalse(site.clicked)

    def test_generic_employer_form_follows_apply_link_and_ignores_other_forms(self):
        post, form = "https://careers.reparto.es/ofertas/42", "https://careers.reparto.es/ofertas/42/form"
        app_id = self.ready(dict(DRIVER, url=post))
        site = FakeSite({
            post: {"fields": [fld("q", label="Search")],
                   "links": [{"href": "https://www.linkedin.com/company/reparto", "text": "Apply"},
                             {"href": form, "text": "Aplicar"}]},
            form: {"fields": [fld("newsletter_email", "email", form=0, label="Newsletter email"),
                              fld("nombre", required=True, form=1, label="Nombre"),
                              fld("correo", "email", required=True, form=1, label="Correo electrónico"),
                              fld("pais", tag="select", form=1, label="País", options=["Francia", "España"]),
                              fld("cv", "file", required=True, form=1, label="Adjunta tu CV")]}})
        res = self.submit(app_id, site)
        self.assertEqual(res["status"], submitter.DRY_RUN_VALIDATED, res)
        self.assertEqual(site.visited, [post, form])
        rep = self.report(app_id)
        self.assertNotIn("Newsletter email", rep["fields_detected"])
        self.assertEqual(rep["would_submit"]["País"], "España")
        self.assertFalse(site.clicked)

    def test_login_walled_form_is_manual(self):
        url = "https://careers.reparto.es/jobs/1"
        for page in ({"html": '<form><input type="password" name="pw"></form>', "fields": APPLY_FORM},
                     {"html": "<p>Sign in to apply for this job</p>", "fields": APPLY_FORM}):
            with self.subTest(page=page["html"]):
                app_id = self.ready(dict(DRIVER, url=url + str(len(page["html"]))))
                site = FakeSite({url + str(len(page["html"])): page})
                self.assertEqual(self.submit(app_id, site)["status"], MANUAL_REQUIRED)
                self.assertIn("Login required", self.app_row(app_id)["status_reason"])
                self.assertEqual(site.uploads, [])

    def test_login_walled_board_route_is_never_opened(self):
        app_id = self.ready(dict(DRIVER, url="https://job-boards.greenhouse.io/reparto/jobs/9"))
        storage.update_application(app_id, db_path=self.db, application_url="https://www.linkedin.com/jobs/view/9")
        opened = mock.Mock()
        res = submitter.submit_application(app_id, self.profile, page_factory=opened, db_path=self.db)
        self.assertEqual(res["status"], MANUAL_REQUIRED)
        opened.assert_not_called()

    def test_captcha_is_manual(self):
        url = "https://job-boards.greenhouse.io/reparto/jobs/3"
        app_id = self.ready(dict(DRIVER, url=url))
        site = FakeSite({url: {"html": '<div class="h-captcha" data-sitekey="x"></div>', "fields": APPLY_FORM}})
        res = self.submit(app_id, site)
        self.assertEqual(res["status"], MANUAL_REQUIRED)
        self.assertIn("CAPTCHA", res["reason"])
        self.assertEqual(site.uploads, [])

    def test_unknown_required_fields_are_manual_and_left_empty(self):
        unknown = [fld("salary_expectation", required=True, label="Salary expectation"),
                   fld("years", "number", required=True, label="Years of driving experience"),
                   fld("consent", "checkbox", required=True, label="I agree to the privacy policy"),
                   fld("work_auth", tag="select", required=True, label="Work authorisation *",
                       options=["", "Yes", "No"]),
                   fld("company_name", required=True, label="Current company name")]
        for extra in unknown:
            with self.subTest(field=extra["label"]):
                url = f"https://job-boards.greenhouse.io/reparto/jobs/u{extra['name']}"
                app_id = self.ready(dict(DRIVER, url=url))
                site = FakeSite({url: {"fields": APPLY_FORM + [extra]}})
                res = self.submit(app_id, site)
                self.assertEqual(res["status"], MANUAL_REQUIRED)
                self.assertIn(extra["label"], self.report(app_id)["fields_manual"])
                idx = len(APPLY_FORM)
                self.assertNotIn(f'[data-jf-idx="{idx}"]', site.filled)
                self.assertFalse(site.clicked)


class TestSubmissionSafety(RouteBase):
    URL = "https://job-boards.greenhouse.io/reparto/jobs/s"

    def live_profile(self, mode="LIVE", allow=True):
        self.profile["pipeline"].update({"submission_mode": mode, "allow_live_submission": allow})

    def test_fixed_cv_checksum_is_verified(self):
        app_id = self.ready(dict(DRIVER, url=self.URL + "1"))
        self.assertEqual(self.app_row(app_id)["cv_sha256"], self.cv_hash)
        for bad in ("0" * 64, ""):
            storage.update_application(app_id, db_path=self.db, cv_sha256=bad)
            site = FakeSite({self.URL + "1": {"fields": APPLY_FORM}})
            self.assertEqual(self.submit(app_id, site)["status"], MANUAL_REQUIRED)
            self.assertEqual(site.visited, [])
            storage.update_application(app_id, db_path=self.db, status=READY_TO_SUBMIT)
        # CV changing between preparation and the LIVE click blocks the submit.
        self.live_profile()
        page = FakeSite({"u": {"fields": APPLY_FORM}})
        with mock.patch.object(submitter, "sha256_file", return_value="f" * 64):
            res = submitter.fill_application_form(page, "u", {"email": "a@b.es"}, FIXED_CV,
                                                  mode=submitter.LIVE, cv_sha256=self.cv_hash)
        self.assertEqual(res["status"], MANUAL_REQUIRED)
        self.assertFalse(page.clicked)

    def test_placeholder_email_is_rejected(self):
        for bad in ("jobs@example.com", "test@test.com", "hr@company.test"):
            job = dict(DRIVER, url="https://www.linkedin.com/jobs/view/e", description=f"Send CV to {bad}")
            m = application_prep.detect_application_method(job)
            self.assertEqual(m["method"], MANUAL_REQUIRED, bad)
            self.assertEqual(m["email"], "")

    def test_dry_run_never_clicks_final_submit(self):
        # Default config, and explicit DRY_RUN even with LIVE fully enabled: never clicks.
        for enable_live in (False, True):
            with self.subTest(enable_live=enable_live):
                if enable_live:
                    self.live_profile()
                url = self.URL + f"2{enable_live}"
                app_id = self.ready(dict(DRIVER, url=url))
                site = FakeSite({url: {"fields": APPLY_FORM}}, after_html="<h1>Thank you for applying!</h1>")
                res = self.submit(app_id, site, mode=submitter.DRY_RUN)
                self.assertEqual(res["status"], submitter.DRY_RUN_VALIDATED)
                self.assertFalse(site.clicked)
                self.assertEqual(self.app_row(app_id)["submitted_at"], "")
                self.assertEqual(self.app_row(app_id)["status"], READY_TO_SUBMIT)

    def test_live_requires_both_flags(self):
        for mode, allow, expect_click in (("LIVE", False, False), ("DRY_RUN", True, False),
                                          ("LIVE", "true", False), ("LIVE", True, True)):
            with self.subTest(mode=mode, allow=allow):
                self.live_profile(mode, allow)
                url = self.URL + f"3{mode}{allow}"
                app_id = self.ready(dict(DRIVER, url=url))
                site = FakeSite({url: {"fields": APPLY_FORM}}, after_html="<h1>Thank you for applying!</h1>")
                res = self.submit(app_id, site, mode=submitter.LIVE)
                self.assertEqual(site.clicked, expect_click)
                self.assertEqual(res["status"], SUBMITTED if expect_click else submitter.DRY_RUN_VALIDATED)

    def test_submitted_requires_observable_evidence(self):
        self.live_profile()
        cases = [({"html": "<p>Thank you for applying!</p>", "fields": APPLY_FORM},  # already on page
                  "<p>Thank you for applying!</p>", None, SUBMISSION_FAILED),
                 ({"fields": APPLY_FORM}, "<form>still here</form>", None, SUBMISSION_FAILED),
                 ({"fields": APPLY_FORM}, "<p>ok</p>", "https://job-boards.greenhouse.io/reparto/jobs/success",
                  SUBMITTED),
                 ({"fields": APPLY_FORM}, "<p>Application ID: GH-99812</p>", None, SUBMITTED)]
        for i, (page, after_html, after_url, expected) in enumerate(cases):
            with self.subTest(case=i):
                url = self.URL + f"4{i}"
                app_id = self.ready(dict(DRIVER, url=url))
                site = FakeSite({url: page}, after_html=after_html, after_url=after_url)
                res = self.submit(app_id, site, mode=submitter.LIVE)
                self.assertTrue(site.clicked)
                self.assertEqual(res["status"], expected)
                app = self.app_row(app_id)
                self.assertEqual(bool(app["submission_evidence"]), expected == SUBMITTED)
                self.assertEqual(bool(app["submitted_at"]), expected == SUBMITTED)
        # SUBMITTED can never be recorded without evidence.
        app_id = self.ready(dict(DRIVER, url=self.URL + "5"))
        self.assertEqual(submitter._record(app_id, self.db, submitter.LIVE, SUBMITTED, "x")["status"],
                         SUBMISSION_FAILED)



class TestExistingManualReroute(RouteBase):
    """`prepare` re-checks existing route-less MANUAL_REQUIRED applications in place."""

    def manual(self, url):
        job = dict(DRIVER, url=url, company=f"Reparto {hashlib.md5(url.encode()).hexdigest()[:6]}")
        self.insert(job)
        r = application_prep.prepare_application(job, self.profile, matcher=self.m, db_path=self.db,
                                                 fetch=lambda u: None)
        self.assertEqual(r["status"], MANUAL_REQUIRED, r)
        return job, r["app_id"]

    def reprepare(self, job, fetch):
        return application_prep.prepare_application(job, self.profile, matcher=self.m, db_path=self.db,
                                                    fetch=fetch)

    def test_existing_manual_gets_route_discovery_attempted(self):
        job, app_id = self.manual("https://es.indeed.com/viewjob?jk=m1")
        fetched = []
        r = self.reprepare(job, lambda u: fetched.append(u) or None)
        self.assertEqual(fetched, [job["url"]])
        self.assertNotEqual(r["status"], "DUPLICATE")
        self.assertEqual((r["status"], r["app_id"]), (MANUAL_REQUIRED, app_id))

    def test_discovered_route_updates_existing_application(self):
        cases = [
            ("greenhouse", '<a href="https://job-boards.greenhouse.io/reparto/jobs/9">Apply</a>', None,
             "https://job-boards.greenhouse.io/reparto/jobs/9"),
            ("lever", '<a href="https://jobs.lever.co/reparto/abc">Apply</a>', None,
             "https://jobs.lever.co/reparto/abc/apply"),
            ("workable", '<a href="https://apply.workable.com/reparto/j/AB12/">Apply</a>', None,
             "https://apply.workable.com/reparto/j/AB12/apply/"),
            ("ashby", '<a href="https://jobs.ashbyhq.com/reparto/1f2e">Apply</a>', None,
             "https://jobs.ashbyhq.com/reparto/1f2e/application"),
            ("employer", "<html></html>", "https://careers.reparto.es/ofertas/7/aplicar",
             "https://careers.reparto.es/ofertas/7/aplicar"),
        ]
        for i, (route, page, redirect, expected) in enumerate(cases):
            with self.subTest(route=route):
                job, app_id = self.manual(f"https://es.indeed.com/viewjob?jk=r{i}")
                before = self.count_apps()
                r = self.reprepare(job, lambda u: (redirect or u, page))
                self.assertEqual((r["status"], r["app_id"]), (READY_TO_SUBMIT, app_id), r)
                self.assertEqual(self.count_apps(), before)  # updated in place, not duplicated
                app = self.app_row(app_id)
                self.assertEqual((app["status"], app["application_method"], app["route_type"],
                                  app["application_url"]), (READY_TO_SUBMIT, "WEB", route, expected))
                self.assertIn(app["route_source"], ("job_page", "job_page_redirect"))
                self.assertEqual(Path(app["cv_pdf_path"]), FIXED_CV)
                self.assertEqual(app["cv_sha256"], self.cv_hash)
                self.assertEqual((app["submission_status"], app["submitted_at"], app["sent_at"]), ("", "", ""))

    def test_existing_manual_without_route_stays_manual(self):
        job, app_id = self.manual("https://es.indeed.com/viewjob?jk=n1")
        r = self.reprepare(job, lambda u: (u, "<p>Inicia sesion para postularte</p>"))
        self.assertEqual((r["status"], r["app_id"]), (MANUAL_REQUIRED, app_id))
        app = self.app_row(app_id)
        self.assertEqual((app["status"], app["application_url"], app["route_type"], app["recruiter_email"]),
                         (MANUAL_REQUIRED, "", "", ""))
        self.assertTrue(app["status_reason"].startswith("Route re-check: No public application route"))
        self.assertEqual(self.count_apps(), 1)

    def test_changed_cv_checksum_blocks_ready(self):
        job, app_id = self.manual("https://es.indeed.com/viewjob?jk=c1")
        storage.update_application(app_id, db_path=self.db, cv_sha256="0" * 64)
        r = self.reprepare(job, lambda u: (u, '<a href="https://jobs.lever.co/reparto/q">Apply</a>'))
        self.assertEqual(r["status"], MANUAL_REQUIRED)
        self.assertIn("SHA-256", self.app_row(app_id)["status_reason"])

    def test_existing_ready_to_submit_is_not_duplicated_or_reset(self):
        url = "https://job-boards.greenhouse.io/reparto/jobs/d1"
        job = dict(DRIVER, url=url, company=f"Reparto {hashlib.md5(url.encode()).hexdigest()[:6]}")
        app_id = self.ready(job)
        before = self.app_row(app_id)
        fetched = []
        r = self.reprepare(job, lambda u: fetched.append(u))
        self.assertEqual((r["status"], r["app_id"]), ("DUPLICATE", app_id))
        self.assertEqual(self.count_apps(), 1)
        self.assertEqual(self.app_row(app_id), before)
        self.assertEqual(fetched, [])

    def test_rerouted_application_dry_run_never_submits_or_emails(self):
        job, app_id = self.manual("https://es.indeed.com/viewjob?jk=s1")
        gh = "https://job-boards.greenhouse.io/reparto/jobs/s1"
        self.reprepare(job, lambda u: (u, f'<a href="{gh}">Apply</a>'))
        site = FakeSite({gh: {"fields": APPLY_FORM}}, after_html="<h1>Thank you for applying!</h1>")
        with mock.patch("smtplib.SMTP", side_effect=AssertionError("SMTP used")):
            res = self.submit(app_id, site)
        self.assertEqual(res["status"], submitter.DRY_RUN_VALIDATED, res)
        self.assertFalse(site.clicked)
        self.assertEqual(site.uploads, [str(FIXED_CV)])
        app = self.app_row(app_id)
        self.assertEqual((app["status"], app["submitted_at"], app["sent_at"]), (READY_TO_SUBMIT, "", ""))

    def test_pipeline_run_does_not_reroute_the_same_application_twice(self):
        import pipeline
        checked, _ = self.manual("https://es.indeed.com/viewjob?jk=p1")  # re-checked by prepare this run
        older, _ = self.manual("https://es.indeed.com/viewjob?jk=p2")    # only the fallback reaches it
        needs_route = application_prep.needs_route
        with mock.patch.object(storage, "DB_PATH", self.db), \
                mock.patch.object(pipeline, "_scrape_all", return_value=[]), \
                mock.patch.object(pipeline, "JobMatcher", return_value=self.m), \
                mock.patch.object(pipeline, "save_jobs", return_value=0), \
                mock.patch.object(pipeline, "get_top_jobs", return_value=[checked]), \
                mock.patch.object(pipeline, "find_existing_application", return_value=None), \
                mock.patch.object(pipeline, "start_pipeline_run", return_value=1), \
                mock.patch.object(pipeline, "finish_pipeline_run"), \
                mock.patch.object(pipeline, "should_send_digest", return_value=False), \
                mock.patch("submitter.process_ready_applications", return_value={}), \
                mock.patch.object(application_prep, "detect_application_method",
                                  wraps=application_prep.detect_application_method) as detect:
            pipeline.run_pipeline(profile=self.profile)
        self.assertEqual(sorted(c.args[0]["url"] for c in detect.call_args_list),
                         sorted([checked["url"], older["url"]]))
        self.assertIs(application_prep.needs_route, needs_route)


if __name__ == "__main__":
    unittest.main()
