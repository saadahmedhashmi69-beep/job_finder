"""Regression: profile.yaml must be read/written as UTF-8 on every platform.

On Windows the default encoding is cp1252, which turned "español" into
"espaÃ±ol" and silently broke Spanish licence patterns and titles.

Run: python -m unittest discover -s tests -v
Uses a temporary profile.yaml; the real one is never touched.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import app as app_module  # noqa: E402
import main  # noqa: E402
import pipeline  # noqa: E402

ACCENTED_PROFILE = """\
categories:
  - name: driver
    titles:
      - chófer
      - conductor
    blocking_requirements:
      - reason: "Requires a Spanish/EU driving licence — CV lists Pakistan & Oman only"
        max_score: 0.30
        patterns:
          - carnet de conducir español
          - permiso de conducción español
search:
  queries:
    - "Diseñador de moda"
    - "Chófer"
"""


class TestProfileLoadsUtf8(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "profile.yaml"
        self.path.write_bytes(ACCENTED_PROFILE.encode("utf-8"))

    def _check(self, profile):
        driver = profile["categories"][0]
        self.assertIn("chófer", driver["titles"])
        req = driver["blocking_requirements"][0]
        self.assertIn("carnet de conducir español", req["patterns"])
        self.assertIn("permiso de conducción español", req["patterns"])
        self.assertIn("—", req["reason"])
        self.assertIn("Diseñador de moda", profile["search"]["queries"])
        # No mojibake anywhere.
        self.assertNotIn("Ã", repr(profile))

    def test_pipeline_load_profile(self):
        with mock.patch.object(pipeline, "CONFIG_PATH", self.path):
            self._check(pipeline.load_profile())

    def test_app_load_profile(self):
        with mock.patch.object(app_module, "CONFIG_PATH", self.path):
            self._check(app_module.load_profile())

    def test_main_load_profile(self):
        with mock.patch.object(main, "CONFIG_PATH", self.path):
            self._check(main.load_profile())

    def test_app_save_profile_round_trip(self):
        # Saving via the UI must keep the file UTF-8 so later loads still work.
        with mock.patch.object(app_module, "CONFIG_PATH", self.path), \
                mock.patch("submitter.demote_unverified_submissions"):  # never touch the real jobs.db
            client = app_module.create_app().test_client()
            resp = client.post("/api/profile/queries", json={"query": "Repartidor/a camión"})
            self.assertEqual(resp.status_code, 200)
            self.path.read_bytes().decode("utf-8")  # raises if not valid UTF-8
            profile = app_module.load_profile()
        self._check(profile)
        self.assertIn("Repartidor/a camión", profile["search"]["queries"])


if __name__ == "__main__":
    unittest.main()
