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
import html
import json
import logging
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qsl, unquote, urlparse

import safety
import storage
from fixed_cv import application_dir_for_slug, resolve_fixed_cv_path
from qualification import (
    APPLICATION_PREPARED, MANUAL_REQUIRED, QUALIFIED, READY_TO_SUBMIT, qualify_job,
)

logger = logging.getLogger(__name__)

WEB = "WEB"
EMAIL = "EMAIL"

# Public ATS application pages (no account needed to reach the form):
# (route type, host regex). Marketing hosts such as www.greenhouse.io never match.
ATS_HOST_PATTERNS = (
    ("greenhouse", re.compile(r"^(boards|job-boards)(\.eu)?\.greenhouse\.io$")),
    ("lever", re.compile(r"^jobs(\.eu)?\.lever\.co$")),
    ("workable", re.compile(r"^(apply|jobs)\.workable\.com$")),
    ("ashby", re.compile(r"^jobs\.ashbyhq\.com$")),
    ("smartrecruiters", re.compile(r"^(jobs|careers)\.smartrecruiters\.com$")),
    ("taleo", re.compile(r"\.taleo\.net$")),
    ("icims", re.compile(r"\.icims\.com$")),
    ("workday", re.compile(r"\.myworkdayjobs\.com$")),
    ("personio", re.compile(r"\.jobs\.personio\.(de|com)$")),
    ("teamtailor", re.compile(r"\.teamtailor\.com$")),
    ("recruitee", re.compile(r"\.recruitee\.com$")),
    ("bamboohr", re.compile(r"\.bamboohr\.com$")),
    ("jobvite", re.compile(r"^jobs\.jobvite\.com$")),
    ("successfactors", re.compile(r"\.successfactors\.(com|eu)$")),
    ("avature", re.compile(r"\.avature\.net$")),
)
# Boards whose apply flow needs a login / is anti-bot protected: never automated.
LOGIN_WALLED_HOSTS = ("linkedin.com", "indeed.com", "glassdoor.", "infojobs.net")
# Aggregators/boards: their pages are listings, not the employer's application form.
JOB_BOARD_HOSTS = LOGIN_WALLED_HOSTS + (
    "adzuna.", "jooble.", "google.", "stepstone.", "bayt.com", "gulftalent.com", "wuzzuf.net",
    "himalayas.app", "remotive.com", "arbeitnow.com", "themuse.com", "talent.com",
    "careerjet.", "trovit.", "jobatus.", "facebook.com", "twitter.com", "x.com", "instagram.com",
)
# Multi-tenant careers platforms whose URL path names the employer (…/companies/<employer>/…).
CAREER_PLATFORM_HOSTS = ("join.com",)
EMPLOYER = "employer"  # direct employer application page (non-ATS)
_NO_EMPLOYER = ("", "unknown", "confidential", "confidencial", "n/a")  # company placeholders

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

