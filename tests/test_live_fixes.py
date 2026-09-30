"""Live-readiness fixes: Indeed Spain locations, title containment, Spanish terms.

Strong explicit target titles must reach 0.50 without relying on English
embedding similarity (semantic is held LOW at 0.2 here). Scope, evidence and
licence rules stay authoritative. Uses the local (gitignored) profile.yaml.

Run: python -m unittest discover -s tests -v
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

PROFILE_PATH = PROJECT_ROOT / "profile.yaml"


def _job(title, description="", location="Barcelona, CT, ES"):
    return Job(title=title, company="Acme", location=location,
               url=f"https://example.com/{title}", board=JobBoard.INDEED, description=description)


class LiveBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not PROFILE_PATH.exists():
            raise unittest.SkipTest("profile.yaml not present (local-only file)")
        with open(PROFILE_PATH, encoding="utf-8") as f:
            cls.profile = yaml.safe_load(f)

    def setUp(self):
        for p in (mock.patch.object(matcher.JobMatcher, "_semantic_score", return_value=0.2),
                  mock.patch.object(matcher, "load_life_story", return_value="")):
            p.start()
            self.addCleanup(p.stop)
        self.m = matcher.JobMatcher(self.profile)

    def score(self, title, description="", location="Barcelona, CT, ES"):
        return self.m.score(_job(title, description, location))


class TestIndeedSpainLocations(LiveBase):
    def test_province_and_region_codes_are_spain(self):
        for loc in ["Madrid, MD, ES", "Girona, GI, ES", "Lleida, L, ES", "Sevilla, SE, ES",
                    "Porriño, GA, ES", "CT, ES", "ES"]:
            self.assertEqual(self.m._location_score(_job("Chófer", "", loc)), 0.9, loc)

    def test_barcelona_still_top_priority(self):
        self.assertEqual(self.m._location_score(_job("Chófer", "", "Barcelona, CT, ES")), 1.0)

    def test_non_spain_codes_not_matched(self):
        for loc in ["Portland, OR, US", "Paris, France", "Lisbon, PT", "Essen, DE"]:
            self.assertEqual(self.m._location_score(_job("Chófer", "", loc)), 0.0, loc)


class TestStrongTitlesReachThreshold(LiveBase):
    WOMEN = "Design knitwear and jersey for our women's collection: sketches, fabrics and sampling."
    DRIVE = "Conducción de vehículo por Barcelona. Imprescindible permiso de conducir."
    VAN = "Reparto con furgoneta de empresa. Permiso de conducir."

    def assertReady(self, title, description, category, location="Barcelona, CT, ES"):
        s, d = self.score(title, description, location)
        msg = f"{title!r}: {s} {d}"
        self.assertEqual(d["category"], category, msg)
        self.assertEqual(d["rejected_reason"], "", msg)
        self.assertEqual(d["requirement_flags"], [], msg)
        self.assertGreaterEqual(s, 0.50, msg)
        self.assertEqual(d["title_score"], 1.0, msg)

    def test_threshold_is_exactly_050(self):
        self.assertEqual(self.profile["pipeline"]["auto_apply_threshold"], 0.50)

    def test_fashion_titles(self):
        for title in ["KNITWEAR DESIGNER - WOMAN", "JERSEY DESIGNER - WOMAN",
                      "Junior Designer Heavy Woven/Denim Women (m/f/d)",
                      "Junior Designer Light Woven Women (m/f/d)",
                      "Diseñador/a Prenda exterior y tailoring Women"]:
            self.assertReady(title, self.WOMEN, "fashion", "Madrid, MD, ES")

    def test_driver_titles(self):
        for title in ["Taxista", "CONDUCTOR VTC", "Conductor VTC Cabify", "Chófer",
                      "Conductor/a de taxi"]:
            self.assertReady(title, self.DRIVE, "driver", "Lleida, L, ES")
        self.assertReady("Conductor/a – Repartidor/a en furgoneta", self.VAN, "driver")

    def test_catalan_licence_counts_as_vehicle_evidence(self):
        s, d = self.score("PERSONAL REPARTIDOR", "Repartiment de material. Permisos de conduir: B.")
        self.assertEqual(d["rejected_reason"], "", d)
        self.assertEqual(d["category"], "driver")


class TestRulesStillAuthoritative(LiveBase):
    def assertRejected(self, title, description=""):
        s, d = self.score(title, description)
        self.assertEqual(s, 0.0, (title, d))
        self.assertTrue(d["rejected_reason"], (title, d))

    def assertCapped(self, title, description, flag):
        s, d = self.score(title, description)
        self.assertLessEqual(s, 0.30, (title, d))
        self.assertTrue(any(flag in f for f in d["requirement_flags"]), (title, d))

    def test_scope_rejections_kept(self):
        for title in ["Menswear Designer", "Kidswear Designer", "Footwear Designer", "Product Designer",
                      "Graphic Designer", "UX/UI Designer", "Accessories Designer",
                      "Software Engineer - Device Driver", "Linux Kernel Driver Developer"]:
            self.assertRejected(title, "Design / engineering role.")

    def test_bike_motorbike_titles_rejected_even_if_description_mentions_car(self):
        for title in ["SANT CUGAT - Repartidor/a BICI ELÉCTRICA/MOTO PROPIA - Food Delivery",
                      "Repartidor/a MOTO PROPIA", "Repartidor/a en Ciclomotor"]:
            self.assertRejected(title, "Reparto de comida. No necesitas coche.")

    def test_car_or_van_alongside_motorbike_kept(self):
        for title in ["Repartidor/a de Moto y furgoneta", "Repartidor/a con coche o moto propia"]:
            _, d = self.score(title, "Reparto a domicilio.")
            self.assertEqual(d["category"], "driver", title)
            self.assertEqual(d["rejected_reason"], "", title)

    def test_evidence_holds_kept(self):
        self.assertCapped("Driver", "", "No evidence this is a real driving role")
        self.assertCapped("Professional Driver", "Be a professional driver of change.",
                          "No evidence this is a real driving role")
        self.assertCapped("Fashion Designer", "", "Not clearly ladies/womenswear")

    def test_professional_licence_caps_kept_and_extended(self):
        for title, desc in [
            ("Chófer C+E", "Ruta nacional."),
            ("Conductor/a", "Imprescindible CAP y tarjeta de tacógrafo."),
            ("Conductor de autobús", "Líneas urbanas."),
            ("CONDUCTOR/A TRAILER", "Turno de tarde."),
            ("Conductor/Gruista de vehículos pesados", ""),
            ("Conductor/a Hormigonera 32t", ""),
            ("Conductor/a Portacoches", ""),
            ("MOZO/A CONDUCTOR/A CARNE C", ""),
            ("Conductor/a C – Ruta Tarragona", ""),
            ("Conductor/a", "Transporte de mercancías peligrosas con ADR en vigor."),
        ]:
            self.assertCapped(title, desc, "professional licence")

    def test_adr_pattern_does_not_hit_madrid(self):
        _, d = self.score("Chófer", "Conducción de vehículo por Madrid.", "Madrid, MD, ES")
        self.assertEqual(d["requirement_flags"], [], d)

    def test_spanish_eu_licence_cap_kept(self):
        self.assertCapped("Conductor Repartidor", "Imprescindible carnet de conducir español.",
                          "Spanish/EU")


if __name__ == "__main__":
    unittest.main()
