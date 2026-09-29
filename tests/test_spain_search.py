"""Spain search config + fashion/driver category matching.

Run: python -m unittest discover -s tests -v
Profile tests use the local (gitignored) profile.yaml and skip if it's absent.
The embedding model is mocked, so no model download is needed.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import matcher  # noqa: E402
from models import Job, JobBoard  # noqa: E402
from scrapers.jobspy_wrapper import _country  # noqa: E402

PROFILE_PATH = PROJECT_ROOT / "profile.yaml"

FASHION_QUERIES = [
    "Fashion Designer", "Fashion Design", "Womenswear Designer", "Ladieswear Designer",
    "Apparel Designer", "Garment Designer", "Fashion Design Assistant",
    "Diseñador de moda", "Diseñador de ropa",
]
DRIVER_QUERIES = [
    "Driver", "Professional Driver", "Delivery Driver", "Van Driver",
    "Uber Driver", "Taxi Driver", "Cab Driver", "Private Driver", "Chauffeur",
    "Conductor", "Chófer", "Taxista", "Repartidor",
]


def _job(title, description, location="Barcelona, Spain"):
    return Job(
        title=title, company="Acme", location=location, url=f"https://example.com/{title}",
        board=JobBoard.INDEED, description=description,
    )


def _load_profile():
    if not PROFILE_PATH.exists():
        raise unittest.SkipTest("profile.yaml not present (local-only file)")
    with open(PROFILE_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


class TestCountryMapping(unittest.TestCase):
    def test_city_country_maps_to_spain(self):
        self.assertEqual(_country("Barcelona, Spain"), "Spain")
        self.assertEqual(_country("Spain"), "Spain")

    def test_existing_mappings_unchanged(self):
        self.assertEqual(_country("Germany"), "Germany")
        self.assertEqual(_country("United Kingdom"), "UK")
        self.assertEqual(_country("Nowhere"), "USA")


class TestSpainProfileConfig(unittest.TestCase):
    def setUp(self):
        self.profile = _load_profile()

    def test_all_required_queries_configured(self):
        queries = self.profile["search"]["queries"]
        for q in FASHION_QUERIES + DRIVER_QUERIES:
            self.assertIn(q, queries)

    def test_ui_quick_scrape_covers_both_categories(self):
        # app.py /api/scrape only uses the first 3 queries.
        first3 = " ".join(self.profile["search"]["queries"][:3]).lower()
        self.assertIn("fashion", first3)
        self.assertIn("driver", first3)

    def test_locations_are_spain_with_barcelona_first(self):
        locs = self.profile["search"]["locations"]
        self.assertEqual(locs[0], "Barcelona, Spain")
        self.assertIn("Spain", locs)
        for loc in locs:
            self.assertEqual(_country(loc), "Spain")

    def test_fixed_cv_unchanged(self):
        self.assertEqual(self.profile["pipeline"]["fixed_cv_path"], "cv/Raheel Tahir Resume Updated.pdf")

    def test_no_spanish_or_eu_licence_claimed(self):
        claims = []
        for cat in self.profile["categories"]:
            claims += cat.get("skills", []) + cat.get("titles", [])
        claims += self.profile.get("skills", [])
        text = " ".join(claims).lower()
        for bad in ["spanish", "español", " eu ", "european", "carnet", "carné", "permiso"]:
            self.assertNotIn(bad, f" {text} ")

    def test_fashion_experience_kept(self):
        fashion = next(c for c in self.profile["categories"] if c["name"] == "fashion")
        self.assertIn("fashion design", fashion["skills"])
        self.assertIn("fashion designer", self.profile["titles"])


class _MatcherBase(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(matcher.JobMatcher, "_semantic_score", return_value=0.5),
            mock.patch.object(matcher, "load_life_story", return_value=""),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class TestCategoryMatching(_MatcherBase):
    def setUp(self):
        super().setUp()
        self.profile = _load_profile()
        self.m = matcher.JobMatcher(self.profile)
        self.threshold = self.profile["pipeline"]["auto_apply_threshold"]

    def test_fashion_job_uses_fashion_category(self):
        score, d = self.m.score(_job(
            "Fashion Designer",
            "Womenswear brand in Barcelona seeks a fashion designer for sketching, "
            "fabric selection, embroidery and sampling of new collections."))
        self.assertEqual(d["category"], "fashion")
        self.assertEqual(d["requirement_flags"], [])
        self.assertGreaterEqual(score, self.threshold)

    def test_driver_job_uses_driver_category(self):
        score, d = self.m.score(_job(
            "Delivery Driver",
            "Delivery driver for van routes around Barcelona. Driving experience valued."))
        self.assertEqual(d["category"], "driver")
        self.assertEqual(d["requirement_flags"], [])
        self.assertGreater(score, 0.30)

    def test_driver_job_requiring_spanish_licence_is_capped(self):
        for req in ["Must hold a valid Spanish driving licence.",
                    "Imprescindible carnet de conducir español.",
                    "Requires an EU driving licence."]:
            score, d = self.m.score(_job("Delivery Driver", f"Van delivery in Barcelona. {req}"))
            self.assertEqual(d["category"], "driver", req)
            self.assertLessEqual(score, 0.30, req)
            self.assertLess(score, self.threshold, req)
            self.assertTrue(any("Spanish/EU" in f for f in d["requirement_flags"]), req)

    def test_driver_job_requiring_professional_licence_is_capped(self):
        score, d = self.m.score(_job("Driver", "Conductor de camión. Carnet C+E y CAP en vigor."))
        self.assertLessEqual(score, 0.30)
        self.assertTrue(d["requirement_flags"])

    def test_carnet_b_is_flagged_not_capped(self):
        plain, _ = self.m.score(_job("Delivery Driver", "Van delivery routes in Barcelona."))
        score, d = self.m.score(_job("Delivery Driver", "Van delivery routes in Barcelona. Carnet B."))
        self.assertTrue(any("carnet B" in f for f in d["requirement_flags"]))
        self.assertGreater(score, 0.30)
        self.assertAlmostEqual(score, plain, delta=0.05)

    def test_fashion_job_not_penalised_by_licence_mention(self):
        score, d = self.m.score(_job(
            "Fashion Designer",
            "Womenswear fashion designer, sketching and sampling. "
            "Se valora carnet de conducir español."))
        self.assertEqual(d["category"], "fashion")
        self.assertEqual(d["requirement_flags"], [])
        self.assertGreaterEqual(score, self.threshold)

    def test_barcelona_preferred_over_rest_of_spain(self):
        desc = "Womenswear fashion designer, sketching and sampling."
        bcn, _ = self.m.score(_job("Fashion Designer", desc, location="Barcelona, Spain"))
        mad, _ = self.m.score(_job("Fashion Designer", desc, location="Madrid, Spain"))
        other, _ = self.m.score(_job("Fashion Designer", desc, location="Paris, France"))
        self.assertGreater(bcn, mad)
        self.assertGreater(mad, other)


class TestAccentTokenization(_MatcherBase):
    def test_accents_are_folded_not_dropped(self):
        # Spanish "/a" gender suffix is dropped too.
        self.assertEqual(matcher.tokenize("Diseñador/a de moda"), ["disenador", "de", "moda"])
        self.assertEqual(matcher.tokenize("Chófer"), ["chofer"])
        self.assertEqual(matcher.tokenize("Conducción CAMIÓN"), ["conduccion", "camion"])

    def test_ascii_tokenization_unchanged(self):
        self.assertEqual(matcher.tokenize("Senior C++/C# Engineer, Node.js"),
                         ["senior", "c++", "c#", "engineer", "node.js"])

    def test_spanish_titles_match_categories(self):
        m = matcher.JobMatcher(_load_profile())
        for title, cat in [("Diseñador/a de moda", "fashion"),
                           ("Disenador de moda", "fashion"),
                           ("Chófer", "driver"),
                           ("Chofer", "driver")]:
            _, d = m.score(_job(title, ""))
            self.assertEqual(d["category"], cat, title)
            self.assertGreater(d["title_score"], 0.85, title)


class TestProfileWithoutCategories(_MatcherBase):
    def test_backward_compatible(self):
        profile = {"titles": ["machine learning engineer"], "skills": ["python"],
                   "keywords": ["computer vision"]}
        score, d = matcher.JobMatcher(profile).score(
            _job("Machine Learning Engineer", "Python computer vision role."))
        self.assertEqual(d["category"], "")
        self.assertEqual(d["requirement_flags"], [])
        self.assertGreater(d["title_score"], 0.9)
        self.assertGreater(score, 0)


if __name__ == "__main__":
    unittest.main()
