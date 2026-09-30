"""Submission of READY_TO_SUBMIT applications (web/ATS form or email).

Submission is always an explicit action (`python main.py submit --app-id N`,
the UI submit button). The pipeline and the daemon never submit: they run
inside safety.no_live_sends(), where LIVE is forced down to DRY_RUN.

Modes:
  DRY_RUN (default) — inspect only. The form is opened and read, fields are
                      mapped to profile answers and reported; NOTHING is typed,
                      selected or uploaded, no submit is clicked, no email sent.
  LIVE              — only when profile.yaml has BOTH
                        pipeline.submission_mode: LIVE
                        pipeline.allow_live_submission: true
                      The application is first claimed atomically
                      (storage.claim_application): exactly one worker may
                      submit it, and never if any earlier send/submission
                      evidence exists. SUBMITTED is recorded only on strong,
                      visible confirmation; SMTP acceptance is SMTP_ACCEPTED.

CAPTCHA, login walls, missing CV upload fields, unknown required questions and
anything else that cannot be completed truthfully stop as MANUAL_REQUIRED.
CAPTCHA/anti-bot protection is never bypassed or solved automatically. In an
interactive explicit submit the browser is headed and a person may solve a
challenge; the wait is bounded (CAPTCHA_WAIT_SECONDS) and never reads stdin.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

import safety
import storage
from application_prep import (
    EMAIL, WEB, application_subject, find_application_email, generate_cover_letter, generate_form_answers,
    letter_problems, sha256_file,
)
from fixed_cv import resolve_fixed_cv_path
from qualification import (
    MANUAL_REQUIRED, QUALIFIED, READY_TO_SUBMIT, SMTP_ACCEPTED, SUBMISSION_FAILED, SUBMITTED, SUBMITTING,
    UNVERIFIED_LEGACY, qualify_job,
)

logger = logging.getLogger(__name__)

DRY_RUN = "DRY_RUN"
LIVE = "LIVE"
DRY_RUN_VALIDATED = "DRY_RUN_VALIDATED"
# How long an interactive submit waits for a person to solve a challenge.
CAPTCHA_WAIT_SECONDS = 180.0
CAPTCHA_POLL_SECONDS = 2.0


def get_submission_mode(profile: Optional[Dict]) -> str:
    cfg = (profile or {}).get("pipeline", {}) or {}
    if str(cfg.get("submission_mode", DRY_RUN)).upper() == LIVE and cfg.get("allow_live_submission") is True:
        return LIVE
    return DRY_RUN


# --- Page inspection ---------------------------------------------------------

# Full-page anti-bot challenge / interstitial: nothing else is reachable until it is passed.
_CAPTCHA_CHALLENGE = re.compile(
    r"challenges\.cloudflare\.com/cdn-cgi/challenge-platform|/cdn-cgi/challenge-platform|cf-chl-|"
    r"<title[^>]*>\s*(just a moment|attention required|un momento|access denied|verif(y|ying) you are human|"
    r"are you a robot|security check)|checking your browser before|verify(ing)? (that )?you are (a )?human|"
    r"comprueba que eres humano|px-captcha|captcha-delivery\.com|datadome", re.I)
# A CAPTCHA widget a person has to solve, embedded in the page/form.
_CAPTCHA_WIDGET = re.compile(
    r"class=[\"'][^\"']*\b(g-recaptcha|h-captcha|cf-turnstile|frc-captcha)\b"
    r"|<iframe[^>]+src=[\"'][^\"']*(recaptcha|hcaptcha|turnstile|captcha)", re.I)
# The widget's response token is filled in only after the challenge was solved.
_CAPTCHA_TOKEN_HTML = re.compile(
    r"name=[\"'](?:g-recaptcha-response|h-captcha-response|cf-turnstile-response|frc-captcha-solution)[\"']"
    r"[^>]*?(?:value=[\"']([^\"']{8,})[\"']|>\s*([^<\s][^<]{7,})<)", re.I)
_CAPTCHA_TOKEN_JS = (
    "() => Array.from(document.querySelectorAll('[name=\"g-recaptcha-response\"],"
    "[name=\"h-captcha-response\"],[name=\"cf-turnstile-response\"],[name=\"frc-captcha-solution\"]'))"
    ".some(e => (e.value || '').length > 8)")
_LOGIN_URL = re.compile(r"/(login|signin|sign-in|authwall|auth|account/login|uas/login)\b", re.I)
_LOGIN_TEXT = re.compile(
    r"sign in to apply|log ?in to apply|create an account to apply|sign in to continue|"
    r"inicia sesi[oó]n para (aplicar|inscribirte|postular|continuar)|reg[ií]strate para (aplicar|inscribirte)", re.I)
# Strong, human-readable statements that an application was received.
_CONFIRM_TEXT = re.compile(
    r"thank you for (applying|your application)|thanks for applying|"
    r"(your )?application (has been |was )?(successfully )?(received|submitted|sent)|"
    r"we(?: have|'ve) received your application|gracias por (tu|su) (candidatura|solicitud|inter[eé]s)|"
    r"(candidatura|solicitud) (ha sido )?(enviada|recibida|registrada)( correctamente| con [eé]xito)?|"
    r"hemos recibido (tu|su) (candidatura|solicitud)", re.I)
_CONFIRM_PATH = re.compile(
    r"/(thanks|thank-you|thank_you|thankyou|gracias|confirmation|confirmacion|success|submitted|"
    r"application-(?:submitted|received|sent))/?$", re.I)
_APPLICATION_ID = re.compile(
    r"\b(application|candidatura|solicitud|confirmation|confirmaci[oó]n)\s+"
    r"(id|number|no\.?|n[uú]mero|n[ºo°]\.?|ref(?:erence|erencia)?\.?|code|c[oó]digo)\s*[:#]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9-]{3,})", re.I)
# Words that turn a nearby "confirmation" into a failure message.
_NEGATION = re.compile(
    r"\b(not|n't|no se|no ha|no pudo|error|errors|invalid|inv[aá]lid[oa]|missing|incorrect\w*|fail(?:ed|ure)?|"
    r"unable|cannot|unsuccess\w*|incomplet\w*|falta\w*|formato|format|required|obligatori\w*|"
    r"try again|int[eé]ntalo|expired|caducad\w*)\b", re.I)
_PAGE_ERROR = re.compile(
    r"\b(error|invalid|inv[aá]lid[oa]|required field|campo obligatorio|try again|int[eé]ntalo de nuevo|"
    r"session (has )?expired|sesi[oó]n (ha )?(expirado|caducado)|sign in|log ?in|inicia sesi[oó]n)\b", re.I)
# A legitimate application never asks the candidate for money or card details.
_PAYMENT = re.compile(
    r"(application|registration|processing|admin(istration)?) fee|tasa de (inscripci[oó]n|tramitaci[oó]n)|"
    r"cuota de inscripci[oó]n|credit card number|n[uú]mero de (la )?tarjeta|name=.?(card.?number|cvv|iban)\b", re.I)

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


def _visible_text(page=None, html: str = "") -> str:
    """Text a person can read on the page: never script/style/template content,
    hidden elements or attributes."""
    if page is not None:
        reader = getattr(page, "inner_text", None)
        if callable(reader):
            try:
                text = reader("body")
                if isinstance(text, str):
                    return re.sub(r"\s+", " ", text).strip()
            except Exception:
                pass
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "head", "title"]):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.attrs is None:
            continue
        style = str(tag.get("style") or "").replace(" ", "").lower()
        if (tag.has_attr("hidden") or tag.get("aria-hidden") == "true" or tag.get("type") == "hidden"
                or "display:none" in style or "visibility:hidden" in style):
            tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def _captcha_state(html: str, page=None) -> str:
    """'challenge' (blocking interstitial), 'unsolved' (embedded widget without a
    response token), 'solved' (widget whose response token is filled in) or 'none'.

    A widget that is still in the HTML is 'solved' only when its response token
    exists; a bare script include or a "protected by reCAPTCHA" notice is not a
    challenge a person could solve and is reported as 'none'."""
    html = html or ""
    if _CAPTCHA_CHALLENGE.search(html):
        return "challenge"
    if not _CAPTCHA_WIDGET.search(html):
        return "none"
    if _CAPTCHA_TOKEN_HTML.search(html):
        return "solved"
    if page is not None:
        try:
            if page.evaluate(_CAPTCHA_TOKEN_JS) is True:
                return "solved"
        except Exception:
            pass
    return "unsolved"


def _password_wall(html: str) -> bool:
    """A password field stands between the visitor and the application form.

    Not a wall: the site's own header/login form sitting NEXT to a separate public
    form that has the CV upload. A wall: a password field with no public upload
    form on the page, or inside the very form that holds the upload (account
    creation is part of applying)."""
    if not re.search(r'type=["\']?password', html or "", re.I):
        return False
    soup = BeautifulSoup(html, "html.parser")

    def has(node, kind):
        return any((i.get("type") or "").lower() == kind for i in node.find_all("input"))

    upload_forms = [f for f in soup.find_all("form") if has(f, "file")]
    if not upload_forms:
        return True
    return any(has(f, "password") for f in upload_forms)


def _blocked_reason(page) -> str:
    """CAPTCHA / login wall / payment request on the current page ('' when public)."""
    html = page.content() or ""
    if _captcha_state(html, page) in ("challenge", "unsolved"):
        return "CAPTCHA detected - manual application required (never bypassed)"
    if (_LOGIN_URL.search(urlparse(page.url or "").path or "") or _password_wall(html)
            or _LOGIN_TEXT.search(_visible_text(None, html))):
        return "Login required - manual application required (authentication never bypassed)"
    if _PAYMENT.search(html):
        return "Payment/fee requested - manual review required (never paid or filled automatically)"
    return ""


def _visible_fields(page) -> List[Dict]:
    return [f for f in (page.evaluate(_FIELDS_JS) or [])
            if isinstance(f, dict) and "idx" in f and f.get("type") not in _CLICKABLE
            and (not f.get("hidden") or f.get("type") == "file")]


def _apply_link(page, visited: List[str]) -> str:
    """A public 'Apply' link on the page: a safe public URL on the same site or a
    known ATS, never a login-walled board or login page."""
    from application_prep import _host, ats_for_url, is_job_board, is_login_walled
    here = _host(page.url or "")
    for link in page.evaluate(_LINKS_JS) or []:
        if not isinstance(link, dict):
            continue
        href = link.get("href") or ""
        if (not safety.is_safe_public_url(href) or href in visited or is_login_walled(href)
                or is_job_board(href) or _LOGIN_URL.search(href)):
            continue
        host = _host(href)
        same_site = bool(here) and (host == here or host.endswith("." + here) or here.endswith("." + host))
        if not (same_site or ats_for_url(href)):
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


def _human_present() -> bool:
    """A person is at the console of this process (an interactive terminal)."""
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())
    except Exception:
        return False


def _wait_for_human(page, reason: str, timeout: Optional[float] = None, poll: Optional[float] = None,
                    sleep: Callable = time.sleep, clock: Callable = time.monotonic) -> bool:
    """Give a person at most `timeout` seconds to solve the challenge in the open
    (headed) browser window. The page is polled; stdin is never read, so this can
    never block forever. True as soon as the page is no longer blocked by a
    CAPTCHA, False on timeout or if the page/browser went away."""
    timeout = CAPTCHA_WAIT_SECONDS if timeout is None else timeout
    poll = CAPTCHA_POLL_SECONDS if poll is None else poll
    try:
        print(f"\n{reason}\n  Page: {page.url}\n  Solve the CAPTCHA in the open browser window within "
              f"{timeout:.0f}s; the form continues automatically once it is solved.", flush=True)
    except Exception:
        pass
    deadline = clock() + timeout
    while True:
        try:
            if not _blocked_reason(page).startswith("CAPTCHA"):
                return True
        except Exception:
            return False
        if clock() >= deadline:
            logger.warning("CAPTCHA not solved within %gs - leaving the application for manual handling", timeout)
            return False
        sleep(poll)


def fill_application_form(page, url: str, answers: Dict[str, str], cv_path: Path,
                          mode: str = DRY_RUN, cv_sha256: str = "", job: Optional[Dict] = None,
                          wait_for_human: Optional[Callable] = None) -> Dict:
    """Drive one public application form on a Playwright-like `page`.

    Opens `url` (following at most two public "Apply" links to reach the form),
    stops on CAPTCHA/login walls and (given `job`) on a page that shows some
    other vacancy, and maps each field to a truthful profile answer.

    Planning comes first and touches nothing. In DRY_RUN that is all that
    happens: the report says what would be entered, but nothing is typed,
    selected or uploaded. Only in LIVE, and only when every required field can
    be answered, the CV upload field exists, a submit button exists and the CV
    hash still matches, are the fields filled, the fixed CV uploaded and the
    final submit clicked.

    On a CAPTCHA, `wait_for_human(page, reason)` (when given) lets a person
    solve it in the browser; the page is then re-checked and the flow continues
    only if nothing blocks it any more. The CAPTCHA is never bypassed.

    Returns {"status", "reason", "filled", "missing_required", "evidence", "report"}.
    """
    report = {"route_url": url, "final_url": "", "navigated": [], "fields_detected": [],
              "fields_prepared": {}, "fields_manual": [], "would_submit": {}, "cv_uploaded": "",
              "submit_clicked": False}
    result = {"status": MANUAL_REQUIRED, "reason": "", "filled": [], "missing_required": [],
              "evidence": "", "report": report}

    _goto(page, url)
    fields: List[Dict] = []
    for hop in range(3):
        blocked = _blocked_reason(page)
        if blocked.startswith("CAPTCHA") and wait_for_human:
            # A person solves it in the headed browser; the same detection decides afterwards.
            report["captcha_human_intervention"] = True
            if wait_for_human(page, blocked):
                blocked = _blocked_reason(page)
        if blocked:
            result["reason"] = blocked
            report["final_url"] = page.url
            return result
        if job and hop == 0:
            # Target vacancy check on the route page, before anything is filled or uploaded.
            from employer_routes import vacancy_evidence
            report["vacancy_match"] = vacancy_evidence(job, page.url or url, page.content() or "")
            if not report["vacancy_match"]["verified"]:
                result["reason"] = ("Opened page does not show the target vacancy "
                                    f"({job.get('title', '')}) - nothing filled, manual application required")
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
    ok, why = safety.check_public_url(page.url or url)
    if not ok:
        result["reason"] = f"Form page is not a safe public URL ({why}) - nothing filled"
        return result

    if not fields:
        result["reason"] = "No application form found on page"
        return result

    # Restrict to the form holding the CV upload (not a newsletter/search form).
    cv_field = next((f for f in fields if f["type"] == "file" and re.search(r"resume|cv|curr[ií]cul", _field_text(f))),
                    next((f for f in fields if f["type"] == "file" and not re.search(r"cover", _field_text(f))), None))
    form_id = cv_field.get("form", -1) if cv_field else -1
    if form_id not in (-1, None):
        fields = [f for f in fields if f.get("form", -1) == form_id]

    # --- Plan: decide every action without touching the page -------------------
    manual, seen_groups, actions = report["fields_manual"], set(), []
    for f in fields:
        label = _field_label(f)
        report["fields_detected"].append(label)
        ftype, required = f["type"], bool(f.get("required"))
        if f is cv_field:
            actions.append(("upload", _selector(f), str(cv_path)))
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
                    actions.append(("select", _selector(f), value))
        elif ftype not in _TEXT_TYPES and f["tag"] != "textarea":
            value = None  # number/date/etc.: nothing in the profile answers them truthfully
        elif key == "cover_letter" and f["tag"] != "textarea" and ftype != "text":
            value = None
        elif value:
            actions.append(("fill", _selector(f), str(value)))
        if value:
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
        result["reason"] = (f"DRY_RUN: {len(report['fields_prepared'])} fields mapped incl. fixed CV; "
                            f"nothing typed or uploaded, final submit NOT clicked")
        return result

    # --- LIVE: every pre-condition holds; only now is the page touched ----------
    if safety.live_sends_forbidden():
        result["reason"] = "LIVE submission is disabled inside the pipeline/daemon - not submitted"
        return result
    if cv_sha256 and sha256_file(cv_path) != cv_sha256:
        result["reason"] = "Fixed CV checksum changed before submit - not submitted"
        return result
    scope = f'[data-jf-form="{form_id}"] ' if form_id not in (-1, None) else ""
    submit = page.query_selector(f'{scope}button[type="submit"], {scope}input[type="submit"]')
    if not submit:
        result["reason"] = "No submit button found - manual application required"
        return result
    for kind, selector, value in actions:
        if kind == "upload":
            page.set_input_files(selector, value)
            report["cv_uploaded"] = value
        elif kind == "select":
            page.select_option(selector, label=value)
        else:
            page.fill(selector, value)
    blocked = _blocked_reason(page)  # e.g. a CAPTCHA that appeared while filling
    if blocked:
        result["reason"] = f"{blocked} (form filled but NOT submitted)"
        return result
    before_html, before_url = page.content() or "", page.url
    before_text = _visible_text(page, before_html)
    # From here on the click may have reached the site, even if click() raises
    # (e.g. a timeout after the click was dispatched): never "nothing submitted".
    report["submit_clicked"] = True
    try:
        submit.click()
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        report["final_url"] = page.url
        outcome = verify_submission(page, before_html=before_html, before_url=before_url, before_text=before_text)
    except Exception as e:  # the click was attempted; its result could not be read
        outcome = {"status": SUBMISSION_FAILED, "evidence": "",
                   "reason": f"Submit clicked but the result page could not be read ({e.__class__.__name__}) "
                             "- outcome unknown, verify manually; never retried automatically"}
    return {**result, **outcome}


_FAILURE_NEARBY = re.compile(
    r"\b(not|n't|no se|no ha|no pudo|error|errors|unable|cannot|could not|fail(?:ed|ure|s)?|unsuccess\w*|"
    r"sin [eé]xito|incorrect\w*)\b", re.I)


def _has_negation(text: str, start: int, end: int, strict: bool = False) -> bool:
    """A failure word right next to the match. `strict` (application ids) also
    treats validation wording ("invalid", "missing", "format", "required") as failure."""
    if strict:
        return bool(_NEGATION.search(text[max(0, start - 45):end + 45]))
    return bool(_FAILURE_NEARBY.search(text[max(0, start - 30):start])
                or _FAILURE_NEARBY.search(text[end:end + 25]))


def verify_submission(page, before_html: str = "", before_url: str = "",
                      before_text: Optional[str] = None) -> Dict:
    """SUBMITTED only on strong, visible evidence that appeared after the click:

      * a confirmation sentence a person can read (never script/hidden text),
        not negated by a nearby failure word; or
      * an application/confirmation id that contains a digit, in non-error
        context; or
      * a redirect whose PATH ends in a success segment, on a page that shows
        no error/login text (query strings and partial words never count).

    A CAPTCHA that appears after the click is MANUAL_REQUIRED. Everything else,
    including a redirect to a login/account page, is SUBMISSION_FAILED."""
    html = page.content() or ""
    text = _visible_text(page, html)
    if before_text is None:
        before_text = _visible_text(None, before_html or "")
    blocking = ("challenge", "unsolved")
    if _captcha_state(html, page) in blocking and _captcha_state(before_html or "") not in blocking:
        return {"status": MANUAL_REQUIRED, "reason": "CAPTCHA shown after submit", "evidence": ""}
    path = urlparse(page.url or "").path or ""
    before_path = urlparse(before_url or "").path or ""
    if _LOGIN_URL.search(path) or _LOGIN_TEXT.search(text):
        return {"status": SUBMISSION_FAILED, "evidence": "",
                "reason": "Submit clicked but the site went to a login page - no confirmation observed"}

    if not _CONFIRM_TEXT.search(before_text):
        for m in _CONFIRM_TEXT.finditer(text):
            if not _has_negation(text, m.start(), m.end()):
                return {"status": SUBMITTED, "reason": "Confirmed by confirmation text", "evidence": m.group(0)[:200]}
    for m in _APPLICATION_ID.finditer(text):
        ident = m.group(3)
        if (any(ch.isdigit() for ch in ident) and ident not in before_text
                and not _has_negation(text, m.start(), m.end(), strict=True)):
            return {"status": SUBMITTED, "reason": "Confirmed by application id", "evidence": m.group(0)[:200]}
    if path != before_path and _CONFIRM_PATH.search(path) and not _PAGE_ERROR.search(text):
        return {"status": SUBMITTED, "reason": "Confirmed by success redirect", "evidence": page.url}
    return {"status": SUBMISSION_FAILED, "reason": "Submit clicked but no confirmation observed", "evidence": ""}


def _open_browser_page(headless: bool = True):
    """Return (page, close_fn) using Playwright, or raise ImportError."""
    from playwright.sync_api import sync_playwright  # optional dependency
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=headless)
    page = browser.new_page()
    page.set_default_timeout(30000)
    page.set_default_navigation_timeout(45000)

    def close():
        try:
            browser.close()
        finally:
            pw.stop()
    return page, close


# --- Orchestration -----------------------------------------------------------

def _email_outcome(result) -> Dict:
    """Normalise a sender's return value (detailed dict, or legacy bool)."""
    if isinstance(result, dict):
        return {"ok": bool(result.get("ok")), "error_class": str(result.get("error_class") or "unknown"),
                "sent_possible": bool(result.get("sent_possible", True)), "detail": str(result.get("detail") or "")}
    if result is True or (result and not isinstance(result, (str, bytes))):
        return {"ok": True, "error_class": "", "sent_possible": True, "detail": ""}
    # A bare False says nothing about how far the send got: treat the outcome as unknown.
    return {"ok": False, "error_class": "unknown", "sent_possible": True, "detail": "Email sending failed"}


