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

_CAPTCHA = re.compile(r"g-recaptcha|recaptcha/api|hcaptcha|cf-turnstile|captcha|challenges\.cloudflare\.com", re.I)
_LOGIN_URL = re.compile(r"/(login|signin|sign-in|authwall|auth|account/login|uas/login)\b", re.I)
_LOGIN_TEXT = re.compile(
    r"sign in to apply|log ?in to apply|create an account to apply|sign in to continue|"
    r"inicia sesi[oó]n para (aplicar|inscribirte|postular|continuar)|reg[ií]strate para (aplicar|inscribirte)", re.I)
_CONFIRM_TEXT = re.compile(
    r"thank you for (applying|your application)|application (has been )?(received|submitted)|"
    r"we have received your application|gracias por (tu|su) (candidatura|solicitud|inter[eé]s)|"
    r"(candidatura|solicitud) (enviada|recibida)|hemos recibido tu (candidatura|solicitud)", re.I)
_CONFIRM_URL = re.compile(r"/(thanks|thank-you|thank_you|confirmation|confirm|success|submitted)\b", re.I)
_APPLICATION_ID = re.compile(r"(application|candidatura|solicitud) (id|number|n[uú]mero|ref(erence)?)\s*[:#]?\s*[A-Z0-9-]{4,}", re.I)

# JS: describe every form control and tag it (and its form) with data-jf-* ids.
_FIELDS_JS = r"""
() => {
  Array.from(document.forms).forEach((f, i) => f.setAttribute('data-jf-form', String(i)));
  return Array.from(document.querySelectorAll('input, textarea, select')).map((el, i) => {
    el.setAttribute('data-jf-idx', String(i));
    const lab = (el.id && document.querySelector(`label[for="${el.id}"]`)) || el.closest('label');
    const label = lab ? lab.innerText : '';
    return {
      idx: i, tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(),
      name: el.name || '', id: el.id || '', placeholder: el.placeholder || '',
      aria: el.getAttribute('aria-label') || '', label: label,
      role: el.getAttribute('role') || '',
      required: el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label.trim()),
      value: el.value || '', checked: !!el.checked,
      options: el.tagName === 'SELECT' ? Array.from(el.options).map(o => o.text.trim()) : [],
      form: el.form ? Number(el.form.getAttribute('data-jf-form')) : -1,
      hidden: el.type === 'hidden' || (el.offsetParent === null && el.type !== 'file'),
    };
  });
}
"""
# JS: links on the page (to follow an "Apply" link to the actual form).
_LINKS_JS = r"""
() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
  href: a.href, text: (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 80)}))
"""
_APPLY_LINK_TEXT = re.compile(
    r"^\s*(apply|apply now|apply for this (job|position|role)|apply here|aplicar|aplica ya|solicitar|"
    r"inscr[ií]bete|inscribirme|postular(me)?|enviar (mi )?candidatura|candid[aá]tate|quiero aplicar)\b", re.I)

# (answer key, regex over the field's name/id/label/placeholder text)
_FIELD_MAP = [
    ("first_name", r"first.?name|given.?name|nombre(?!.*apellido)$|^nombre\b"),
    ("last_name", r"last.?name|surname|family.?name|apellido"),
    ("full_name", r"full.?name|^name$|your name|nombre completo"),
    ("email", r"e-?mail|correo"),
    ("phone", r"phone|mobile|tel[eé]fono|m[oó]vil"),
    ("city", r"\bcity\b|ciudad|localidad"),
    ("country", r"\bcountry\b|pa[ií]s"),
    ("location", r"location|ubicaci[oó]n|where are you based"),
    ("linkedin", r"linkedin"),
    ("cover_letter", r"cover.?letter|carta de presentaci[oó]n|motivation"),
    ("why_interested", r"why .*(interested|apply|join)|por qu[eé]"),
]
# Fields about someone/something else (a referee, an employer...) are never
# answered with the candidate's own details.
_NOT_ABOUT_CANDIDATE = re.compile(
    r"company|empresa|employer|referen|referee|emergency|recruiter|hiring manager|school|"
    r"universi|colegio|salary|salario|sueldo|visa|permit|permiso|sponsor|notice|start date|"
    r"incorporaci|disponibilidad|years|a[nñ]os|gender|g[eé]nero|ethnic|race|disab|veteran", re.I)
_SPAIN_NAMES = {"spain", "españa", "espana"}
_TEXT_TYPES = ("text", "email", "tel", "url", "")
_CLICKABLE = ("submit", "button", "reset", "image")


def _field_text(f: Dict) -> str:
    return " ".join(str(f.get(k, "")) for k in ("name", "id", "label", "placeholder", "aria")).lower().strip()


def _field_label(f: Dict) -> str:
    for k in ("label", "aria", "placeholder", "name", "id"):
        v = re.sub(r"\s+", " ", str(f.get(k) or "")).strip()
        if v:
            return v[:80]
    return f"field #{f['idx']}"


