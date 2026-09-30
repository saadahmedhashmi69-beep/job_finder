"""Employer route discovery: last step before an application is MANUAL_REQUIRED.

Used by application_prep.detect_application_method when the job-board posting
(LinkedIn/Indeed/...) exposes no application route of its own. A bounded public
web search looks for the SAME vacancy on the employer's own domain or a public
ATS, and a route is returned only when

  * the page is that vacancy: near-exact job title AND employer identity AND
    (location, vacancy reference or clear overlap with the posting text), and
  * the page (or the one public "Apply" link on it) shows an application form
    with a CV upload, with no login wall or CAPTCHA.

Only URLs returned by the search or linked from a verified page are used —
nothing is guessed. Pages are read with plain public GETs: no login, no
cookies, no anti-bot workarounds. Nothing is ever submitted or sent here.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Callable, Dict, List, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

import application_prep as prep
import submitter

logger = logging.getLogger(__name__)

SOURCE = "employer_search"
# Hard bounds per job: 2 searches, 5 vacancy pages (+1 apply page for a verified one).
MAX_QUERIES = 2
MAX_RESULTS_PER_QUERY = 8
MAX_CANDIDATES = 5
TITLE_RATIO = 0.88      # near-exact job title
OVERLAP_RATIO = 0.6     # share of the posting's distinctive words found on the page
MIN_OVERLAP_WORDS = 6

_LEGAL_SUFFIX = {"sl", "slu", "sa", "sau", "sll", "ltd", "inc", "llc", "gmbh", "bv", "plc", "srl", "co"}
# Words too common to identify an employer by its domain.
_GENERIC_COMPANY = {"grupo", "group", "spain", "espana", "empresa", "servicios", "services", "logistica",
                    "logistics", "transportes", "transport", "company", "international", "global", "iberia",
                    "trabajo", "temporal", "solutions", "soluciones", "jobs", "empleo", "careers"}
_NO_COMPANY = {"unknown", "confidential", "confidencial", "empresa confidencial", "n a", "na", "none"}
_GENDER_MARK =re.compile(r"\((?:[hmfdxw]\s*/\s*)+[hmfdxw]\)|\b[hmfdxw]/[hmfdxw](?:/[hmfdxw])?\b|/as?\b|\(a\)", re.I)
_TITLE_SEP = re.compile(r"\s+[|\-–—·:]\s+")
_REF_RE = re.compile(r"\b(?:ref(?:erencia|erence)?|job\s*id|req(?:uisition)?(?:\s*id)?|id\s+de\s+(?:la\s+)?oferta)\b"
                     r"[\s.:#nº°-]*([A-Z0-9][A-Z0-9/_-]{3,})", re.I)


def web_search(query: str, max_results: int = MAX_RESULTS_PER_QUERY) -> List[str]:
    """Result URLs of one public web search (DuckDuckGo). [] on any failure."""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            logger.warning("ddgs not installed — employer route search skipped (pip install ddgs)")
            return []
    try:
        return [(r.get("href") or "").strip() for r in DDGS().text(query, max_results=max_results) or []]
    except Exception as e:
        logger.info("Employer route search failed for %r: %s", query, e)
        return []


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _city(job: Dict) -> str:
    return (job.get("location") or "").split(",")[0].strip()


def _norm_title(title: str, city: str = "") -> str:
    """Comparable title: no gender markers, no trailing city ("... - Barcelona", "... (Málaga)")."""
    title, city = _norm(_GENDER_MARK.sub(" ", title or "")), _norm(city)
    return title[:-len(city)].strip() if city and title.endswith(" " + city) else title


def _search_title(job: Dict) -> str:
    """Job title as posted, minus a city the board appended to it."""
    title, city = (job.get("title") or "").strip(), _city(job)
    if city:
        title = re.sub(rf"\s*(?:[-–—|,]\s*|\(\s*){re.escape(city)}\s*\)?\s*$", "", title, flags=re.I)
    return title.strip()


def _company_tokens(company: str) -> List[str]:
    tokens = _norm(company).split()
    # Drop legal forms ("S.L." -> "s l", "SA", "GmbH"); keep names made only of initials.
    return [t for t in tokens if len(t) > 1 and t not in _LEGAL_SUFFIX] or tokens


def _host_is_employer(url: str, company: str) -> bool:
    """The host itself carries the employer's name (e.g. empleo.mercadona.es)."""
    labels = re.split(r"[.\-]", prep._host(url))
    tokens = _company_tokens(company)
    slug = "".join(tokens)
    if len(slug) >= 5 and slug in "".join(labels):
        return True
    distinctive = [t for t in tokens if len(t) >= 3 and t not in _GENERIC_COMPANY]
    return any(t in labels or (len(t) >= 5 and any(t in lab for lab in labels)) for t in distinctive)