def submit_application(app_id: int, profile: Dict, *, mode: Optional[str] = None,
                       page_factory: Optional[Callable] = None,
                       email_sender: Optional[Callable] = None,
                       db_path: Optional[Path] = None,
                       interactive: Optional[bool] = None, matcher=None) -> Dict:
    """Run the web or email route for one READY_TO_SUBMIT application.

    This is the explicit submit action. LIVE needs both profile flags and is
    forced down to DRY_RUN inside safety.no_live_sends() (pipeline/daemon).
    Before anything is sent the job is re-qualified under the current rules,
    the letter/answers are regenerated from the current profile and checked for
    grounding, and (LIVE) the application is claimed atomically."""
    db = db_path or storage.DB_PATH
    mode = mode if mode in (DRY_RUN, LIVE) else get_submission_mode(profile)
    if mode == LIVE and (get_submission_mode(profile) != LIVE or safety.live_sends_forbidden()):
        mode = DRY_RUN  # LIVE must be enabled in configuration and explicitly requested
    conn = storage.get_db(db)
    row = conn.execute(
        """SELECT a.*, j.title, j.company, j.location, j.description, j.url, j.board, j.date_posted, j.job_type
           FROM applications a JOIN jobs j ON a.job_url = j.url WHERE a.id = ?""", (app_id,)).fetchone()
    conn.close()
    if not row:
        return {"status": "error", "reason": "Application not found"}
    app = dict(row)

    # Duplicate gates: any earlier send/submission evidence blocks, whatever the status says.
    if app["status"] == SUBMITTED or app.get("submitted_at"):
        return {"status": SUBMITTED, "reason": "Already submitted - duplicate submission blocked"}
    if (app.get("sent_at") or app.get("submission_evidence") or app["status"] == SMTP_ACCEPTED
            or app.get("submission_status") in (SUBMITTED, SMTP_ACCEPTED, UNVERIFIED_LEGACY)):
        return {"status": app["status"],
                "reason": "Already sent (send/submission evidence is recorded) - duplicate submission blocked"}
    if app["status"] == SUBMITTING or app.get("submission_status") == SUBMITTING:
        return {"status": SUBMITTING,
                "reason": "Submission already in progress or interrupted - duplicate submission blocked"}
    if app["status"] != READY_TO_SUBMIT:
        return {"status": app["status"], "reason": f"Not READY_TO_SUBMIT (status {app['status']})"}
    logger.info("Submission attempt: application #%s (%s, mode %s) %s at %s", app_id,
                app.get("application_method") or "?", mode, app.get("title"), app.get("company"))

    # Fixed CV only: exact configured file, SHA-256 must match the one recorded at preparation.
    cv_path = resolve_fixed_cv_path(profile)
    if (Path(app["cv_pdf_path"] or "").resolve() != cv_path or not app.get("cv_sha256")
            or sha256_file(cv_path) != app["cv_sha256"]):
        return _record(app_id, db, mode, MANUAL_REQUIRED, "Fixed CV path/checksum mismatch")

    # The job must pass TODAY's qualification rules, not only those of the day it was prepared.
    q = qualify_job(app, profile, matcher=matcher)
    if q.status != QUALIFIED:
        storage.set_job_qualification(app["job_url"], q.status, q.category, q.reasons, db_path=db)
        return _record(app_id, db, mode, MANUAL_REQUIRED,
                       f"Job no longer passes qualification ({q.status}): {'; '.join(q.reasons)}")

    # Content is regenerated from the current profile (a stored letter may be stale) and checked.
    letter = generate_cover_letter(app, profile, q.category)
    problems = letter_problems(letter, profile)
    if problems:
        return _record(app_id, db, mode, MANUAL_REQUIRED,
                       "Application content is not grounded in the profile: " + "; ".join(problems))
    answers = generate_form_answers(app, profile, q.category, letter)
    subject = application_subject(app, profile)
    if letter != (app.get("email_body") or "") or subject != (app.get("email_subject") or ""):
        storage.update_application(app_id, db_path=db, email_body=letter, email_subject=subject,
                                   form_answers_json=json.dumps(answers))

    if app.get("application_method") == WEB:
        from application_prep import is_login_walled
        route_url = app.get("application_url") or app["url"]
        report = {"route_url": route_url, "route_type": app.get("route_type") or "",
                  "cv_path": str(cv_path), "cv_sha256": app["cv_sha256"]}
        ok, why = safety.check_public_url(route_url)
        if not ok:
            return _record(app_id, db, mode, MANUAL_REQUIRED,
                           f"Application URL is not a safe public URL ({why}) - never opened", report=report)
        if is_login_walled(route_url):
            return _record(app_id, db, mode, MANUAL_REQUIRED,
                           "Application route is a login-walled job board - not automated", report=report)
        # The stored route may predate today's ownership rules: it must still belong to this employer/vacancy.
        import application_prep
        source = app.get("route_source") or ("job_url" if route_url == app["url"] else "")
        problem = application_prep.verify_route(app, {"url": route_url, "source": source},
                                                fetch=application_prep._http_fetch)
        if problem:
            return _record(app_id, db, mode, MANUAL_REQUIRED,
                           f"Application URL not used: {problem}", report=report)
        if page_factory is None:  # real browser: the host must not resolve to a private address
            ok, why = safety.resolves_to_public(route_url)
            if not ok:
                return _record(app_id, db, mode, MANUAL_REQUIRED,
                               f"Application URL refused ({why}) - never opened", report=report)
        # A person can help only in an explicit interactive LIVE submit with a real browser.
        if interactive is None:
            interactive = _human_present()
        human = bool(interactive) and page_factory is None and mode == LIVE
        if mode == LIVE and not storage.claim_application(app_id, db):
            return {"status": SUBMITTING, "reason": "Application is already claimed by another submitter or "
                                                    "carries send evidence - duplicate submission blocked"}
        claimed = mode == LIVE
        close = None
        try:
            try:
                page, close = (page_factory() if page_factory else _open_browser_page(headless=not human))
            except ImportError:
                return _record(app_id, db, mode, MANUAL_REQUIRED,
                               "Playwright not installed (pip install playwright; playwright install chromium)",
                               report=report, claimed=claimed)
            try:
                res = fill_application_form(page, route_url, answers, cv_path, mode=mode,
                                            cv_sha256=app["cv_sha256"], job=app,
                                            wait_for_human=_wait_for_human if human else None)
            except Exception as e:
                # fill_application_form handles everything after the click itself, so
                # an exception here means nothing was submitted.
                logger.exception("Browser automation stopped for application #%s", app_id)
                res = {"status": MANUAL_REQUIRED, "reason": f"Browser automation stopped: {e}", "evidence": ""}
        except Exception as e:
            res = {"status": MANUAL_REQUIRED, "reason": f"Browser could not be started: {e}", "evidence": ""}
        finally:
            if close:
                try:
                    close()
                except Exception as e:
                    logger.warning("Browser did not close cleanly: %s", e)
        report.update(res.get("report") or {})
        return _record(app_id, db, mode, res["status"], res["reason"], res.get("evidence", ""), report=report,
                       claimed=claimed)

    if app.get("application_method") == EMAIL:
        to = find_application_email(app, app.get("recruiter_email") or "")
        if not to:
            return _record(app_id, db, mode, MANUAL_REQUIRED, "No legitimate application email")
        earlier = storage.recipient_already_emailed(to, app_id, db)
        if earlier:
            return _record(app_id, db, mode, MANUAL_REQUIRED,
                           f"{to} already received application #{earlier} - a second email needs a human decision")
        if mode != LIVE:
            return _record(app_id, db, mode, DRY_RUN_VALIDATED,
                           f"DRY_RUN: email to {to} prepared with fixed CV; NOT sent")
        if not storage.claim_application(app_id, db):
            return {"status": SUBMITTING, "reason": "Application is already claimed by another submitter or "
                                                    "carries send evidence - duplicate submission blocked"}
        # Re-check under the claim: two different applications for the same address,
        # submitted at the same moment, must not both go out.
        storage.update_application_if_status(app_id, (SUBMITTING,), db_path=db, recruiter_email=to)
        rival = storage.recipient_already_emailed(to, app_id, db, include_in_progress=True)
        if rival:
            return _record(app_id, db, mode, MANUAL_REQUIRED,
                           f"{to} is already being emailed / was emailed by application #{rival} - "
                           "a second email needs a human decision", claimed=True)
        if email_sender is None:
            from applier import send_application_email_detailed as email_sender
        try:
            outcome = _email_outcome(email_sender(to_email=to, subject=subject, body=letter, cv_path=cv_path,
                                                  cover_letter_path=None))
        except Exception as e:
            logger.exception("Email sender raised for application #%s", app_id)
            return _record(app_id, db, mode, SUBMISSION_FAILED,
                           f"Exception during send ({e.__class__.__name__}) - delivery state unknown; "
                           "check the Sent folder. Never retried automatically", claimed=True)
        if outcome["ok"]:
            # SMTP acceptance is not proof that an application was received.
            return _record(app_id, db, mode, SMTP_ACCEPTED,
                           f"Email accepted by SMTP server for {to}; not a verified submission", claimed=True)
        detail = f"{outcome['error_class']}: {outcome['detail']}".strip(": ")
        if outcome["error_class"] == "recipient_rejected" and not outcome["sent_possible"]:
            return _record(app_id, db, mode, MANUAL_REQUIRED, f"Recipient rejected ({detail}) - nothing was sent",
                           claimed=True)
        if not outcome["sent_possible"]:
            # Provably nothing left this machine: the claim is released for an explicit retry.
            return _record(app_id, db, mode, SUBMISSION_FAILED,
                           f"Email NOT sent ({detail}). Nothing left this machine; an explicit submit may retry",
                           keep_ready=True, claimed=True)
        return _record(app_id, db, mode, SUBMISSION_FAILED,
                       f"Email sending failed ({detail}) - delivery state unknown; never retried automatically",
                       claimed=True)

    return _record(app_id, db, mode, MANUAL_REQUIRED, "No supported application method")


