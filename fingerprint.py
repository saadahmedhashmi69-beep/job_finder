"""Canonical job identity: text / URL normalisation, vacancy fingerprint, slugs.

Two postings are the same vacancy when their canonical URLs are equal, or when
normalised title + employer + city are equal. City is part of the key, so the
same role at the same employer in another city stays a separate vacancy.
Postings without a named employer ("Unknown") additionally carry a digest of
their description, so different anonymous employers are never merged.
Normalisation is done in Python only (SQLite LOWER() does not fold accents).
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from typing import Iterable, List
from urllib.parse import parse_qsl, urlencode, urlparse

logger = logging.getLogger(__name__)

_TRACKING_PARAM = re.compile(
    r"^(utm_.*|fbclid|gclid|gbraid|wbraid|msclkid|mc_[a-z]+|trk|trkinfo|trackingid|refid|ref|referrer|"
    r"referer|from|source|src|vjs|tk|advn|adid|sponsored|xkcb|atk|position|pagenum|pid|cmp|campaign|spm|"
    r"_ga|_gl|igshid|s_cid|origin|lipi|eid|ebp|sk|currentjobid|alid|rcm)$", re.I)
_GENDER_MARK = re.compile(r"\((?:[hmfdxw]\s*/\s*)+[hmfdxw]\)|\b[hmfdxw]/[hmfdxw](?:/[hmfdxw])?\b|"
                          r"\s*/\s*(?:as?|es|ora|oras)\b|\(\s*(?:as?|es|ora)\s*\)", re.I)
_LEGAL_FORMS = {"sl", "slu", "sa", "sau", "sll", "slp", "ltd", "inc", "llc", "gmbh", "bv", "plc", "srl",
                "co", "kg", "cia", "scp", "scl", "coop"}
NO_COMPANY = {"", "unknown", "confidential", "confidencial", "empresa confidencial", "n a", "na", "none",
              "anonimo", "anonymous"}


def normalize_text(text) -> str:
    """Accent-folded, case-folded, punctuation- and whitespace-collapsed text."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def normalize_company(company) -> str:
    tokens = normalize_text(company).split()
    # Drop legal forms ("S.L." -> "s l", "SA", "GmbH"); keep digits ("Grupo 7").
    kept = [t for t in tokens if t not in _LEGAL_FORMS and (len(t) > 1 or t.isdigit())]
    return " ".join(kept or tokens)


def is_unnamed_company(company) -> bool:
    return normalize_text(company) in NO_COMPANY


def normalize_city(location) -> str:
    return normalize_text(str(location or "").split(",")[0])


def normalize_title(title, location="") -> str:
    """Title without gender markers ("/a", "(H/M/X)") or a city the board appended."""
    norm = normalize_text(_GENDER_MARK.sub(" ", str(title or "")))
    # Boards that strip the slash leave the marker as loose tokens ("conductor a",
    # "repartidor h m x"): drop "a"/"as" and runs of gender initials. Other single
    # letters ("conductor c" vs "conductor d": licence classes) are kept.
    tokens, kept = norm.split(), []
    for i, tok in enumerate(tokens):
        if tok in ("a", "as"):
            continue
        if len(tok) == 1 and tok in "hmfdxw":
            before = i > 0 and len(tokens[i - 1]) == 1 and tokens[i - 1] in "hmfdxw"
            after = i + 1 < len(tokens) and len(tokens[i + 1]) == 1 and tokens[i + 1] in "hmfdxw"
            if before or after:
                continue
        kept.append(tok)
    norm = " ".join(kept)
    city = normalize_city(location)
    if city and len(city) > 2 and norm.endswith(" " + city):
        norm = norm[: -len(city)].strip()
    return norm


def canonical_url(url) -> str:
    """Stable identity of a posting URL: host lower-cased without "www.", no
    scheme/fragment/trailing slash, tracking parameters dropped, remaining
    parameters sorted. Indeed and LinkedIn collapse to their native job id."""
    url = str(url or "").strip()
    try:
        parts = urlparse(url)
    except ValueError:
        return url.lower()
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return url.lower()
    host = parts.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    path = re.sub(r"/+$", "", parts.path)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if "indeed." in host:
        jk = next((v for k, v in query if k.lower() == "jk" and v), "")
        if jk:
            return f"indeed:{jk.lower()}"
    if "linkedin." in host:
        m = re.search(r"/jobs/view/(?:[^/?#]*?-)?(\d{6,})$", path)
        if m:
            return f"linkedin:{m.group(1)}"
    kept = sorted((k, v) for k, v in query if not _TRACKING_PARAM.match(k))
    return f"{host}{path}" + (f"?{urlencode(kept)}" if kept else "")


def job_fingerprint(title, company, location="", description="") -> str:
    """title | employer | city. Unnamed employers add a digest of the posting text."""
    comp = normalize_company(company)
    if is_unnamed_company(company):
        digest = hashlib.sha1(normalize_text(description)[:600].encode("utf-8")).hexdigest()[:12]
        comp = f"unnamed:{digest}"
    return f"{normalize_title(title, location)}|{comp}|{normalize_city(location)}"


def sanitize_job(job) -> bool:
    """Coerce missing text fields to "" in place. False when the job is unusable
    (no title, or a URL that is not http/https) and must be dropped."""
    for attr in ("title", "company", "location", "url", "description", "salary", "date_posted",
                 "job_type", "apply_url"):
        value = getattr(job, attr, "")
        if not isinstance(value, str):
            setattr(job, attr, "" if value is None else str(value))
    job.title, job.url = job.title.strip(), job.url.strip()
    return bool(job.title) and job.url.lower().startswith(("http://", "https://"))


def dedupe_jobs(jobs: Iterable) -> List:
    """Drop unusable jobs and in-batch duplicates (canonical URL or fingerprint)."""
    seen_urls, seen_fps, unique, malformed = set(), set(), [], 0
    for job in jobs:
        try:
            if not sanitize_job(job):
                malformed += 1
                continue
            cu = canonical_url(job.url)
            fp = job_fingerprint(job.title, job.company, job.location, job.description)
        except Exception as e:  # one broken row never aborts the batch
            malformed += 1
            logger.warning("Dropped malformed job: %s", e)
            continue
        if cu in seen_urls or fp in seen_fps:
            continue
        seen_urls.add(cu)
        seen_fps.add(fp)
        unique.append(job)
    if malformed:
        logger.warning("Dropped %d malformed job(s) (no title / non-http URL)", malformed)
    return unique


def _slugify(text: str) -> str:
    text = normalize_text(text)
    return re.sub(r"\s+", "-", text)


def unique_slug(company, title, url) -> str:
    """Readable slug that cannot collide: a digest of the posting URL is appended,
    so long identical prefixes never share (and overwrite) an application folder."""
    base = _slugify(f"{company or 'unknown'}-{title or ''}")[:48].strip("-") or "application"
    digest = hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()[:10]
    return f"{base}-{digest}"