def _is_candidate(url: str, company: str) -> bool:
    """Public employer-domain or ATS URL; never a job board, maps, asset or login page."""
    if not prep._host(url) or prep.is_job_board(url) or prep._ASSET.search(url) or submitter._LOGIN_URL.search(url):
        return False
    return bool(prep.ats_for_url(url)) or _host_is_employer(url, company)


def _queries(job: Dict) -> List[str]:
    company, title, city = (job.get("company") or "").strip(), _search_title(job), _city(job)
    return [f'"{company}" "{title}" {city}'.strip(), f"{company} {title} {city}".strip()][:MAX_QUERIES]


def _job_postings(soup: BeautifulSoup) -> List[Dict]:
    """schema.org JobPosting objects embedded in the page."""
    found = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            stack = [json.loads(tag.string or "")]
        except ValueError:
            continue
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if node.get("@type") == "JobPosting":
                    found.append(node)
                stack.append(node.get("@graph") or [])
    return found


def _read_page(page_html: str) -> Tuple[List[str], str]:
    """(title strings of the page, all visible text incl. JobPosting data)."""
    soup = BeautifulSoup(page_html or "", "html.parser")
    postings = _job_postings(soup)
    titles = [str(p.get("title") or "") for p in postings]
    og = soup.find("meta", attrs={"property": "og:title"})
    heads = [og.get("content") or "" if og else "", soup.title.get_text(" ", strip=True) if soup.title else ""]
    heads += [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
    for head in heads:
        titles += [head] + _TITLE_SEP.split(head)
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ") + " " + re.sub(r"<[^>]+>", " ", json.dumps(postings, ensure_ascii=False))
    return [t for t in titles if t.strip()], text


def _same_vacancy(job: Dict, url: str, page_html: str) -> Tuple[List[str], List[str]]:
    """(matched signals, missing signals). `missing` is empty only when the
    page is this vacancy: title + employer + one of location/reference/text overlap."""
    titles, raw_text = _read_page(page_html)
    text = f" {_norm(raw_text)} "
    matched, missing = [], []

    city = _city(job)
    want = _norm_title(job.get("title") or "", city)
    if len(want) >= 4 and any(want == t or SequenceMatcher(None, want, t).ratio() >= TITLE_RATIO
                              for t in (_norm_title(t, city) for t in titles) if t):
        matched.append("job title")
    else:
        missing.append("job title")

    company = job.get("company") or ""
    name, slug = " ".join(_company_tokens(company)), "".join(_company_tokens(company))
    if (_host_is_employer(url, company) or (name and f" {name} " in text)
            or (len(slug) >= 3 and slug in re.sub(r"[^a-z0-9]", "", urlparse(url).path.lower()))):
        matched.append("employer")
    else:
        missing.append("employer")

    extra = []
    city = _norm(city)
    if city and f" {city} " in text:
        extra.append("location")
    description = job.get("description") or ""
    refs = [r for r in _REF_RE.findall(description) if any(c.isdigit() for c in r)]
    if any(r.lower() in raw_text.lower() for r in refs):
        extra.append("vacancy reference")
    words = {w for w in _norm(description).split() if len(w) >= 5}
    if len(words) >= MIN_OVERLAP_WORDS and len(words & set(text.split())) / len(words) >= OVERLAP_RATIO:
        extra.append("description overlap")
    if extra:
        matched += extra
    else:
        missing.append("location / vacancy reference / description overlap")
    return matched, missing


class _StaticPage:
    """Fetched HTML behind the page interface of submitter's inspection helpers."""

    def __init__(self, url: str, page_html: str):
        self.url, self._html = url, page_html or ""
        self.soup = BeautifulSoup(self._html, "html.parser")

    def content(self) -> str:
        return self._html

    def evaluate(self, js: str) -> List[Dict]:
        if js != submitter._LINKS_JS:
            return []
        return [{"href": urljoin(self.url, a["href"]), "text": a.get_text(" ", strip=True)[:80]}
                for a in self.soup.find_all("a", href=True)]

    def has_cv_upload(self) -> bool:
        return any((i.get("type") or "").lower() == "file"
                   for form in self.soup.find_all("form") for i in form.find_all("input"))


def _public_form(url: str, page_html: str, fetch: Callable) -> Tuple[str, str]:
    """(URL of the public application form, '') or ('', why there is none).
    Looks at the vacancy page and at most one public 'Apply' link on it."""
    page = _StaticPage(url, page_html)
    blocked = submitter._blocked_reason(page)
    if blocked:
        return "", blocked.split(" - ")[0]
    if page.has_cv_upload():
        return url, ""
    link = submitter._apply_link(page, [url])

    def allowed(u: str) -> bool:
        return not prep.is_job_board(u) and (prep._host(u) == prep._host(url) or bool(prep.ats_for_url(u)))

    if not link or not allowed(link):
        return "", "it is not an application form and links to no public one"
    fetched = fetch(link)
    if not fetched:
        return "", f"its apply link {link} is not publicly reachable"
    final_url, apply_html = fetched
    apply_page = _StaticPage(final_url, apply_html)
    if not prep._host(final_url) or not allowed(final_url):
        return "", f"its apply link leads off the employer/ATS site ({final_url})"
    blocked = submitter._blocked_reason(apply_page)
    if blocked:
        return "", f"its apply page: {blocked.split(' - ')[0]}"
    if apply_page.has_cv_upload():
        return final_url, ""
    return "", "its apply page shows no public application form with a CV upload"


def discover_employer_route(job: Dict, *, search: Callable, fetch: Callable) -> Dict:
    """Find the same vacancy, with a public application form, on the employer's
    domain or a public ATS.

    `search(query, max_results) -> [url]`, `fetch(url) -> (final_url, html) | None`.
    Returns {route_type, url, source, reason}; route_type/url are "" (and
    `reason` says precisely why) when no verified route exists.
    """
    none = {"route_type": "", "url": "", "source": ""}
    company = (job.get("company") or "").strip()
    if (not _company_tokens(company) or _norm(company) in _NO_COMPANY
            or not (job.get("title") or "").strip()):
        return {**none, "reason": "Employer route search skipped: the posting does not name the employer "
                                  "or the job title"}

    candidates: List[str] = []
    searches = 0
    for query in _queries(job):
        searches += 1
        try:
            results = list(search(query, MAX_RESULTS_PER_QUERY) or [])[:MAX_RESULTS_PER_QUERY]
        except Exception as e:
            logger.info("Employer route search failed for %r: %s", query, e)
            results = []
        for raw in results:
            url = str(raw or "").strip()
            if url not in candidates and _is_candidate(url, company):
                candidates.append(url)
        if len(candidates) >= MAX_CANDIDATES:
            break
    candidates = sorted(candidates, key=lambda u: not prep.ats_for_url(u))[:MAX_CANDIDATES]
    if not candidates:
        return {**none, "reason": f"Employer route search: {searches} public web search(es) returned no "
                                  f"employer-domain or public ATS page for this vacancy"}

    rejected = []
    for url in candidates:
        page = fetch(url)
        if not page:
            rejected.append(f"{url} (not publicly reachable)")
            continue
        final_url, page_html = page
        if not _is_candidate(final_url, company):
            rejected.append(f"{url} (redirects to a job board, login or unrelated page)")
            continue
        matched, missing = _same_vacancy(job, final_url, page_html)
        if missing:
            rejected.append(f"{final_url} (not verified as the same vacancy: no match on {', '.join(missing)})")
            continue
        form_url, problem = _public_form(final_url, page_html, fetch)
        if not form_url:
            rejected.append(f"{final_url} (same vacancy, but {problem})")
            continue
        ats = prep.ats_for_url(form_url)
        logger.info("Employer route for %s at %s: %s", job.get("title"), company, form_url)
        return {"route_type": ats or prep.EMPLOYER, "url": form_url, "source": SOURCE,
                "reason": f"Same vacancy found on the {ats + ' ATS' if ats else 'employer site'} by public web "
                          f"search (matched {', '.join(matched)}); public application form with CV upload"}
    return {**none, "reason": f"Employer route search: checked {len(rejected)} candidate page(s), none is a "
                              f"verified public application route: " + "; ".join(rejected[:3])}
