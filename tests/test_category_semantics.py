"""Category-specific semantic matching (real embedding model, no mocks).

After the scope gate places a job in a category, its semantic score is computed
against that category's `semantic_profile`, not the fashion-heavy global
profile. Gates, evidence rules and licence caps stay authoritative.

Run: python -m unittest discover -s tests -v
Uses the local (gitignored) profile.yaml and the locally cached
all-MiniLM-L6-v2 model; skips (never downloads) if either is absent.
"""

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import matcher  # noqa: E402
from models import Job, JobBoard  # noqa: E402

PROFILE_PATH = PROJECT_ROOT / "profile.yaml"
MODEL_CACHE = Path.home() / ".cache" / "huggingface" / "hub" / "models--sentence-transformers--all-MiniLM-L6-v2"

WOMENSWEAR = "Design womenswear collections: sketching, fabric selection, embroidery and sampling."
WOMENSWEAR_ES = "Diseño de colecciones de moda mujer: bocetos, tejidos, bordado y muestras."
PASSENGERS = "Drive passengers around Barcelona in the company car. Driving licence required."
VAN = "Drive the company van delivering parcels around Barcelona. Driving licence required."


def _job(title, description=""):
    return Job(title=title, company="", location="Barcelona, Spain",
               url=f"https://example.com/{title}", board=JobBoard.INDEED, description=description)


