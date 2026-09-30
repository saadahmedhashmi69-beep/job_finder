"""Fixed CV attachment.

Every application (fashion design, driving, or anything else) attaches the same
pre-made CV PDF configured at `pipeline.fixed_cv_path` in profile.yaml. The CV is
never generated, customized, compiled or picked from an application directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

import yaml

_PROJECT_ROOT = Path(__file__).parent
CONFIG_PATH = _PROJECT_ROOT / "profile.yaml"
DEFAULT_FIXED_CV_PATH = "cv/Raheel Tahir Resume Updated.pdf"


class FixedCVMissingError(FileNotFoundError):
    """Raised when the configured fixed CV PDF does not exist."""


def _load_profile() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_fixed_cv_path(profile: Optional[dict] = None) -> Path:
    """Return the absolute path of the fixed CV PDF, or raise if it is missing."""
    if profile is None:
        profile = _load_profile()
    raw = (profile or {}).get("pipeline", {}).get("fixed_cv_path") or DEFAULT_FIXED_CV_PATH
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    path = path.resolve()
    if not path.is_file():
        raise FixedCVMissingError(
            f"Fixed CV PDF not found: {path}. "
            "Set pipeline.fixed_cv_path in profile.yaml to the candidate's CV PDF."
        )
    return path


def application_dir_for_slug(slug: str, profile: Optional[dict] = None) -> Path:
    """Directory holding per-application generated files (cover letter, JD)."""
    from cv_customizer import resolve_cv_dir
    return resolve_cv_dir(profile) / "applications" / slug


def prepare_fixed_cv_application(
    *,
    job_url: str,
    title: str,
    company: str,
    location: str,
    description: str,
    profile: Optional[dict] = None,
) -> Dict:
    """Create the application directory and return slug, cv_pdf_path, app_dir.

    Drop-in replacement for customize_cv_for_job()'s result, but the CV path is
    always the fixed PDF. Raises FixedCVMissingError if the PDF is missing.
    """
    from fingerprint import unique_slug

    cv_path = resolve_fixed_cv_path(profile)

    # The slug ends in a digest of the posting URL: two vacancies can never share
    # (and overwrite) one application directory, however similar their names.
    slug = unique_slug(company, title, job_url)
    app_dir = application_dir_for_slug(slug, profile)
    app_dir.mkdir(parents=True, exist_ok=True)

    jd_content = f"# {title} at {company}\n\n**Location:** {location}\n**URL:** {job_url}\n\n---\n\n{description}"
    (app_dir / "job-description.md").write_text(jd_content, encoding="utf-8")

    return {
        "slug": slug,
        "cv_pdf_path": str(cv_path),
        "app_dir": str(app_dir),
    }