def process_ready_applications(profile: Dict, *, db_path: Optional[Path] = None, limit: int = 10,
                               page_factory: Optional[Callable] = None,
                               email_sender: Optional[Callable] = None,
                               mode: Optional[str] = None, interactive: Optional[bool] = False) -> Dict[str, int]:
    """Run READY_TO_SUBMIT applications through submit_application.

    The pipeline calls this with mode=DRY_RUN (inside safety.no_live_sends()):
    each application is inspected once (already DRY_RUN_VALIDATED ones are
    skipped) and nothing is submitted or sent. Only an explicit caller may pass
    LIVE-enabled configuration; then each application is claimed and submitted
    at most once. Returns outcome counts."""
    db = db_path or storage.DB_PATH
    effective = mode if mode in (DRY_RUN, LIVE) else get_submission_mode(profile)
    if effective == LIVE and (get_submission_mode(profile) != LIVE or safety.live_sends_forbidden()):
        effective = DRY_RUN
    conn = storage.get_db(db)
    rows = conn.execute(
        """SELECT a.id, a.submission_status FROM applications a JOIN jobs j ON a.job_url = j.url
           WHERE a.status = ? AND j.qualification_status = ? AND COALESCE(a.submitted_at, '') = ''
             AND COALESCE(a.sent_at, '') = ''
           ORDER BY a.id""", (READY_TO_SUBMIT, QUALIFIED)).fetchall()
    conn.close()
    ids = [r[0] for r in rows if effective == LIVE or (r[1] or "") != DRY_RUN_VALIDATED][:max(0, limit)]
    counts: Dict[str, int] = {}
    matcher = None
    if ids:
        from matcher import JobMatcher
        matcher = JobMatcher(profile)
    for app_id in ids:
        try:
            outcome = submit_application(app_id, profile, mode=effective, page_factory=page_factory,
                                         email_sender=email_sender, db_path=db, interactive=interactive,
                                         matcher=matcher)["status"]
        except Exception as e:  # one broken application never stops the run
            logger.exception("Application #%s could not be processed: %s", app_id, e)
            outcome = "error"
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def _record(app_id: int, db: Path, mode: str, outcome: str, reason: str, evidence: str = "",
            report: Optional[Dict] = None, keep_ready: bool = False, claimed: bool = False) -> Dict:
    """Persist the outcome. DRY_RUN keeps the application READY_TO_SUBMIT, as does
    a LIVE failure that provably happened before anything was sent (`keep_ready`).

    The write is conditional on the state this worker expects: SUBMITTING when it
    holds the claim (`claimed`), READY_TO_SUBMIT otherwise. If another worker has
    moved the application on in the meantime, nothing is overwritten."""
    if outcome == SUBMITTED and not evidence:
        outcome, reason = SUBMISSION_FAILED, f"{reason} (no observable evidence)"
    fields = {"submission_mode": mode, "submission_status": outcome, "status_reason": reason,
              "submission_evidence": evidence}
    if report is not None:
        fields["submission_report_json"] = json.dumps(report)
    if outcome != DRY_RUN_VALIDATED:
        fields["status"] = READY_TO_SUBMIT if keep_ready else outcome
    if outcome == SUBMITTED:
        fields["submitted_at"] = datetime.now().isoformat()
    if outcome == SMTP_ACCEPTED:
        fields["sent_at"] = datetime.now().isoformat()
    expected = (SUBMITTING,) if claimed else (READY_TO_SUBMIT,)
    if not storage.update_application_if_status(app_id, expected, db_path=db, **fields):
        logger.warning("Application #%s changed state concurrently; %s outcome '%s' was NOT recorded over it",
                       app_id, mode, outcome)
        return {"status": outcome, "reason": f"{reason} (not recorded: the application was changed by another "
                                             "worker in the meantime)", "evidence": evidence, "mode": mode,
                "report": report or {}, "retryable": False, "recorded": False}
    if outcome == SUBMITTED:
        conn = storage.get_db(db)
        conn.execute("UPDATE jobs SET applied = 1 WHERE url = (SELECT job_url FROM applications WHERE id = ?)",
                     (app_id,))
        conn.commit()
        conn.close()
    logger.info("Submission result: application #%s -> %s (%s) %s", app_id, outcome, mode, reason)
    return {"status": outcome, "reason": reason, "evidence": evidence, "mode": mode, "report": report or {},
            "retryable": keep_ready, "recorded": True}


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
    sent_at is kept: it is the record that something was sent and blocks re-sending.
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