def _selector(f: Dict) -> str:
    return f'[data-jf-idx="{f["idx"]}"]'


def _answer_key(f: Dict) -> Optional[str]:
    text = _field_text(f)
    if _NOT_ABOUT_CANDIDATE.search(text):
        return None
    return next((k for k, rx in _FIELD_MAP if re.search(rx, text)), None)


def _blocked_reason(page) -> str:
    """CAPTCHA / login wall on the current page ('' when the page is public)."""
    html = page.content() or ""
    if _CAPTCHA.search(html):
        return "CAPTCHA detected - manual application required (never bypassed)"
    if (_LOGIN_URL.search(page.url or "") or re.search(r'type=["\']?password', html, re.I)
            or _LOGIN_TEXT.search(re.sub(r"<[^>]+>", " ", html))):
        return "Login required - manual application required (authentication never bypassed)"
    return ""


def _visible_fields(page) -> List[Dict]:
    return [f for f in (page.evaluate(_FIELDS_JS) or [])
            if isinstance(f, dict) and "idx" in f and f.get("type") not in _CLICKABLE
            and (not f.get("hidden") or f.get("type") == "file")]


def _apply_link(page, visited: List[str]) -> str:
    """A public 'Apply' link on the page (never a login-walled board or login page)."""
    from application_prep import is_login_walled
    for link in page.evaluate(_LINKS_JS) or []:
        if not isinstance(link, dict):
            continue
        href = link.get("href") or ""
        if (not href.startswith(("http://", "https://")) or href in visited or is_login_walled(href)
                or _LOGIN_URL.search(href)):
            continue
        if _APPLY_LINK_TEXT.search(link.get("text") or "") or re.search(r"/apply/?(\?|$)|/application/?(\?|$)", href):
            return href
    return ""


def _goto(page, url: str):
    page.goto(url)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass


def fill_application_form(page, url: str, answers: Dict[str, str], cv_path: Path,
                          mode: str = DRY_RUN, cv_sha256: str = "") -> Dict:
    """Drive one public application form on a Playwright-like `page`.

    Opens `url` (following at most two public "Apply" links to reach the form),
    stops on CAPTCHA/login walls, uploads the fixed CV, fills only fields that
    map to truthful profile answers and reports everything else. The final
    submit button is clicked only in LIVE mode, after re-checking the CV hash.

    Returns {"status", "reason", "filled", "missing_required", "evidence", "report"}.
    """
    report = {"route_url": url, "final_url": "", "navigated": [], "fields_detected": [],
              "fields_prepared": {}, "fields_manual": [], "would_submit": {}, "cv_uploaded": ""}
    result = {"status": MANUAL_REQUIRED, "reason": "", "filled": [], "missing_required": [],
              "evidence": "", "report": report}

    _goto(page, url)
    fields: List[Dict] = []
    for hop in range(3):
        blocked = _blocked_reason(page)
        if blocked:
            result["reason"] = blocked
            report["final_url"] = page.url
            return result
        fields = _visible_fields(page)
        if any(f["type"] == "file" for f in fields) or hop == 2:
            break
        link = _apply_link(page, [url] + report["navigated"])
        if not link:
            break
        report["navigated"].append(link)
        _goto(page, link)
    report["final_url"] = page.url

    if not fields:
        result["reason"] = "No application form found on page"
        return result

    # Restrict to the form holding the CV upload (not a newsletter/search form).
    cv_field = next((f for f in fields if f["type"] == "file" and re.search(r"resume|cv|curr[ií]cul", _field_text(f))),
                    next((f for f in fields if f["type"] == "file" and not re.search(r"cover", _field_text(f))), None))
    form_id = cv_field.get("form", -1) if cv_field else -1
    if form_id not in (-1, None):
        fields = [f for f in fields if f.get("form", -1) == form_id]

    manual, seen_groups = report["fields_manual"], set()
    for f in fields:
        label = _field_label(f)
        report["fields_detected"].append(label)
        ftype, required = f["type"], bool(f.get("required"))
        if f is cv_field:
            page.set_input_files(_selector(f), str(cv_path))
            report["cv_uploaded"] = str(cv_path)
            report["fields_prepared"][label] = "cv_upload"
            report["would_submit"][label] = Path(cv_path).name
            result["filled"].append("cv_upload")
            continue
        if ftype in ("checkbox", "radio"):
            # Consents, yes/no questions, demographics: never answered automatically.
            group = f.get("name") or label
            if required and not f.get("checked") and group not in seen_groups:
                manual.append(label)
            seen_groups.add(group)
            continue
        key = _answer_key(f)
        value = answers.get(key) if key else None
        if ftype == "file" or f.get("role") == "combobox":
            value = None  # other uploads / custom widgets: left for a human
        elif f["tag"] == "select":
            if value:
                wanted = {str(value).lower()}
                if wanted & _SPAIN_NAMES:
                    wanted |= _SPAIN_NAMES
                value = next((o for o in f.get("options") or [] if o.lower() in wanted), None)
                if value:
                    page.select_option(_selector(f), label=value)
        elif ftype not in _TEXT_TYPES and f["tag"] != "textarea":
            value = None  # number/date/etc.: nothing in the profile answers them truthfully
        elif key == "cover_letter" and f["tag"] != "textarea" and ftype != "text":
            value = None
        elif value:
            page.fill(_selector(f), str(value))
        if value:
            f["value"] = str(value)
            report["fields_prepared"][label] = key
            report["would_submit"][label] = str(value)[:160]
            result["filled"].append(key)
        elif required and not f.get("value"):
            manual.append(label)

    result["missing_required"] = list(manual)
    if not cv_field:
        result["reason"] = "No CV upload field found - manual application required"
        return result
    if manual:
        result["reason"] = "Required fields without a truthful profile answer: " + "; ".join(manual[:8])
        return result

    if mode != LIVE:
        result["status"] = DRY_RUN_VALIDATED
        result["reason"] = (f"DRY_RUN: {len(report['fields_prepared'])} fields filled incl. fixed CV; "
                            f"final submit NOT clicked")
        return result

    if cv_sha256 and sha256_file(cv_path) != cv_sha256:
        result["reason"] = "Fixed CV checksum changed before submit - not submitted"
        return result
    scope = f'[data-jf-form="{form_id}"] ' if form_id not in (-1, None) else ""
    submit = page.query_selector(f'{scope}button[type="submit"], {scope}input[type="submit"]')
    if not submit:
        result["reason"] = "No submit button found - manual application required"
        return result
    before_html, before_url = page.content() or "", page.url
    submit.click()
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    report["final_url"] = page.url
    return {**result, **verify_submission(page, before_html=before_html, before_url=before_url)}