def _load_profile():
    if not PROFILE_PATH.exists():
        raise unittest.SkipTest("profile.yaml not present (local-only file)")
    with open(PROFILE_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


class TestCategorySemanticWiring(unittest.TestCase):
    """Mocked: which reference the semantic score is computed against."""

    def setUp(self):
        p = mock.patch.object(matcher, "load_life_story", return_value="")
        p.start()
        self.addCleanup(p.stop)
        self.profile = _load_profile()

    def test_placed_job_uses_its_category_reference(self):
        m = matcher.JobMatcher(self.profile)
        with mock.patch.object(matcher.JobMatcher, "_semantic_score", return_value=0.5) as sem:
            _, d = m.score(_job("Taxi Driver", PASSENGERS))
        self.assertEqual(d["semantic_reference"], "driver")
        self.assertEqual(sem.call_args.args[1]["name"], "driver")

    def test_rejected_job_uses_global_reference(self):
        m = matcher.JobMatcher(self.profile)
        with mock.patch.object(matcher.JobMatcher, "_semantic_score", return_value=0.9) as sem:
            score, d = m.score(_job("Software Engineer - Device Driver", "Write Linux device drivers."))
        self.assertEqual(d["semantic_reference"], "global")
        self.assertIsNone(sem.call_args.args[1])
        self.assertEqual(score, 0.0)

    def test_semantic_profile_text_used_when_configured(self):
        m = matcher.JobMatcher(self.profile)
        texts = {c["name"]: c["semantic_text"] for c in m._categories}
        self.assertIn("passenger transport", texts["driver"])
        self.assertIn("Carnet B", texts["driver"])
        self.assertIn("womenswear", texts["fashion"])
        self.assertIn("diseñadora de", texts["fashion"])

    def test_fallback_text_built_from_category_terms(self):
        cat = matcher.JobMatcher._prepare_category(
            {"name": "x", "titles": ["taxi driver"], "skills": ["driving"], "keywords": ["taxi"]})
        self.assertEqual(cat["semantic_text"],
                         "Desired roles: taxi driver Skills: driving Expertise in: taxi")


@unittest.skipUnless(MODEL_CACHE.exists(), "all-MiniLM-L6-v2 not cached locally (not downloading)")
class TestCategorySemanticsRealModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.profile = _load_profile()
        cls.threshold = cls.profile["pipeline"]["auto_apply_threshold"]
        cls.m = matcher.JobMatcher(cls.profile)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def assertPasses(self, title, description, category):
        score, d = self.m.score(_job(title, description))
        msg = f"{title!r}: {score} {d}"
        self.assertEqual(d["category"], category, msg)
        self.assertEqual(d["rejected_reason"], "", msg)
        self.assertEqual(d["requirement_flags"], [], msg)
        self.assertEqual(d["semantic_reference"], category, msg)
        self.assertGreaterEqual(score, self.threshold, msg)

    def assertRejected(self, title, description=""):
        score, d = self.m.score(_job(title, description))
        self.assertEqual(score, 0.0, (title, d))
        self.assertTrue(d["rejected_reason"], (title, d))

    def test_threshold_unchanged(self):
        self.assertEqual(self.threshold, 0.50)

    # --- FASHION ---
    def test_womenswear_titles_pass(self):
        self.assertPasses("Senior Womenswear Fashion Designer", WOMENSWEAR, "fashion")
        self.assertPasses("Womenswear Designer", WOMENSWEAR, "fashion")

    def test_spanish_womenswear_materially_improved(self):
        fashion = next(c for c in self.m._categories if c["name"] == "fashion")
        for title in ["Diseñador/a de moda mujer", "Diseñadora de ropa de mujer"]:
            job = _job(title, WOMENSWEAR_ES)
            before = self.m._semantic_score(job)            # global profile
            after = self.m._semantic_score(job, fashion)    # fashion reference
            self.assertGreaterEqual(after - before, 0.20, (title, before, after))
        self.assertPasses("Diseñador/a de moda mujer", WOMENSWEAR_ES, "fashion")

    def test_non_womenswear_designers_still_rejected(self):
        for title in ["Menswear Designer", "Kidswear Designer", "Product Designer", "Footwear Designer"]:
            self.assertRejected(title, WOMENSWEAR)

    # --- DRIVER (with genuine driving evidence) ---
    def test_passenger_driver_titles_pass(self):
        for title in ["Professional Driver", "Uber Driver", "Taxi Driver", "Cab Driver",
                      "Private Driver", "Chauffeur", "Conductor", "Chófer", "Taxista"]:
            self.assertPasses(title, PASSENGERS, "driver")

    def test_van_delivery_driver_titles_pass(self):
        self.assertPasses("Repartidor/a en furgoneta", VAN, "driver")
        self.assertPasses("Delivery Driver - Van", VAN, "driver")

    def test_driver_semantic_materially_improved(self):
        driver = next(c for c in self.m._categories if c["name"] == "driver")
        for title in ["Taxi Driver", "Private Driver", "Delivery Driver - Van"]:
            job = _job(title, PASSENGERS)
            self.assertGreaterEqual(
                self.m._semantic_score(job, driver) - self.m._semantic_score(job), 0.30, title)

    # --- NEGATIVE ---
    def test_non_driving_driver_titles_still_rejected(self):
        self.assertRejected("Software Engineer - Device Driver", "Write Linux device drivers in C.")
        self.assertRejected("Linux Kernel Driver Developer", "Kernel and driver development.")

    def test_unsupported_driver_titles_still_held(self):
        for title, desc in [("Professional Driver of Change",
                             "Be a professional driver of change for our clients."),
                            ("Driver", "")]:
            score, d = self.m.score(_job(title, desc))
            self.assertLessEqual(score, 0.30, title)
            self.assertLess(score, self.threshold, title)
            self.assertTrue(any("No evidence this is a real driving role" in f
                                for f in d["requirement_flags"]), (title, d))

    def test_bike_motorbike_delivery_still_rejected(self):
        self.assertRejected("Repartidor/a en bici", "Reparto de comida a domicilio en bicicleta.")
        self.assertRejected("Repartidor/a MOTO PROPIA", "Reparto de pedidos con tu moto.")
        self.assertRejected("Delivery Driver", "Deliver food orders by e-bike around Barcelona.")

    def test_professional_licence_cap_still_applies(self):
        score, d = self.m.score(_job("Chófer C+E", VAN + " Carnet C+E y CAP en vigor."))
        self.assertLessEqual(score, 0.30)
        self.assertTrue(any("professional licence" in f for f in d["requirement_flags"]), d)


if __name__ == "__main__":
    unittest.main()
