"""Employer route discovery (employer_routes) before MANUAL_REQUIRED.

Run: python -m unittest discover -s tests -p "test_*.py" -v
The "web" is a local fake: search results and employer/ATS pages are in-memory
dicts. No network, browser or SMTP is used; databases are temporary and the
real jobs.db / fixed CV are checked to be unchanged afterwards.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import application_prep  # noqa: E402
import applier  # noqa: E402
import employer_routes  # noqa: E402
import submitter  # noqa: E402
from qualification import MANUAL_REQUIRED, READY_TO_SUBMIT  # noqa: E402
from test_application_routes import APPLY_FORM, FakeSite, RouteBase, _file_sha  # noqa: E402
from test_application_workflow import DRIVER, FIXED_CV  # noqa: E402

JOB = dict(DRIVER, url="https://www.linkedin.com/jobs/view/9001", company="Reparto Rapido S.L.")
EMPLOYER_URL = "https://empleo.repartorapido.es/ofertas/conductor-furgoneta-madrid"
LEVER_URL = "https://jobs.lever.co/repartorapido/4f1c"
FORM = ('<form action="/candidatura" method="post"><input name="nombre"><input type="email" name="email">'
        '<input type="file" name="cv"><button type="submit">Enviar</button></form>')


def vacancy(title="Conductor/a de furgoneta", company="Reparto Rapido", city="Madrid",
            body=DRIVER["description"], extra=FORM):
    return (f"<html><head><title>{title} - {company}</title></head><body><h1>{title}</h1>"
            f"<p>{company} · {city}</p><p>{body}</p>{extra}</body></html>")


class FakeWeb:
    """Local search engine + pages {url: html | (final_url, html)}."""

    def __init__(self, results, pages):
        self.results, self.pages = results, pages
        self.queries, self.fetched = [], []

    def search(self, query, max_results):
        self.queries.append(query)
        return list(self.results)

    def fetch(self, url):
        self.fetched.append(url)
        page = self.pages.get(url)
        return (url, page) if isinstance(page, str) else page


class EmployerRouteBase(RouteBase):
    def manual(self, job=JOB):
        self.insert(job)
        r = application_prep.prepare_application(job, self.profile, matcher=self.m, db_path=self.db,
                                                 fetch=lambda u: None, search=lambda q, n: [])
        self.assertEqual(r["status"], MANUAL_REQUIRED, r)
        return r["app_id"]

    def reprepare(self, web, job=JOB):
        return application_prep.prepare_application(job, self.profile, matcher=self.m, db_path=self.db,
                                                    fetch=web.fetch, search=web.search)

    def assert_still_manual(self, app_id, web, *in_reason):
        before = self.count_apps()
        r = self.reprepare(web)
        self.assertEqual((r["status"], r["app_id"]), (MANUAL_REQUIRED, app_id), r)
        app = self.app_row(app_id)
        self.assertEqual((app["status"], app["application_method"], app["application_url"], app["route_type"]),
                         (MANUAL_REQUIRED, MANUAL_REQUIRED, "", ""))
        for part in in_reason:
            self.assertIn(part, app["status_reason"])
        self.assertEqual(self.count_apps(), before)
        return app


class TestEmployerRouteFound(EmployerRouteBase):
    def test_exact_employer_vacancy_found(self):
        app_id = self.manual()
        web = FakeWeb([JOB["url"], EMPLOYER_URL], {EMPLOYER_URL: vacancy()})
        r = self.reprepare(web)
        self.assertEqual((r["status"], r["app_id"]), (READY_TO_SUBMIT, app_id), r)
        app = self.app_row(app_id)
        self.assertEqual((app["application_method"], app["route_type"], app["route_source"], app["application_url"]),
                         ("WEB", "employer", "employer_search", EMPLOYER_URL))
        for signal in ("job title", "employer", "location", "description overlap"):
            self.assertIn(signal, app["status_reason"])
        self.assertIn("Reparto Rapido", web.queries[0])
        self.assertIn(JOB["title"], web.queries[0])

    def test_ats_vacancy_found_through_its_apply_link(self):
        app_id = self.manual()
        posting = vacancy(title="Conductor de furgoneta (H/M)", company="Logistica",
                          extra='<a href="/repartorapido/4f1c/apply">Apply for this job</a>')
        web = FakeWeb([EMPLOYER_URL, LEVER_URL],
                      {EMPLOYER_URL: vacancy(title="Trabaja con nosotros"), LEVER_URL: posting,
                       LEVER_URL + "/apply": f"<h2>Submit your application</h2>{FORM}"})
        r = self.reprepare(web)
        self.assertEqual((r["status"], r["app_id"]), (READY_TO_SUBMIT, app_id), r)
        app = self.app_row(app_id)
        self.assertEqual((app["route_type"], app["route_source"], app["application_url"]),
                         ("lever", "employer_search", LEVER_URL + "/apply"))
        self.assertEqual(web.fetched[1], LEVER_URL)  # ATS candidates are checked first

    def test_city_appended_to_the_board_title_is_ignored(self):
        for i, title in enumerate(("Conductor/a de furgoneta - Madrid", "Conductor/a de furgoneta (Madrid)")):
            with self.subTest(title):
                job = dict(JOB, title=title, url=f"{JOB['url']}{i}", company=f"Reparto Rapido {i}")
                self.insert(job)
                web = FakeWeb([EMPLOYER_URL], {EMPLOYER_URL: vacancy()})
                self.assertEqual(self.reprepare(web, job)["status"], READY_TO_SUBMIT)
                self.assertIn('"Conductor/a de furgoneta" Madrid', web.queries[0])

    def test_vacancy_reference_identifies_the_same_job(self):
        job = dict(JOB, description=JOB["description"] + " Referencia: RR-2041")
        self.insert(job)
        page = vacancy(city="Zona centro", body="Buscamos personal. Oferta RR-2041.")
        web = FakeWeb([EMPLOYER_URL], {EMPLOYER_URL: page})
        r = self.reprepare(web, job)
        self.assertEqual(r["status"], READY_TO_SUBMIT, r)
        self.assertIn("vacancy reference", self.app_row(r["app_id"])["status_reason"])

    def test_existing_application_updated_in_place_with_fixed_cv_unchanged(self):
        app_id = self.manual()
        before, real_db = self.app_row(app_id), _file_sha(application_prep.storage.DB_PATH)
        r = self.reprepare(FakeWeb([EMPLOYER_URL], {EMPLOYER_URL: vacancy()}))
        self.assertEqual((r["status"], r["app_id"]), (READY_TO_SUBMIT, app_id))
        self.assertEqual(self.count_apps(), 1)
        app = self.app_row(app_id)
        for key in ("id", "job_url", "cv_pdf_path", "cv_sha256", "form_answers_json", "email_body"):
            self.assertEqual(app[key], before[key], key)
        self.assertEqual((Path(app["cv_pdf_path"]), app["cv_sha256"]), (FIXED_CV, self.cv_hash))
        self.assertEqual(_file_sha(FIXED_CV), self.cv_hash)
        self.assertEqual((app["submission_status"], app["submitted_at"], app["sent_at"]), ("", "", ""))
        self.assertEqual(_file_sha(application_prep.storage.DB_PATH), real_db)

    def test_batch_reroute_uses_employer_search(self):
        app_id = self.manual()
        web = FakeWeb([EMPLOYER_URL], {EMPLOYER_URL: vacancy()})
        self.assertEqual(application_prep.reroute_manual_applications(self.db, fetch=web.fetch, search=web.search), 1)
        self.assertEqual(self.app_row(app_id)["application_url"], EMPLOYER_URL)

    def test_discovered_route_dry_run_never_submits_or_emails(self):
        app_id = self.manual()
        web = FakeWeb([EMPLOYER_URL], {EMPLOYER_URL: vacancy()})
        sender = mock.Mock()
        with mock.patch("smtplib.SMTP", side_effect=AssertionError("SMTP used")), \
                mock.patch.object(applier, "send_application_email", sender):
            self.reprepare(web)
            site = FakeSite({EMPLOYER_URL: {"fields": APPLY_FORM}}, after_html="<h1>Thank you for applying!</h1>")
            res = self.submit(app_id, site)
        self.assertEqual(res["status"], submitter.DRY_RUN_VALIDATED, res)
        self.assertEqual(res["mode"], submitter.DRY_RUN)
        self.assertFalse(site.clicked)
        self.assertEqual((site.uploads, site.filled), ([], {}))  # DRY_RUN touches nothing
        sender.assert_not_called()
        app = self.app_row(app_id)
        self.assertEqual((app["status"], app["submitted_at"], app["sent_at"]), (READY_TO_SUBMIT, "", ""))


class TestEmployerRouteRejected(EmployerRouteBase):
    def test_wrong_vacancy_rejected(self):
        app_id = self.manual()
        wrong = {"other role": vacancy(title="Mozo/a de almacen"),
                 "similar role": vacancy(title="Conductor/a de camion con remolque"),
                 "other city and text": vacancy(city="Sevilla", body="Ruta nocturna de paqueteria en Andalucia."),
                 "other employer": vacancy(company="Otra Empresa")}
        for name, page in wrong.items():
            with self.subTest(name):
                url = LEVER_URL.replace("repartorapido", "acme") if name == "other employer" else EMPLOYER_URL
                self.assert_still_manual(app_id, FakeWeb([url], {url: page}), "not verified as the same vacancy")

    def test_careers_homepage_rejected(self):
        app_id = self.manual()
        home = "https://www.repartorapido.es/empleo"
        page = ("<html><head><title>Trabaja con nosotros | Reparto Rapido</title></head><body>"
                "<h1>Ofertas de empleo</h1><ul><li>Conductor/a de furgoneta - Madrid</li><li>Mozo</li></ul>"
                f"<p>{DRIVER['description']}</p><h2>Candidatura espontanea</h2>{FORM}</body></html>")
        app = self.assert_still_manual(app_id, FakeWeb([home], {home: page}), home, "no match on job title")
        self.assertTrue(app["status_reason"].startswith("Route re-check: No public application route"))

    def test_same_vacancy_without_public_form_rejected(self):
        app_id = self.manual()
        login = "https://empleo.repartorapido.es/login?oferta=7"
        cases = {
            "no form": ({EMPLOYER_URL: vacancy(extra="")}, "not an application form"),
            "search form only": ({EMPLOYER_URL: vacancy(extra='<form><input name="q"></form>')},
                                 "not an application form"),
            "login wall": ({EMPLOYER_URL: vacancy(extra='<form><input type="password" name="p">'
                                                        '<input type="file" name="cv"></form>')}, "Login required"),
            "captcha": ({EMPLOYER_URL: vacancy(extra=FORM + '<div class="g-recaptcha"></div>')}, "CAPTCHA"),
            "apply link to login": ({EMPLOYER_URL: vacancy(extra=f'<a href="{login}">Inscribirme</a>'),
                                     login: FORM}, "not an application form"),
            "apply link to board": ({EMPLOYER_URL: vacancy(extra='<a href="https://www.linkedin.com/jobs/view/9001">'
                                                                 'Apply</a>')}, "not an application form"),
            "apply page without form": ({EMPLOYER_URL: vacancy(extra='<a href="/ofertas/7/aplicar">Aplicar</a>'),
                                         "https://empleo.repartorapido.es/ofertas/7/aplicar": "<p>Llamanos</p>"},
                                        "no public application form"),
        }
        for name, (pages, reason) in cases.items():
            with self.subTest(name):
                web = FakeWeb([EMPLOYER_URL], pages)
                self.assert_still_manual(app_id, web, "same vacancy, but", reason)
                self.assertNotIn(login, web.fetched)
                self.assertNotIn("https://www.linkedin.com/jobs/view/9001", web.fetched[1:])

    def test_no_route_remains_manual_and_walled_pages_are_never_opened(self):
        app_id = self.manual()
        off_limits = ["https://www.google.com/maps/place/Reparto+Rapido",
                      "https://es.indeed.com/viewjob?jk=abc",
                      "https://www.linkedin.com/company/reparto-rapido/jobs",
                      "https://www.jobaggregator.es/oferta/conductor-furgoneta",   # not the employer, not an ATS
                      "https://www.greenhouse.io/careers",                         # ATS marketing site
                      "https://empleo.repartorapido.es/login"]
        web = FakeWeb(off_limits, {u: vacancy() for u in off_limits})
        self.assert_still_manual(app_id, web, "No public application route",
                                 "returned no employer-domain or public ATS page")
        self.assertEqual(web.fetched, [JOB["url"]])  # only the existing job-page check
        self.assertEqual(len(web.queries), employer_routes.MAX_QUERIES)
        # Search unavailable / failing: still a clean MANUAL_REQUIRED.
        for search in (lambda q, n: [], mock.Mock(side_effect=RuntimeError("offline"))):
            r = application_prep.prepare_application(JOB, self.profile, matcher=self.m, db_path=self.db,
                                                     fetch=lambda u: None, search=search)
            self.assertEqual((r["status"], r["app_id"]), (MANUAL_REQUIRED, app_id))

    def test_search_and_fetches_are_bounded(self):
        app_id = self.manual()
        urls = [f"https://empleo.repartorapido.es/ofertas/{i}" for i in range(30)]
        web = FakeWeb(urls, {u: vacancy(title="Otra oferta") for u in urls})
        self.assert_still_manual(app_id, web, "checked 5 candidate page(s)")
        self.assertLessEqual(len(web.queries), employer_routes.MAX_QUERIES)
        self.assertEqual(len(web.fetched) - 1, employer_routes.MAX_CANDIDATES)  # minus the job-page check

    def test_unnamed_employer_is_not_searched(self):
        web = FakeWeb([EMPLOYER_URL], {EMPLOYER_URL: vacancy()})
        for company in ("Unknown", "", "Confidencial"):
            m = application_prep.detect_application_method(dict(JOB, company=company), fetch=web.fetch,
                                                           search=web.search)
            self.assertEqual(m["method"], MANUAL_REQUIRED)
            self.assertIn("does not name the employer", m["reason"])
        self.assertEqual(web.queries, [])

    def test_no_search_means_no_network(self):
        self.assertEqual(application_prep.detect_application_method(JOB)["method"], MANUAL_REQUIRED)
        m = application_prep.detect_application_method(JOB, fetch=lambda u: None)
        self.assertEqual(m["method"], MANUAL_REQUIRED)
        self.assertNotIn("Employer route search", m["reason"])

    def test_email_route_is_not_replaced_by_search(self):
        job = dict(JOB, description=JOB["description"] + " Envia tu CV a seleccion@repartorapido.es")
        web = FakeWeb([EMPLOYER_URL], {EMPLOYER_URL: vacancy()})
        m = application_prep.detect_application_method(job, fetch=web.fetch, search=web.search)
        self.assertEqual((m["method"], m["email"]), ("EMAIL", "seleccion@repartorapido.es"))
        self.assertEqual(web.queries, [])


# --- Unknown employer resolution ------------------------------------------------

UNKNOWN = dict(JOB, company="Unknown", url="https://es.indeed.com/viewjob?jk=unk1")


def ld(title=JOB["title"], org="Reparto Rapido"):
    return ('<script type="application/ld+json">{"@context": "https://schema.org", "@type": "JobPosting", '
            f'"title": "{title}", "hiringOrganization": {{"@type": "Organization", "name": "{org}"}}}}</script>')


def header(title=JOB["title"], org="Reparto Rapido"):
    return f'<h1>{title}</h1><a class="topcard__org-name-link" href="#"> {org} </a>'


class TestUnknownEmployer(EmployerRouteBase):
    def resolve(self, page):
        return employer_routes.resolve_employer(UNKNOWN, lambda u: (u, page) if page is not None else None)

    def job_row(self, url):
        conn = application_prep.storage.get_db(self.db)
        row = dict(conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone())
        conn.close()
        return row

    def test_employer_extracted_from_public_posting(self):
        for page in (ld(), header(), ld() + header(), ld(title="Conductor de furgoneta (H/M)")):
            self.assertEqual(self.resolve(page), "Reparto Rapido", page)

    def test_employer_stays_unknown_when_ambiguous_or_unverified(self):
        pages = {"not reachable": None,
                 "no employer on page": "<h1>Conductor/a de furgoneta</h1><p>Buscamos repartidor.</p>",
                 "name only in free text": "<h1>Conductor/a de furgoneta</h1><p>Reparto Rapido busca personal</p>",
                 "other vacancy (json-ld)": ld(title="Mozo/a de almacen"),
                 "other vacancy (header)": header(title="Ofertas de empleo"),
                 "conflicting names": ld(org="Reparto Rapido") + header(org="Otra Empresa"),
                 "job board as employer": ld(org="Indeed"),
                 "aggregator as employer": header(org="Jooble Empleo"),
                 "placeholder": ld(org="Confidencial")}
        for name, page in pages.items():
            with self.subTest(name):
                self.assertEqual(self.resolve(page), "")

    def test_unverified_employer_is_never_searched_or_saved(self):
        app_id = self.manual(UNKNOWN)
        web = FakeWeb([EMPLOYER_URL], {UNKNOWN["url"]: "<h1>Conductor/a de furgoneta</h1>", EMPLOYER_URL: vacancy()})
        r = self.reprepare(web, UNKNOWN)
        self.assertEqual((r["status"], r["app_id"]), (MANUAL_REQUIRED, app_id))
        self.assertEqual(web.queries, [])  # no search from a guessed name
        self.assertEqual(self.job_row(UNKNOWN["url"])["company"], "Unknown")
        self.assertIn("could not be verified from the public job page", self.app_row(app_id)["status_reason"])

    def test_resolved_employer_enables_route_search_and_updates_in_place(self):
        app_id = self.manual(UNKNOWN)
        before = self.job_row(UNKNOWN["url"])
        web = FakeWeb([EMPLOYER_URL], {UNKNOWN["url"]: ld(), EMPLOYER_URL: vacancy()})
        r = self.reprepare(web, UNKNOWN)
        self.assertEqual((r["status"], r["app_id"]), (READY_TO_SUBMIT, app_id), r)
        self.assertIn('"Reparto Rapido"', web.queries[0])
        job = self.job_row(UNKNOWN["url"])
        self.assertEqual(job["company"], "Reparto Rapido")
        for key in ("url", "title", "board", "location", "description", "qualification_status"):
            self.assertEqual(job[key], before[key], key)
        app = self.app_row(app_id)
        self.assertEqual((app["route_source"], app["application_url"], app["cv_sha256"]),
                         ("employer_search", EMPLOYER_URL, self.cv_hash))
        # Duplicate protection: by URL, and by title + (now resolved) employer on another board URL.
        self.assertEqual(self.reprepare(web, UNKNOWN)["status"], "DUPLICATE")
        twin = dict(UNKNOWN, company="Reparto Rapido", url="https://www.linkedin.com/jobs/view/777")
        self.insert(twin)
        self.assertEqual((self.reprepare(web, twin)["status"], self.count_apps()), ("DUPLICATE", 1))

    def test_resolved_employer_without_route_is_saved_and_stays_manual(self):
        app_id = self.manual(UNKNOWN)
        web = FakeWeb([], {UNKNOWN["url"]: header()})
        r = self.reprepare(web, UNKNOWN)
        self.assertEqual((r["status"], r["app_id"]), (MANUAL_REQUIRED, app_id))
        self.assertEqual(self.job_row(UNKNOWN["url"])["company"], "Reparto Rapido")
        self.assertEqual(len(web.queries), employer_routes.MAX_QUERIES)

    def test_cover_letter_never_addresses_a_placeholder_employer(self):
        letter = application_prep.generate_cover_letter(UNKNOWN, self.profile, "driver")
        self.assertNotIn("Unknown", letter)
        self.assertIn("Dear Hiring Team,", letter)  # truthful generic greeting, no invented name
        self.assertNotIn("your company", letter)


# --- Browser driver stops, DRY_RUN report, pipeline submission step --------------

TITLE_HTML = "<h1>Conductor/a de furgoneta</h1><form></form>"
GH_URL = "https://job-boards.greenhouse.io/reparto/jobs/"


class TestDriverAndPipelineStep(RouteBase):
    def live(self, mode="LIVE", allow=True):
        self.profile["pipeline"].update({"submission_mode": mode, "allow_live_submission": allow})

    def process(self, site, sender=None):
        factory = mock.Mock(side_effect=lambda: (site, lambda: None))
        counts = submitter.process_ready_applications(self.profile, db_path=self.db, page_factory=factory,
                                                      email_sender=sender)
        return counts, factory

    def test_dry_run_report_has_route_vacancy_cv_and_field_evidence(self):
        url = GH_URL + "r1"
        app_id = self.ready(dict(DRIVER, url=url))
        site = FakeSite({url: {"html": TITLE_HTML, "fields": APPLY_FORM}}, after_html="<p>Thank you for applying</p>")
        res = self.submit(app_id, site)
        self.assertEqual(res["status"], submitter.DRY_RUN_VALIDATED, res)
        rep = self.report(app_id)
        self.assertEqual((rep["route_url"], rep["route_type"]), (url, "greenhouse"))
        # DRY_RUN reports what WOULD be uploaded; nothing is uploaded.
        self.assertEqual((rep["cv_path"], rep["cv_sha256"], rep["cv_uploaded"]),
                         (str(FIXED_CV), self.cv_hash, ""))
        self.assertEqual(rep["would_submit"]["Resume/CV"], FIXED_CV.name)
        self.assertEqual(site.uploads, [])
        self.assertTrue(rep["vacancy_match"]["verified"])
        self.assertIn("job title", rep["vacancy_match"]["matched"])
        self.assertIn("Email", rep["fields_prepared"])
        self.assertEqual(rep["fields_manual"], [])
        self.assertFalse(site.clicked)

    def test_other_vacancy_and_payment_pages_stop_before_anything_is_filled(self):
        cases = {"other vacancy": ("<html><head><title>Mozo de almacen - Reparto</title></head>"
                                   "<h1>Mozo de almacen</h1><form></form></html>", "does not show the target vacancy"),
                 "fee": (TITLE_HTML + "<p>A registration fee of 30 EUR applies.</p>", "Payment/fee requested"),
                 "card": (TITLE_HTML + '<input name="card_number">', "Payment/fee requested")}
        self.live()  # even with LIVE fully enabled
        for i, (name, (page_html, reason)) in enumerate(cases.items()):
            with self.subTest(name):
                url = f"{GH_URL}x{i}"
                app_id = self.ready(dict(DRIVER, url=url))
                site = FakeSite({url: {"html": page_html, "fields": APPLY_FORM}})
                res = self.submit(app_id, site)
                self.assertEqual(res["status"], MANUAL_REQUIRED)
                self.assertIn(reason, res["reason"])
                self.assertEqual((site.uploads, site.filled, site.clicked), ([], {}, False))

    def test_pipeline_step_dry_run_validates_once_and_never_submits(self):
        url = GH_URL + "p1"
        app_id = self.ready(dict(DRIVER, url=url))
        manual_job = dict(DRIVER, url="https://www.linkedin.com/jobs/view/555", company="Otra SL")
        self.insert(manual_job)
        application_prep.prepare_application(manual_job, self.profile, matcher=self.m, db_path=self.db,
                                             fetch=lambda u: None, search=lambda q, n: [])
        self.live("LIVE", False)  # one flag alone is still DRY_RUN
        site = FakeSite({url: {"html": TITLE_HTML, "fields": APPLY_FORM}}, after_html="<p>Thank you for applying</p>")
        counts, factory = self.process(site)
        self.assertEqual(counts, {submitter.DRY_RUN_VALIDATED: 1})  # MANUAL_REQUIRED app untouched
        self.assertFalse(site.clicked)
        app = self.app_row(app_id)
        self.assertEqual((app["status"], app["submission_mode"], app["submitted_at"]), (READY_TO_SUBMIT, "DRY_RUN", ""))
        counts, factory = self.process(site)
        self.assertEqual(counts, {})
        factory.assert_not_called()

    def test_pipeline_step_live_submits_once_with_evidence_and_never_again(self):
        self.live()
        url = GH_URL + "p2"
        app_id = self.ready(dict(DRIVER, url=url))
        site = FakeSite({url: {"html": TITLE_HTML, "fields": APPLY_FORM}}, after_html="<p>Thank you for applying!</p>")
        counts, _ = self.process(site)
        self.assertEqual(counts, {"SUBMITTED": 1})
        app = self.app_row(app_id)
        self.assertTrue(app["submission_evidence"] and app["submitted_at"])
        self.assertEqual(site.uploads, [str(FIXED_CV)])
        again = FakeSite({url: {"html": TITLE_HTML, "fields": APPLY_FORM}})
        counts, factory = self.process(again)
        self.assertEqual(counts, {})
        factory.assert_not_called()
        self.assertIn("duplicate submission blocked", self.submit(app_id, again, mode=submitter.LIVE)["reason"])
        self.assertFalse(again.clicked)

    def test_pipeline_step_live_ambiguous_result_is_not_submitted_or_retried(self):
        self.live()
        url = GH_URL + "p3"
        app_id = self.ready(dict(DRIVER, url=url))
        site = FakeSite({url: {"html": TITLE_HTML, "fields": APPLY_FORM}}, after_html="<form>try again</form>")
        self.assertEqual(self.process(site)[0], {"SUBMISSION_FAILED": 1})
        app = self.app_row(app_id)
        self.assertEqual((app["status"], app["submitted_at"], app["submission_evidence"]), ("SUBMISSION_FAILED", "", ""))
        counts, factory = self.process(site)
        self.assertEqual(counts, {})
        factory.assert_not_called()

    def test_pipeline_step_email_is_never_sent_in_dry_run_nor_submitted_in_live(self):
        job = dict(DRIVER, url="https://www.linkedin.com/jobs/view/88",
                   description=DRIVER["description"] + " Envia tu CV a empleo@repartorapido.es")
        self.insert(job)
        r = application_prep.prepare_application(job, self.profile, matcher=self.m, db_path=self.db,
                                                 fetch=lambda u: None, search=lambda q, n: [])
        self.assertEqual((r["status"], r["method"]), (READY_TO_SUBMIT, "EMAIL"))
        sender = mock.Mock(return_value=True)
        self.assertEqual(self.process(None, sender)[0], {submitter.DRY_RUN_VALIDATED: 1})
        sender.assert_not_called()
        self.live()
        self.assertEqual(self.process(None, sender)[0], {"SMTP_ACCEPTED": 1})
        sender.assert_called_once()
        self.assertEqual(sender.call_args.kwargs["cv_path"], FIXED_CV)
        app = self.app_row(r["app_id"])
        self.assertEqual((app["status"], app["submitted_at"]), ("SMTP_ACCEPTED", ""))
        self.assertEqual(self.process(None, sender)[0], {})
        sender.assert_called_once()


if __name__ == "__main__":
    unittest.main()
