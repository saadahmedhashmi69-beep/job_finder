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
        self.assertEqual(site.uploads, [str(FIXED_CV)])
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


if __name__ == "__main__":
    unittest.main()
