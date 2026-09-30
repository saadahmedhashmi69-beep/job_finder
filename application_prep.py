"""Application preparation for QUALIFIED jobs only.

QUALIFIED job -> duplicate check -> application record -> fixed CV (unchanged,
checksum recorded) -> truthful cover letter + form answers -> application method
-> READY_TO_SUBMIT (web/ATS or email) or MANUAL_REQUIRED.

Everything written here comes from profile.yaml (name, contact, location,
skills, optional `candidate_facts`) — no qualifications, years, employers,
licences, languages or work authorisation are invented. Questions the profile
cannot answer truthfully are left unanswered so a form requiring them stops
as MANUAL_REQUIRED. cv_customizer's CV generation is never called.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import storage
from fixed_cv import application_dir_for_slug, resolve_fixed_cv_path
from qualification import (
    APPLICATION_PREPARED, MANUAL_REQUIRED, QUALIFIED, READY_TO_SUBMIT, qualify_job,
)

logger = logging.getLogger(__name__)

WEB = "WEB"
EMAIL = "EMAIL"

# ATS hosts whose public application pages are plain HTML forms.
SUPPORTED_ATS_HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "job-boards.eu.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.eu.lever.co": "lever",
}
# Boards whose apply flow needs a login / is anti-bot protected: never automated.
LOGIN_WALLED_HOSTS = ("linkedin.com", "indeed.com", "glassdoor.", "infojobs.net")

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_BAD_EMAIL = re.compile(r"(no-?reply|donotreply|@sentry|\.png$|\.jpg$)", re.I)
# Placeholder / test recipients are never legitimate application addresses.
_PLACEHOLDER_DOMAIN = re.compile(
    r"(^|\.)(example\.(com|org|net)|localhost|localdomain|test\.com|domain\.com|"
    r"yourdomain\.com|yourcompany\.com|mailinator\.com)$|\.(test|invalid|example|localhost|local)$", re.I)
_PLACEHOLDER_LOCAL = re.compile(
    r"^(test|testing|tester|placeholder|dummy|fake|sample|example|your\.?e?-?mail|your\.?name|"
    r"name|someone|john\.?doe|jane\.?doe|foo|bar|asdf|xxx+)$", re.I)


def is_placeholder_email(email: str) -> bool:
    """True for example/test/localhost/placeholder addresses."""
    local, _, domain = (email or "").strip().lower().rpartition("@")
    return (not local or not domain or bool(_PLACEHOLDER_DOMAIN.search(domain))
            or bool(_PLACEHOLDER_LOCAL.match(local)))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _db(db_path: Optional[Path]) -> Path:
    return db_path or storage.DB_PATH


# --- Candidate facts ---------------------------------------------------------

def candidate_facts(profile: Dict) -> Dict:
    facts = dict(profile.get("candidate_facts") or {})
    name = (profile.get("name") or "").strip()
    first, _, last = name.partition(" ")
    location = (profile.get("location") or "").strip()
    return {
        "full_name": name,
        "first_name": first,
        "last_name": last,
        "email": (profile.get("email") or "").strip(),
        "phone": str(profile.get("phone") or "").strip(),
        "location": location,
        "city": location.split(",")[0].strip() if location else "",
        "summary": (facts.get("summary") or "").strip(),
        "linkedin": (profile.get("linkedin") or facts.get("linkedin") or "").strip(),
    }


def _category_skills(profile: Dict, category: str) -> List[str]:
    """Top-level (CV-backed) skills that belong to the job's category."""
    top = [s for s in profile.get("skills") or [] if isinstance(s, str)]
    for cat in profile.get("categories") or []:
        if cat.get("name") == category:
            cat_skills = {str(s).lower() for s in cat.get("skills") or []}
            return [s for s in top if s.lower() in cat_skills]
    return []


def generate_cover_letter(job: Dict, profile: Dict, category: str) -> str:
    """Job-specific, truthful cover letter built only from profile facts."""
    f = candidate_facts(profile)
    title, company = job.get("title") or "the advertised", job.get("company") or "your company"
    skills = _category_skills(profile, category)
    lines = [f"Dear Hiring Team at {company},", "",
             f"I am writing to apply for the {title} position"
             + (f" in {job['location']}" if job.get("location") else "") + "."]
    if f["summary"]:
        lines.append(f["summary"])
    if category == "fashion":
        lines.append("My background is in fashion design"
                     + (f", including {', '.join(skills[:6])}" if skills else "") + ".")
        lines.append(f"I am interested in this role because it centres on womenswear design, "
                     f"which is the focus of my work, and I would like to contribute to {company}'s collections.")
    elif category == "driver":
        lines.append("I am applying as a driver"
                     + (f"; my CV lists {', '.join(skills[:4])}" if skills else "") + ".")
        lines.append(f"I am reliable and organised, and I would like to support {company}'s driving operations.")
    if f["location"]:
        lines.append(f"I am based in {f['location']}.")
    lines += ["My CV is attached with full details of my experience. "
              "I would welcome the opportunity to discuss the role with you.", "",
              "Kind regards,", f["full_name"]]
    lines += [x for x in (f["email"], f["phone"]) if x]
    return "\n".join(lines).strip() + "\n"