def format_phone(profile: Dict) -> str:
    """Phone in international format (+<country code><number>), or "" when the
    profile gives no country code. A local-format number is never written to an
    employer: it is unusable from another country."""
    facts = profile.get("candidate_facts") or {}
    raw = str(profile.get("phone") or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if raw.startswith("+"):
        return "+" + digits
    if raw.startswith("00"):
        return "+" + digits[2:]
    code = re.sub(r"\D", "", str(facts.get("phone_country_code") or ""))
    if not code:
        logger.warning("profile phone has no country code (set candidate_facts.phone_country_code); "
                       "phone omitted from applications")
        return ""
    return f"+{code}{digits.lstrip('0')}"


def candidate_facts(profile: Dict) -> Dict:
    """Facts written into letters/forms. Everything comes from profile.yaml
    (`candidate_facts` mirrors the CV); nothing is inferred or invented."""
    facts = dict(profile.get("candidate_facts") or {})
    name = (profile.get("name") or "").strip()
    first, _, last = name.partition(" ")
    relocating_to = str(facts.get("relocating_to") or "").strip()
    current = str(facts.get("current_location") or "").strip()
    # While relocating, the destination is NOT where the candidate is based.
    location = current if relocating_to else (profile.get("location") or "").strip()
    if "," in location:
        city, country = location.split(",")[0].strip(), location.split(",")[-1].strip()
    elif relocating_to:
        city, country = "", location
    else:
        city, country = location, ""
    licences = facts.get("driving_licences") or []
    if isinstance(licences, str):
        licences = [licences]
    return {
        "full_name": name,
        "first_name": first,
        "last_name": last,
        "email": (profile.get("email") or "").strip(),
        "phone": format_phone(profile),
        "location": location,
        "city": city,
        "country": country,
        "relocating_to": relocating_to,
        "work_authorisation": str(facts.get("work_authorisation") or "").strip(),
        "driving_licences": [str(x).strip() for x in licences if str(x).strip()],
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


def _employer_name(job: Dict) -> str:
    """The employer's real name, or "" when the posting does not give one. A
    placeholder such as "Unknown"/"Confidencial" is never written as a name."""
    from fingerprint import is_unnamed_company
    company = " ".join(str(job.get("company") or "").split())
    return "" if is_unnamed_company(company) else company


def _company_label(job: Dict) -> str:
    return _employer_name(job) or "your company"


def _job_city(job: Dict) -> str:
    """City of the vacancy for prose ("Ferrol"), never a bare region/country code ("CT", "ES")."""
    first = str(job.get("location") or "").split(",")[0].strip()
    return "" if (len(first) <= 3 and first.upper() == first) else first


def generate_cover_letter(job: Dict, profile: Dict, category: str) -> str:
    """Job-specific cover letter built only from profile facts (no LLM).

    No employer name is printed unless the posting names one; the candidate's
    location is stated as "relocating to ..." when the profile says so, never as
    "based in" the destination; driving roles cite only the licences listed in
    the profile and claim no driving employment; the phone is international."""
    f = candidate_facts(profile)
    title, employer = " ".join(str(job.get("title") or "advertised").split()), _employer_name(job)
    city = _job_city(job)
    skills = _category_skills(profile, category)
    lines = [f"Dear Hiring Team at {employer}," if employer else "Dear Hiring Team,", "",
             f"I am writing to apply for the {title} position" + (f" in {city}" if city else "") + "."]
    if f["summary"]:
        lines.append(f["summary"])
    if category == "fashion":
        lines.append("My background is in fashion design"
                     + (f", including {', '.join(skills[:6])}" if skills else "") + ".")
        lines.append("I am interested in this role because it centres on womenswear design, which is the "
                     "focus of my work, and I would like to contribute to "
                     + (f"{employer}'s collections." if employer else "your collections."))
    elif category == "driver":
        lines.append("I am applying for this driving role.")
        if f["driving_licences"]:
            lines.append("My CV lists the following driving licences: " + "; ".join(f["driving_licences"]) + ".")
        lines.append("I would like to support " + (f"{employer}'s" if employer else "your")
                     + " driving operations.")
    if f["relocating_to"]:
        lines.append(f"I am relocating to {f['relocating_to']}"
                     + (f" with {f['work_authorisation']}" if f["work_authorisation"] else "") + ".")
    elif f["location"]:
        lines.append(f"I am based in {f['location']}.")
    lines += ["My CV is attached with full details of my experience. "
              "I would welcome the opportunity to discuss the role with you.", "",
              "Kind regards,", f["full_name"]]
    lines += [x for x in (f["email"], f["phone"]) if x]
    return "\n".join(lines).strip() + "\n"


_UNKNOWN_EMPLOYER_TEXT = re.compile(r"\b(?:at|to|for)\s+(?:unknown|confidential|confidencial|n/a|none)\b"
                                    r"|\b(?:unknown|confidential|confidencial)'s\b|\bat your company,", re.I)
_LOCAL_PHONE_LINE = re.compile(r"^\s*(?!\+)[\d(][\d\s().-]{6,}\s*$", re.M)


def letter_problems(letter: str, profile: Dict) -> List[str]:
    """Grounding check run before any letter is sent. Returns the problems found
    (empty = none): placeholder employer written as a name, "based in" the
    relocation destination, or a phone number without a country code."""
    f = candidate_facts(profile)
    problems = []
    if _UNKNOWN_EMPLOYER_TEXT.search(letter or ""):
        problems.append("placeholder employer name in the letter")
    if f["relocating_to"] and re.search(rf"\bbased in {re.escape(f['relocating_to'].split(',')[0])}", letter or "", re.I):
        problems.append("letter claims the candidate is based in the relocation destination")
    if _LOCAL_PHONE_LINE.search(letter or ""):
        problems.append("phone number without a country code")
    if not f["full_name"] or f["full_name"] not in (letter or ""):
        problems.append("candidate name missing from the letter")
    return problems


def application_subject(job: Dict, profile: Dict) -> str:
    """Single-line subject (newlines in a scraped title can never add headers)."""
    return " ".join(f"Application for {job.get('title') or 'the advertised position'} - "
                    f"{candidate_facts(profile)['full_name']}".split())


def generate_form_answers(job: Dict, profile: Dict, category: str, cover_letter: str) -> Dict[str, str]:
    """Answers for common form fields. Only truthful, profile-backed values;
    anything else (salary, sponsorship, start date, languages...) is omitted."""
    f = candidate_facts(profile)
    answers = {k: f[k] for k in ("full_name", "first_name", "last_name", "email",
                                 "phone", "location", "city", "country", "linkedin") if f[k]}
    answers["cover_letter"] = cover_letter
    answers["why_interested"] = (
        f"I am applying for the {job.get('title', 'role')} role"
        + (f" at {_employer_name(job)} " if _employer_name(job) else " ")
        + ("because it is a womenswear design role, which matches my fashion design background."
           if category == "fashion" else
           ("because it is a driving role and my CV lists my driving licences." if f["driving_licences"]
            else "because it is a driving role."))
    )
    return answers


# --- Application method ------------------------------------------------------

# Role mailboxes that are not where applications go (privacy, legal, support...).
_ROLE_MAILBOX = re.compile(
    r"^(privacy|privacidad|dpo|dpd|lopd|rgpd|gdpr|data[-_.]?protection|proteccion[-_.]?(de[-_.]?)?datos|"
    r"protecciondedatos|legal|compliance|support|soporte|help|helpdesk|ayuda|abuse|postmaster|webmaster|"
    r"mailer-daemon|newsletter|marketing|press|prensa|billing|facturacion|unsubscribe|baja|bajas|"
    r"accessib\w*|accommodation\w*|arco|derechos\w*|security|seguridad|admin|administrator|root)"
    r"([-_.+].*)?$", re.I)
# Words that make a sentence an instruction to apply / send a CV.
_APPLY_CONTEXT = re.compile(
    r"\b(cv|c\.v\.?|curr[ií]cul\w*|candidaturas?|solicitud(es)?|resum[eé]s?|apply|applying|applications?|"
    r"aplica\w*|postul\w*|inscr[ií]b\w*|interesad\w*)\b", re.I)
# "... send/envía ... CV/candidatura ... to <address>": the address is explicitly the application contact.
_EXPLICIT_APPLY = re.compile(
    r"\b(env[ií]a\w*|enviar\w*|mand[ae]\w*|remit\w*|send|submit|e-?mail|forward)\b[^@\n]{0,80}"
    r"\b(cv|c\.v\.?|curr[ií]cul\w*|candidaturas?|solicitud|resum[eé]|applications?)\b[^@\n]{0,40}$", re.I)
_NO_EMAIL_APPLICATIONS = re.compile(
    r"\b(no (enviar|env[ií]es|env[ií]en|mandar|mandes|remitir)|do not (send|e-?mail)|don.?t (send|e-?mail)|"
    r"no se (aceptan|admiten|reciben)|(we )?(do not|don.?t|cannot) accept|not accept\w*)\b[^.\n]{0,80}"
    r"\b(cvs?|curr[ií]cul\w*|candidaturas?|applications?|resum[eé]s?|e-?mail|correo)\b", re.I)
_PRIVACY_CONTEXT = re.compile(
    r"protecci[oó]n de datos|data protection|privacy|privacidad|rgpd|gdpr|lopd|derechos? (de )?(acceso|arco)|"
    r"ejerc\w+ (sus|tus|los) derechos|delegado de protecci|data controller|responsable del? (tratamiento|datos)|"
    r"unsubscribe|darse de baja|accommodation|accessib|discapacidad|reasonable adjust|"
    r"solicitud de (acceso|rectificaci|supresi|oposici)", re.I)
_INJECTION_TEXT = re.compile(r"ignore (all |any )?(previous|prior|above) (instructions|rules)|system prompt|"
                             r"disregard (the |all )?(above|previous)|you are an? (ai|assistant|language model)", re.I)
_SENTENCE_END = re.compile(r"[.!?:;]\s+|\n")


def _email_domain_ok(email: str) -> bool:
    domain = email.rpartition("@")[2].lower()
    labels = domain.split(".")
    return (len(labels) >= 2 and all(labels) and labels[-1].isalpha() and not domain.startswith("xn--")
            and not any(lab.startswith("xn--") for lab in labels)
            and not re.fullmatch(r"[\d.]+", domain) and len(email) <= 120)


def find_application_email(job: Dict, recruiter_email: str = "") -> str:
    """The address the posting itself names as the place to send an application.

    An address is accepted only when its own sentence is an instruction to
    apply / send a CV. Privacy, legal, support and no-reply mailboxes are
    accepted only when the sentence explicitly says to send the CV to them, and
    never inside a data-protection sentence. A posting that says not to apply
    by email, that carries prompt-injection text, or that names several
    different application addresses yields "" (-> MANUAL_REQUIRED).
    A recruiter_email set on the application is used only if the posting
    confirms it. Nothing is ever guessed."""
    text = job.get("description") or ""
    if _NO_EMAIL_APPLICATIONS.search(text) or _INJECTION_TEXT.search(text):
        return ""
    accepted, explicit = [], []
    for m in _EMAIL_RE.finditer(text):
        email = m.group(0).strip().rstrip(".")
        if _BAD_EMAIL.search(email) or is_placeholder_email(email) or not _email_domain_ok(email):
            continue
        before = text[max(0, m.start() - 220):m.start()]
        pieces = _SENTENCE_END.split(before)
        sentence = pieces[-1]
        if len(sentence.strip(" *_-•\t")) < 12 and len(pieces) > 1:
            sentence = pieces[-2] + " " + sentence  # "Interesados:\n<address>"
        tail = _SENTENCE_END.split(text[m.end():m.end() + 80])[0]
        if _PRIVACY_CONTEXT.search(sentence):
            continue
        is_explicit = bool(_EXPLICIT_APPLY.search(sentence))
        if _ROLE_MAILBOX.match(email.rpartition("@")[0]):
            # Role mailboxes: explicit "send your CV to" wording, and no data-protection
            # context anywhere nearby ("Protección de datos: envía tu solicitud a gdpr@...").
            if not is_explicit or _PRIVACY_CONTEXT.search(before[-160:]):
                continue
        elif not (_APPLY_CONTEXT.search(sentence) or _APPLY_CONTEXT.search(tail)):
            continue
        if email.lower() not in [a.lower() for a in accepted]:
            accepted.append(email)
            if is_explicit:
                explicit.append(email)
    wanted = (recruiter_email or "").strip().lower()
    for email in accepted:
        if email.lower() == wanted:
            return email
    if len(accepted) == 1:
        return accepted[0]
    if len(explicit) == 1:
        return explicit[0]
    return ""  # none, or several candidates with no single explicit one: a human decides


def _host(url: str) -> str:
    url = (url or "").strip()
    return urlparse(url).netloc.lower().split(":")[0] if url.startswith(("http://", "https://")) else ""


def ats_for_url(url: str) -> str:
    host = _host(url)
    return next((name for name, rx in ATS_HOST_PATTERNS if host and rx.search(host)), "")


def is_job_board(url: str) -> bool:
    host = _host(url)
    return bool(host) and any(h in host for h in JOB_BOARD_HOSTS)


def is_login_walled(url: str) -> bool:
    host = _host(url)
    return bool(host) and any(h in host for h in LOGIN_WALLED_HOSTS)


def normalize_application_url(url: str, ats: str) -> str:
    """Point ATS posting URLs at the page that actually holds the form."""
    base = url.split("#")[0]
    path = urlparse(base).path.rstrip("/")
    if ats == "lever" and not path.endswith("/apply"):
        return base.split("?")[0].rstrip("/") + "/apply"
    if ats == "workable" and "/j/" in path and not path.endswith("/apply"):
        return base.split("?")[0].rstrip("/") + "/apply/"
    if ats == "ashby" and path.count("/") >= 2 and not path.endswith("/application"):
        return base.split("?")[0].rstrip("/") + "/application"
    return base


_URL_RE = re.compile(r"https?://[^\s\"'<>()\[\]{}|\\^`]+", re.I)
_REDIRECT_PARAMS = ("url", "u", "dest", "destination", "redirect", "redirect_url", "redirecturl",
                    "target", "applyurl", "apply_url", "to", "link")
_APPLY_PATH = re.compile(r"apply|application|aplicar|candidat|inscri|careers?|/jobs?/|empleo|"
                         r"trabaja|vacante|oferta|job-?offer|recruit|solicitud", re.I)
# Board links that redirect to the employer's site ("apply on company website").
_BOARD_APPLY_REDIRECT = re.compile(r"/(applystart|rc/clk|pagead/clk|jobs/view/externalApply)", re.I)
_ASSET = re.compile(r"\.(png|jpe?g|gif|svg|css|js|ico|woff2?|webp)(\?|$)", re.I)


def _unwrap(url: str, depth: int = 3) -> str:
    """Follow redirect wrappers like ...?url=https%3A%2F%2Femployer... (no network)."""
    url = html.unescape(url or "").strip().rstrip(".,;:'\"")
    for _ in range(depth):
        params = {k.lower(): v for k, v in parse_qsl(urlparse(url).query)}
        nxt = next((unquote(params[k]) for k in _REDIRECT_PARAMS
                    if unquote(params.get(k, "")).lower().startswith(("http://", "https://"))), "")
        if not nxt:
            break
        url = nxt
    return url


def _routes_in_text(text: str, source: str, *, employer_ok: bool) -> List[Dict]:
    """ATS (and optionally employer apply) URLs found in text/HTML, ATS first."""
    ats_routes, employer_routes, seen = [], [], set()
    for raw in _URL_RE.findall(html.unescape(text or "")):
        url = _unwrap(raw)
        if url in seen or not _host(url) or _ASSET.search(url):
            continue
        seen.add(url)
        ats = ats_for_url(url)
        if ats:
            ats_routes.append({"route_type": ats, "url": normalize_application_url(url, ats), "source": source})
        elif employer_ok and not is_job_board(url) and _APPLY_PATH.search(urlparse(url).path):
            employer_routes.append({"route_type": EMPLOYER, "url": url, "source": source})
    return ats_routes + employer_routes


def _http_fetch(url: str):
    """GET a public page (follows public redirects). Returns (final_url, html) or None.
    No cookies, no login, no anti-bot workarounds: a blocked page just yields None."""
    ok, why = safety.resolves_to_public(url)
    if not ok:  # never fetch localhost / private addresses named in untrusted posting text
        logger.warning("Route discovery refused to fetch %s: %s", url, why)
        return None
    try:
        import requests
        resp = requests.get(url, timeout=12, allow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"})
        if resp.status_code != 200 or not safety.is_safe_public_url(resp.url):
            return None
        return resp.url, resp.text
    except Exception as e:  # network errors: route stays undiscovered
        logger.info("Route discovery fetch failed for %s: %s", url, e)
        return None


def _web_search(query: str, max_results: int = 8) -> List[str]:
    """Result URLs of one public web search (see employer_routes)."""
    from employer_routes import web_search
    return web_search(query, max_results)


def _route(url: str, source: str, reason: str) -> Dict:
    ats = ats_for_url(url)
    return {"route_type": ats or EMPLOYER, "url": normalize_application_url(url, ats),
            "source": source, "reason": reason.format(kind=ats or "employer")}


def discover_application_route(job: Dict, fetch: Optional[Callable] = None) -> Dict:
    """Find where the application form actually lives.

    Order: job URL on a public ATS -> apply URL stored from the board -> ATS /
    employer apply links in the description -> (board postings only, when
    `fetch` is given) the public job page: a redirect off the board, ATS links
    in it, or the board's "apply on company site" redirect link.
    Never logs in or bypasses walls. Returns {route_type, url, source, reason};
    route_type/url are "" when no public route exists.
    """
    job_url = (job.get("url") or "").strip()
    if ats_for_url(job_url):
        return _route(job_url, "job_url", "{kind} ATS form")
    apply_url = _unwrap(job.get("apply_url") or "")
    if _host(apply_url) and not is_job_board(apply_url):
        return _route(apply_url, "apply_url", "{kind} application URL exposed by the board")
    routes = _routes_in_text(job.get("description") or "", "description", employer_ok=True)
    if routes:
        return {**routes[0], "reason": f"{routes[0]['route_type']} application URL in the posting"}
    if fetch and is_job_board(job_url):
        page = fetch(job_url)
        if page:
            final_url, page_html = page
            if _host(final_url) and not is_job_board(final_url):
                return _route(final_url, "job_page_redirect", "Board posting redirects to the {kind} page")
            routes = _routes_in_text(page_html, "job_page", employer_ok=False)
            if routes:
                return {**routes[0], "reason": f"{routes[0]['route_type']} application URL on the public job page"}
            for raw in _URL_RE.findall(html.unescape(page_html or "")):
                if is_job_board(raw) and _BOARD_APPLY_REDIRECT.search(raw):
                    resolved = fetch(raw)
                    if resolved and _host(resolved[0]) and not is_job_board(resolved[0]):
                        return _route(resolved[0], "apply_redirect",
                                      "Board 'apply on company site' link resolves to the {kind} page")
    return {"route_type": "", "url": "", "source": "", "reason": ""}


def url_names_employer(url: str, company: str) -> bool:
    """The URL itself carries the employer's name: in the host
    (empleo.mercadona.es, acme.teamtailor.com) or in the path of an ATS / careers
    platform (jobs.lever.co/acme/..., join.com/companies/acme/...)."""
    from employer_routes import _GENERIC_COMPANY, _company_tokens, _host_is_employer, is_unnamed
    if is_unnamed(company):
        return False
    if _host_is_employer(url, company):
        return True
    # A name in the path only counts on multi-tenant ATS/careers platforms, where
    # the path segment identifies the tenant; on any other host it proves nothing.
    host = _host(url)
    if not (ats_for_url(url) or any(host == h or host.endswith("." + h) for h in CAREER_PLATFORM_HOSTS)):
        return False
    path = re.sub(r"[^a-z0-9]", "", urlparse(url).path.lower())
    tokens = _company_tokens(company)
    slug = "".join(tokens)
    if len(slug) >= 5 and slug in path:
        return True
    return any(len(t) >= 5 and t not in _GENERIC_COMPANY and t in path for t in tokens)


def verify_route(job: Dict, route: Dict, fetch: Optional[Callable] = None) -> str:
    """'' when the WEB route may be used, else why not.

    The URL must be a safe public http(s) URL, and must belong to this employer
    and vacancy: it is the posting itself, or its host/path names the employer,
    or the fetched page is verifiably this vacancy (job title + employer +
    location/reference/text overlap). A URL that merely appears in untrusted
    posting text is never enough."""
    url = route.get("url") or ""
    ok, why = safety.check_public_url(url)
    if not ok:
        return f"unsafe application URL ({why})"
    if is_job_board(url):
        return "application URL is a job board, not the employer/ATS"
    if route.get("source") in ("job_url", "employer_search"):
        return ""  # the posting's own page / already verified by employer_routes
    if url_names_employer(url, job.get("company") or ""):
        return ""
    if fetch:
        page = fetch(url)
        if page and safety.is_safe_public_url(page[0]) and not is_job_board(page[0]):
            from employer_routes import _same_vacancy
            _, missing = _same_vacancy(job, page[0], page[1])
            if not missing:
                return ""
    return ("ownership of the application URL could not be established (its host/path does not name "
            "the employer and the page is not verifiably this vacancy)")


def detect_application_method(job: Dict, recruiter_email: str = "", fetch: Optional[Callable] = None,
                              search: Optional[Callable] = None) -> Dict:
    """Return {method: WEB|EMAIL|MANUAL_REQUIRED, ats, route_type, url, source, email, reason}.

    `fetch` (url -> (final_url, html) | None) enables looking inside job-board
    postings; without it no network is used. With `search`
    ((query, max_results) -> [url]) as well, a job that would otherwise be
    MANUAL_REQUIRED gets one bounded employer route search first
    (employer_routes.discover_employer_route). Every WEB route must pass
    verify_route(); an unverified URL is never used.
    """
    url = (job.get("url") or "").strip()
    email = find_application_email(job, recruiter_email)
    route = discover_application_route(job, fetch=fetch)
    rejected = ""
    if route["url"]:
        rejected = verify_route(job, route, fetch=fetch)
        if not rejected:
            return {"method": WEB, "ats": route["route_type"], "email": email, **route}
        rejected = f"Application URL {route['url']} rejected: {rejected}. "
        logger.warning("Route rejected for %s: %s", job.get("title"), rejected)
    if email:
        return {"method": EMAIL, "ats": "", "route_type": "email", "url": "", "source": "description",
                "email": email, "reason": "Application email in posting"}
    if _host(url) and not is_job_board(url) and not rejected:
        generic = {"route_type": EMPLOYER, "url": url, "source": "job_url"}
        problem = verify_route(job, generic)
        if not problem:
            return {"method": WEB, "ats": "generic", **generic, "email": "",
                    "reason": "Generic web form (attempted; stops as MANUAL_REQUIRED if unsupported)"}
        rejected = f"Job URL rejected: {problem}. "
    reason = rejected + (
        "No public application route: the job-board posting (LinkedIn/Indeed/...) exposes no "
        "employer/ATS application URL and no application email" if _host(url)
        else "No application URL or email")
    employer = ""
    if fetch and search:
        # Last resort: the same vacancy on the employer's own site / a public ATS.
        from employer_routes import discover_employer_route
        found = discover_employer_route(job, search=search, fetch=fetch)
        if found["url"]:
            return {"method": WEB, "ats": found["route_type"], "email": "", **found}
        reason, employer = f"{reason}. {found['reason']}", found["employer"]
    return {"method": MANUAL_REQUIRED, "ats": "", "route_type": "", "url": "", "source": "", "email": "",
            "employer": employer, "reason": reason}


def _save_resolved_employer(job_url: str, employer: str, db: Path) -> None:
    """Record an employer verified from the public posting. Only a placeholder
    ("Unknown"/empty) is replaced; title, URL, board and ids are untouched."""
    if not employer:
        return
    conn = storage.get_db(db)
    conn.execute(f"UPDATE jobs SET company = ? WHERE url = ? AND LOWER(TRIM(COALESCE(company, ''))) "
                 f"IN ({','.join('?' for _ in _NO_EMPLOYER)})", (employer, job_url, *_NO_EMPLOYER))
    conn.commit()
    conn.close()


# --- Preparation -------------------------------------------------------------

def prepare_application(job: Dict, profile: Dict, *, matcher=None,
                        db_path: Optional[Path] = None, fetch: Optional[Callable] = None,
                        search: Optional[Callable] = None) -> Dict:
    """Qualify a job and, only if QUALIFIED, prepare its application package.

    Returns {"status", "app_id", "reasons", "method"}; status is the job's
    qualification status when not qualified, or the application status.
    """
    db = _db(db_path)
    existing = storage.find_existing_application(
        job["url"], job.get("title") or "", job.get("company") or "", location=job.get("location") or "",
        description=job.get("description") or "", db_path=db)
    if existing:
        # A route-less MANUAL_REQUIRED application is re-checked for a public
        # employer/ATS route and updated in place; anything else (e.g.
        # READY_TO_SUBMIT) is left untouched and reported as a duplicate.
        if needs_route(existing) and _job_qualified(existing["job_url"], db):
            return reroute_application(existing, dict(job, url=existing["job_url"]), profile=profile,
                                       db_path=db, fetch=fetch or _http_fetch, search=search, matcher=matcher)
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
        email_subject=application_subject(job, profile),
        email_body=letter,
    )

    method = detect_application_method(job, fetch=fetch or _http_fetch, search=search or _web_search)
    _save_resolved_employer(job["url"], method.get("employer", ""), db)
    status = MANUAL_REQUIRED if method["method"] == MANUAL_REQUIRED else READY_TO_SUBMIT
    problems = letter_problems(letter, profile)
    if problems:  # never READY with content that is not grounded in the profile
        status = MANUAL_REQUIRED
        method = dict(method, reason=f"Application content not grounded: {'; '.join(problems)}. {method['reason']}")
    storage.update_application(
        app_id, db_path=db, status=status, application_method=method["method"],
        recruiter_email=method["email"], status_reason=method["reason"],
        application_url=method["url"], route_type=method["route_type"], route_source=method["source"],
    )
    logger.info("Prepared application #%s (%s, %s) for %s at %s", app_id, status,
                method["method"], job.get("title"), job.get("company"))
    return {"status": status, "app_id": app_id, "reasons": [method["reason"]], "method": method["method"]}


def needs_route(app: Dict) -> bool:
    """MANUAL_REQUIRED only because no application route was known."""
    return (app.get("status") == MANUAL_REQUIRED
            and (app.get("application_method") or MANUAL_REQUIRED) == MANUAL_REQUIRED
            and not (app.get("application_url") or "").strip())


def _job_qualified(job_url: str, db: Path) -> bool:
    conn = storage.get_db(db)
    row = conn.execute("SELECT qualification_status FROM jobs WHERE url = ?", (job_url,)).fetchone()
    conn.close()
    return bool(row) and row[0] == QUALIFIED


def reroute_application(app: Dict, job: Dict, *, profile: Optional[Dict] = None,
                        db_path: Optional[Path] = None, fetch: Optional[Callable] = None,
                        search: Optional[Callable] = None, matcher=None) -> Dict:
    """Re-run route discovery for an existing route-less MANUAL_REQUIRED
    application and update it in place. Uses the job URL, the posting
    description, the board apply URL and any URL already saved on the
    application, then the bounded employer route search.
    A found route makes it READY_TO_SUBMIT only while the
    recorded fixed CV is still byte-identical; otherwise it stays
    MANUAL_REQUIRED with the reason recorded. Nothing is submitted or sent.
    Returns the same shape as prepare_application."""
    db = _db(db_path)
    app_id = app["id"]
    if profile is not None:
        # The job was qualified under the rules of that day; it must still pass today's.
        q = qualify_job(job, profile, matcher=matcher)
        if q.status != QUALIFIED:
            storage.set_job_qualification(app["job_url"], q.status, q.category, q.reasons, db_path=db)
            reason = f"No longer passes qualification ({q.status}): {'; '.join(q.reasons)}"
            storage.update_application(app_id, db_path=db, status_reason=reason)
            return {"status": MANUAL_REQUIRED, "app_id": app_id,
                    "reasons": [f"Existing application #{app_id}: {reason}"], "method": MANUAL_REQUIRED}
    job = dict(job, apply_url=job.get("apply_url") or app.get("application_url") or "")
    method = detect_application_method(job, fetch=fetch or _http_fetch, search=search or _web_search)
    _save_resolved_employer(app["job_url"], method.get("employer", ""), db)
    if method["method"] == MANUAL_REQUIRED:
        reason = f"Route re-check: {method['reason']}"
        storage.update_application(app_id, db_path=db, status_reason=reason)
        return {"status": MANUAL_REQUIRED, "app_id": app_id,
                "reasons": [f"Existing application #{app_id} re-checked; still MANUAL_REQUIRED: {method['reason']}"],
                "method": MANUAL_REQUIRED}

    # Same fixed CV as when the application was prepared, byte for byte.
    cv_path = Path(app.get("cv_pdf_path") or "")
    cv_problem = ""
    if not cv_path.is_file() or not app.get("cv_sha256") or sha256_file(cv_path) != app["cv_sha256"]:
        cv_problem = "fixed CV missing or its SHA-256 no longer matches the recorded checksum"
    elif profile is not None and cv_path.resolve() != resolve_fixed_cv_path(profile).resolve():
        cv_problem = "recorded CV is not the configured fixed CV"
    status = MANUAL_REQUIRED if cv_problem else READY_TO_SUBMIT
    reason = (f"Route found ({method['reason']}) but {cv_problem}" if cv_problem
              else f"Re-routed: {method['reason']}")
    storage.update_application(
        app_id, db_path=db, status=status, application_method=method["method"],
        recruiter_email=method["email"], status_reason=reason,
        application_url=method["url"], route_type=method["route_type"], route_source=method["source"])
    logger.info("Re-routed application #%s -> %s (%s %s)", app_id, status, method["route_type"], method["url"])
    return {"status": status, "app_id": app_id,
            "reasons": [f"Existing application #{app_id}: {reason}"], "method": method["method"]}


def reroute_manual_applications(db_path: Optional[Path] = None, fetch: Optional[Callable] = None,
                                search: Optional[Callable] = None, profile: Optional[Dict] = None,
                                matcher=None) -> int:
    """Re-run route discovery for QUALIFIED jobs whose application is MANUAL_REQUIRED
    only because no route was known. A found route makes it READY_TO_SUBMIT.
    With `profile`, each job is first re-qualified under the current rules.
    Nothing is submitted. Returns the number of applications re-routed."""
    db = _db(db_path)
    conn = storage.get_db(db)
    rows = [dict(r) for r in conn.execute(
        """SELECT j.*, a.id AS app_id FROM applications a JOIN jobs j ON a.job_url = j.url
           WHERE a.status = ? AND j.qualification_status = ?""",
        (MANUAL_REQUIRED, QUALIFIED))]
    conn.close()
    rerouted = 0
    for row in rows:
        app = storage.get_application_by_job(row["url"], db_path=db)
        if not app or app["id"] != row["app_id"] or not needs_route(app):
            continue
        if reroute_application(app, row, profile=profile, db_path=db, fetch=fetch, search=search,
                               matcher=matcher)["status"] == READY_TO_SUBMIT:
            rerouted += 1
    return rerouted