def verify_submission(page, before_html: str = "", before_url: str = "") -> Dict:
    """SUBMITTED only with observable confirmation that was not on the page
    before submitting; otherwise SUBMISSION_FAILED (or MANUAL_REQUIRED)."""
    html = page.content() or ""
    text = re.sub(r"<[^>]+>", " ", html)
    before_text = re.sub(r"<[^>]+>", " ", before_html or "")
    if _CAPTCHA.search(html) and not _CAPTCHA.search(before_html or ""):
        return {"status": MANUAL_REQUIRED, "reason": "CAPTCHA shown after submit", "evidence": ""}
    for rx, label in ((_CONFIRM_TEXT, "confirmation text"), (_APPLICATION_ID, "application id")):
        m = rx.search(text)
        if m and not rx.search(before_text):
            return {"status": SUBMITTED, "reason": f"Confirmed by {label}", "evidence": m.group(0)[:200]}
    if (page.url or "") != (before_url or "") and _CONFIRM_URL.search(page.url or ""):
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

    # Fixed CV only: exact configured file, SHA-256 must match the one recorded at preparation.
    cv_path = resolve_fixed_cv_path(profile)
    if (Path(app["cv_pdf_path"] or "").resolve() != cv_path or not app.get("cv_sha256")
            or sha256_file(cv_path) != app["cv_sha256"]):
        return _record(app_id, db, mode, MANUAL_REQUIRED, "Fixed CV path/checksum mismatch")
    answers = json.loads(app.get("form_answers_json") or "{}")

    if app.get("application_method") == WEB:
        from application_prep import is_login_walled
        route_url = app.get("application_url") or app["url"]
        report = {"route_url": route_url, "route_type": app.get("route_type") or ""}
        if is_login_walled(route_url):
            return _record(app_id, db, mode, MANUAL_REQUIRED,
                           "Application route is a login-walled job board - not automated", report=report)
        try:
            page, close = (page_factory() if page_factory else _open_browser_page())
        except ImportError:
            return _record(app_id, db, mode, MANUAL_REQUIRED,
                           "Playwright not installed (pip install playwright; playwright install chromium)",
                           report=report)
        try:
            res = fill_application_form(page, route_url, answers, cv_path, mode=mode,
                                        cv_sha256=app["cv_sha256"])
        except Exception as e:
            res = {"status": MANUAL_REQUIRED, "reason": f"Browser automation stopped: {e}", "evidence": ""}
        finally:
            close()
        report.update(res.get("report") or {})
        return _record(app_id, db, mode, res["status"], res["reason"], res.get("evidence", ""), report=report)

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


def _record(app_id: int, db: Path, mode: str, outcome: str, reason: str, evidence: str = "",
            report: Optional[Dict] = None) -> Dict:
    """Persist the outcome. DRY_RUN keeps the application READY_TO_SUBMIT."""
    if outcome == SUBMITTED and not evidence:
        outcome, reason = SUBMISSION_FAILED, f"{reason} (no observable evidence)"
    fields = {"submission_mode": mode, "submission_status": outcome, "status_reason": reason,
              "submission_evidence": evidence}
    if report is not None:
        fields["submission_report_json"] = json.dumps(report)
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
    return {"status": outcome, "reason": reason, "evidence": evidence, "mode": mode, "report": report or {}}


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
