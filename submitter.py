"""Submission of READY_TO_SUBMIT applications (web/ATS form or email).

Modes:
  DRY_RUN (default) — open the form, fill fields, upload the fixed CV, validate
                      required fields; NEVER clicks submit / sends email.
  LIVE              — only when profile.yaml has BOTH
                        pipeline.submission_mode: LIVE
                        pipeline.allow_live_submission: true
                      Submits, then marks SUBMITTED only on observable
                      confirmation (confirmation text, application id, or a
                      known success redirect). Otherwise SUBMISSION_FAILED.

The browser flow handles plain HTML forms (Greenhouse/Lever-style). CAPTCHA,
login walls, missing CV upload fields, unknown required questions and anything
else it cannot complete truthfully stop as MANUAL_REQUIRED. It never tries to
bypass CAPTCHA, anti-bot protection or authentication.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

import storage
from application_prep import EMAIL, WEB, find_application_email, sha256_file
from fixed_cv import resolve_fixed_cv_path
from qualification import (
    MANUAL_REQUIRED, QUALIFIED, READY_TO_SUBMIT, SMTP_ACCEPTED, SUBMISSION_FAILED, SUBMITTED,
    UNVERIFIED_LEGACY,
)

logger = logging.getLogger(__name__)

DRY_RUN = "DRY_RUN"
LIVE = "LIVE"
DRY_RUN_VALIDATED = "DRY_RUN_VALIDATED"


def get_submission_mode(profile: Optional[Dict]) -> str:
    cfg = (profile or {}).get("pipeline", {}) or {}
    if str(cfg.get("submission_mode", DRY_RUN)).upper() == LIVE and cfg.get("allow_live_submission") is True:
        return LIVE
    return DRY_RUN


# --- Page inspection ---------------------------------------------------------

_CAPTCHA = re.compile(r"g-recaptcha|recaptcha/api|hcaptcha|cf-turnstile|captcha", re.I)
_LOGIN_URL = re.compile(r"/(login|signin|sign-in|authwall|auth|account/login)\b", re.I)
_CONFIRM_TEXT = re.compile(
    r"thank you for (applying|your application)|application (has been )?(received|submitted)|"
    r"we have received your application|gracias por (tu|su) (candidatura|solicitud|inter[eé]s)|"
    r"(candidatura|solicitud) (enviada|recibida)|hemos recibido tu (candidatura|solicitud)", re.I)
_CONFIRM_URL = re.compile(r"/(thanks|thank-you|thank_you|confirmation|confirm|success|submitted)\b", re.I)
_APPLICATION_ID = re.compile(r"(application|candidatura|solicitud) (id|number|n[uú]mero|ref(erence)?)\s*[:#]?\s*[A-Z0-9-]{4,}", re.I)

# JS: describe every visible form control and tag it with data-jf-idx.
_FIELDS_JS = r"""
() => Array.from(document.querySelectorAll('input, textarea, select')).map((el, i) => {
  el.setAttribute('data-jf-idx', String(i));
  const lab = (el.id && document.querySelector(`label[for="${el.id}"]`)) || el.closest('label');
  return {
    idx: i, tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(),
    name: el.name || '', id: el.id || '', placeholder: el.placeholder || '',
    aria: el.getAttribute('aria-label') || '', label: lab ? lab.innerText : '',
    required: el.required || el.getAttribute('aria-required') === 'true',
    value: el.value || '', hidden: el.type === 'hidden' || el.offsetParent === null,
  };
})
"""

# (answer key, regex over the field's name/id/label/placeholder text)
_FIELD_MAP = [
    ("first_name", r"first.?name|given.?name|nombre(?!.*apellido)$|^nombre\b"),
    ("last_name", r"last.?name|surname|family.?name|apellido"),
    ("full_name", r"full.?name|^name$|your name|nombre completo"),
    ("email", r"e-?mail|correo"),
    ("phone", r"phone|mobile|tel[eé]fono|m[oó]vil"),
    ("city", r"\bcity\b|ciudad|localidad"),
    ("location", r"location|address|ubicaci[oó]n|direcci[oó]n"),
    ("linkedin", r"linkedin"),
    ("cover_letter", r"cover.?letter|carta de presentaci[oó]n|motivation"),
    ("why_interested", r"why .*(interested|apply|join)|por qu[eé]"),
]


def _field_text(f: Dict) -> str:
    return " ".join(str(f.get(k, "")) for k in ("name", "id", "label", "placeholder", "aria")).lower().strip()


def _selector(f: Dict) -> str:
    return f'[data-jf-idx="{f["idx"]}"]'


def fill_application_form(page, url: str, answers: Dict[str, str], cv_path: Path,
                          mode: str = DRY_RUN) -> Dict:
    """Drive one application form on a Playwright-like `page`.

    Returns {"status", "reason", "filled", "missing_required", "evidence"}.
    """
    result = {"status": MANUAL_REQUIRED, "reason": "", "filled": [], "missing_required": [], "evidence": ""}
    page.goto(url)
    html = page.content() or ""
    if _CAPTCHA.search(html):
        result["reason"] = "CAPTCHA detected - manual application required"
        return result
    if _LOGIN_URL.search(page.url or "") or re.search(r'type=["\']password', html, re.I):
        result["reason"] = "Login required - manual application required"
        return result

    fields = [f for f in (page.evaluate(_FIELDS_JS) or []) if not f.get("hidden") or f.get("type") == "file"]
    if not fields:
        result["reason"] = "No application form found on page"
        return result

    cv_uploaded = False
    for f in fields:
        text = _field_text(f)
        if f["type"] == "file":
            if re.search(r"resume|cv|curr[ií]cul", text) or not cv_uploaded:
                if re.search(r"cover", text):
                    continue  # cover letter goes into its text field, not as a file
                page.set_input_files(_selector(f), str(cv_path))
                cv_uploaded = True
                result["filled"].append("cv_upload")
            continue
        if f["type"] in ("submit", "button", "checkbox", "radio", "hidden") or f["tag"] == "select":
            continue
        key = next((k for k, rx in _FIELD_MAP if re.search(rx, text)), None)
        value = answers.get(key) if key else None
        if key == "cover_letter" and f["tag"] != "textarea" and f["type"] not in ("text", ""):
            value = None
        if value:
            page.fill(_selector(f), str(value))
            f["value"] = str(value)
            result["filled"].append(key)

    for f in fields:
        if f.get("required") and not f.get("value") and f["type"] != "file" and f["type"] not in ("submit", "button"):
            result["missing_required"].append(_field_text(f) or f"field #{f['idx']}")
        if f.get("required") and f["type"] == "file" and not cv_uploaded:
            result["missing_required"].append("cv upload")
    if not cv_uploaded:
        result["reason"] = "No CV upload field found - manual application required"
        return result
    if result["missing_required"]:
        result["reason"] = ("Required fields without a truthful profile answer: "
                            + "; ".join(result["missing_required"][:8]))
        return result

    if mode != LIVE:
        result["status"] = DRY_RUN_VALIDATED
        result["reason"] = "DRY_RUN: form filled and CV uploaded; submit NOT clicked"
        return result

    submit = page.query_selector('button[type="submit"], input[type="submit"]')
    if not submit:
        result["reason"] = "No submit button found - manual application required"
        return result
    submit.click()
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    return {**result, **verify_submission(page)}


def verify_submission(page) -> Dict:
    """SUBMITTED only with observable confirmation; otherwise SUBMISSION_FAILED."""
    html = page.content() or ""
    text = re.sub(r"<[^>]+>", " ", html)
    if _CAPTCHA.search(html):
        return {"status": MANUAL_REQUIRED, "reason": "CAPTCHA shown after submit", "evidence": ""}
    for rx, label in ((_CONFIRM_TEXT, "confirmation text"), (_APPLICATION_ID, "application id")):
        m = rx.search(text)
        if m:
            return {"status": SUBMITTED, "reason": f"Confirmed by {label}", "evidence": m.group(0)[:200]}
    if _CONFIRM_URL.search(page.url or ""):
        return {"status": SUBMITTED, "reason": "Confirmed by success redirect", "evidence": page.url}
    return {"status": SUBMISSION_FAILED, "reason": "Submit clicked but no confirmation observed", "evidence": ""}


def _open_browser_page():
    """Return (page, close_fn) using Playwright, or raise ImportError."""
    from playwright.sync_api import sync_playwright  # optional dependency
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()

    def close():
        browser.close()
        pw.stop()
    return page, close


# --- Orchestration -----------------------------------------------------------

def submit_application(app_id: int, profile: Dict, *, mode: Optional[str] = None,
                       page_factory: Optional[Callable] = None,
                       email_sender: Optional[Callable] = None,
                       db_path: Optional[Path] = None) -> Dict:
    """Run the web or email route for one READY_TO_SUBMIT application."""
    db = db_path or storage.DB_PATH
    mode = mode if mode in (DRY_RUN, LIVE) else get_submission_mode(profile)
    if mode == LIVE and get_submission_mode(profile) != LIVE:
        mode = DRY_RUN  # LIVE must also be enabled in configuration
    conn = storage.get_db(db)
    row = conn.execute(
        """SELECT a.*, j.title, j.company, j.location, j.description, j.url
           FROM applications a JOIN jobs j ON a.job_url = j.url WHERE a.id = ?""", (app_id,)).fetchone()
    conn.close()
    if not row:
        return {"status": "error", "reason": "Application not found"}
    app = dict(row)
    if app["status"] == SUBMITTED or app.get("submitted_at"):
        return {"status": SUBMITTED, "reason": "Already submitted - duplicate submission blocked"}
    if app["status"] != READY_TO_SUBMIT:
        return {"status": app["status"], "reason": f"Not READY_TO_SUBMIT (status {app['status']})"}

    cv_path = resolve_fixed_cv_path(profile)
    if Path(app["cv_pdf_path"]).resolve() != cv_path or (
            app.get("cv_sha256") and sha256_file(cv_path) != app["cv_sha256"]):
        return _record(app_id, db, mode, MANUAL_REQUIRED, "Fixed CV path/checksum mismatch")
    answers = json.loads(app.get("form_answers_json") or "{}")

    if app.get("application_method") == WEB:
        try:
            page, close = (page_factory() if page_factory else _open_browser_page())
        except ImportError:
            return _record(app_id, db, mode, MANUAL_REQUIRED,
                           "Playwright not installed (pip install playwright; playwright install chromium)")
        try:
            res = fill_application_form(page, app["url"], answers, cv_path, mode=mode)
        except Exception as e:
            res = {"status": MANUAL_REQUIRED, "reason": f"Browser automation stopped: {e}", "evidence": ""}
        finally:
            close()
        return _record(app_id, db, mode, res["status"], res["reason"], res.get("evidence", ""))

    if app.get("application_method") == EMAIL:
        to = find_application_email(app, app.get("recruiter_email") or "")
        if not to:
            return _record(app_id, db, mode, MANUAL_REQUIRED, "No legitimate application email")
        if mode != LIVE:
            return _record(app_id, db, mode, DRY_RUN_VALIDATED,
                           f"DRY_RUN: email to {to} prepared with fixed CV; NOT sent")
        from applier import send_application_email
        sender = email_sender or send_application_email
        ok = sender(to_email=to, subject=app.get("email_subject") or f"Application for {app['title']}",
                    body=app.get("email_body") or "", cv_path=cv_path, cover_letter_path=None)
        if ok:
            # SMTP acceptance is not proof that an application was received.
            return _record(app_id, db, mode, SMTP_ACCEPTED,
                           f"Email accepted by SMTP server for {to}; not a verified submission")
        return _record(app_id, db, mode, SUBMISSION_FAILED, "Email sending failed")

    return _record(app_id, db, mode, MANUAL_REQUIRED, "No supported application method")


def _record(app_id: int, db: Path, mode: str, outcome: str, reason: str, evidence: str = "") -> Dict:
    """Persist the outcome. DRY_RUN keeps the application READY_TO_SUBMIT."""
    fields = {"submission_mode": mode, "submission_status": outcome, "status_reason": reason,
              "submission_evidence": evidence}
    if outcome != DRY_RUN_VALIDATED:
        fields["status"] = outcome
    if outcome == SUBMITTED:
        fields["submitted_at"] = datetime.now().isoformat()
    if outcome == SMTP_ACCEPTED:
        fields["sent_at"] = datetime.now().isoformat()
    storage.update_application(app_id, db_path=db, **fields)
    if outcome == SUBMITTED:
        conn = storage.get_db(db)
        conn.execute("UPDATE jobs SET applied = 1 WHERE url = (SELECT job_url FROM applications WHERE id = ?)",
                     (app_id,))
        conn.commit()
        conn.close()
    return {"status": outcome, "reason": reason, "evidence": evidence, "mode": mode}


# --- Verified-submission contract ---------------------------------------------

def is_verified_submission(app: Dict) -> bool:
    """A genuine SUBMITTED application under the current contract: QUALIFIED job,
    fixed-CV checksum, WEB method and observable confirmation evidence.
    EMAIL can never be verified (SMTP acceptance only)."""
    return (app.get("status") == SUBMITTED and app.get("submission_status") == SUBMITTED
            and bool(app.get("submission_evidence")) and bool(app.get("cv_sha256"))
            and app.get("application_method") == WEB
            and app.get("qualification_status") == QUALIFIED)


def demote_unverified_submissions(db_path: Optional[Path] = None) -> int:
    """Reclassify rows that claim SUBMITTED but fail the contract above.

    Legacy/test rows become UNVERIFIED_LEGACY; current-workflow EMAIL rows become
    SMTP_ACCEPTED. Their job is no longer flagged applied. No evidence is invented.
    Returns the number of rows reclassified.
    """
    db = db_path or storage.DB_PATH
    conn = storage.get_db(db)
    rows = [dict(r) for r in conn.execute(
        """SELECT a.*, j.qualification_status FROM applications a LEFT JOIN jobs j ON a.job_url = j.url
           WHERE a.status = ? OR a.submission_status = ?""", (SUBMITTED, SUBMITTED))]
    conn.close()
    demoted = 0
    for app in rows:
        if is_verified_submission(app):
            continue
        current = (app.get("application_method") == EMAIL and app.get("cv_sha256")
                   and app.get("qualification_status") == QUALIFIED)
        state = SMTP_ACCEPTED if current else UNVERIFIED_LEGACY
        storage.update_application(
            app["id"], db_path=db, status=state, submission_status=state, submitted_at="",
            status_reason=f"{state}: not a verified submission (was: {app.get('status_reason') or 'no reason'})")
        conn = storage.get_db(db)
        conn.execute("UPDATE jobs SET applied = 0 WHERE url = ?", (app["job_url"],))
        conn.commit()
        conn.close()
        demoted += 1
        logger.warning("Application #%s reclassified %s (no verified submission)", app["id"], state)
    return demoted