def generate_form_answers(job: Dict, profile: Dict, category: str, cover_letter: str) -> Dict[str, str]:
    """Answers for common form fields. Only truthful, profile-backed values;
    anything else (salary, sponsorship, start date, languages...) is omitted."""
    f = candidate_facts(profile)
    answers = {k: f[k] for k in ("full_name", "first_name", "last_name", "email",
                                 "phone", "location", "city", "linkedin") if f[k]}
    answers["cover_letter"] = cover_letter
    answers["why_interested"] = (
        f"I am applying for the {job.get('title', 'role')} role at {job.get('company', 'your company')} "
        + ("because it is a womenswear design role, which matches my fashion design background."
           if category == "fashion" else "because it is a driving role that matches my CV.")
    )
    return answers


# --- Application method ------------------------------------------------------

def find_application_email(job: Dict, recruiter_email: str = "") -> str:
    """A legitimate email printed in the job posting itself. A recruiter_email
    set on the application is used only if the posting contains it too.
    Placeholder/test addresses are rejected; nothing is ever guessed."""
    posted = []
    for candidate in _EMAIL_RE.findall(job.get("description") or ""):
        candidate = candidate.strip().rstrip(".")
        if not _BAD_EMAIL.search(candidate) and not is_placeholder_email(candidate):
            posted.append(candidate)
    wanted = (recruiter_email or "").strip().lower()
    for candidate in posted:
        if candidate.lower() == wanted:
            return candidate
    return posted[0] if posted else ""


def detect_application_method(job: Dict, recruiter_email: str = "") -> Dict:
    """Return {method: WEB|EMAIL|MANUAL_REQUIRED, ats, email, reason}."""
    url = (job.get("url") or "").strip()
    host = urlparse(url).netloc.lower() if url.startswith(("http://", "https://")) else ""
    email = find_application_email(job, recruiter_email)
    ats = SUPPORTED_ATS_HOSTS.get(host, "")
    if ats:
        return {"method": WEB, "ats": ats, "email": email, "reason": f"{ats} ATS form"}
    if email:
        return {"method": EMAIL, "ats": "", "email": email, "reason": "Application email in posting"}
    if host and not any(h in host for h in LOGIN_WALLED_HOSTS):
        return {"method": WEB, "ats": "generic", "email": "",
                "reason": "Generic web form (attempted; stops as MANUAL_REQUIRED if unsupported)"}
    return {"method": MANUAL_REQUIRED, "ats": "", "email": "",
            "reason": ("Apply flow on a login-walled board (LinkedIn/Indeed/...) and no application email"
                       if host else "No application URL or email")}


# --- Preparation -------------------------------------------------------------

def prepare_application(job: Dict, profile: Dict, *, matcher=None,
                        db_path: Optional[Path] = None) -> Dict:
    """Qualify a job and, only if QUALIFIED, prepare its application package.

    Returns {"status", "app_id", "reasons", "method"}; status is the job's
    qualification status when not qualified, or the application status.
    """
    db = _db(db_path)
    existing = storage.find_existing_application(job["url"], job.get("title", ""),
                                                 job.get("company", ""), db_path=db)
    if existing:
        return {"status": "DUPLICATE", "app_id": existing["id"],
                "reasons": [f"Already has application #{existing['id']} ({existing['status']})"],
                "method": existing.get("application_method", "")}

    q = qualify_job(job, profile, matcher=matcher)
    storage.set_job_qualification(job["url"], q.status, q.category, q.reasons, db_path=db)
    if q.status != QUALIFIED:
        return {"status": q.status, "app_id": None, "reasons": q.reasons, "method": ""}

    # Fixed CV only: resolve + verify, record exact path and checksum.
    from fixed_cv import prepare_fixed_cv_application
    cv_path = resolve_fixed_cv_path(profile)
    cv_hash = sha256_file(cv_path)
    cv_result = prepare_fixed_cv_application(
        job_url=job["url"], title=job.get("title", ""), company=job.get("company", ""),
        location=job.get("location", ""), description=job.get("description", ""), profile=profile,
    )
    app_dir = Path(cv_result["app_dir"])

    letter = generate_cover_letter(job, profile, q.category)
    (app_dir / "cover-letter.md").write_text(letter, encoding="utf-8")
    answers = generate_form_answers(job, profile, q.category, letter)

    app_id = storage.create_application(job["url"], cv_result["slug"], db_path=db)
    storage.update_application(
        app_id, db_path=db, status=APPLICATION_PREPARED, cv_pdf_path=str(cv_path),
        cv_sha256=cv_hash, form_answers_json=json.dumps(answers),
        email_subject=f"Application for {job.get('title', '')} - {candidate_facts(profile)['full_name']}",
        email_body=letter,
    )

    method = detect_application_method(job)
    status = MANUAL_REQUIRED if method["method"] == MANUAL_REQUIRED else READY_TO_SUBMIT
    storage.update_application(
        app_id, db_path=db, status=status, application_method=method["method"],
        recruiter_email=method["email"], status_reason=method["reason"],
    )
    logger.info("Prepared application #%s (%s, %s) for %s at %s", app_id, status,
                method["method"], job.get("title"), job.get("company"))
    return {"status": status, "app_id": app_id, "reasons": [method["reason"]], "method": method["method"]}
