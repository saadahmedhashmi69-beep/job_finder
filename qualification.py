"""Hard qualification gate between matching and application preparation.

A match score only ranks jobs; it never qualifies one. A job is QUALIFIED only
when the matcher's hard rules (category title gates, evidence rules, blocking
requirements — see matcher.py and profile.yaml) pass with no flags, the job is
in Spain, and the category-specific checks below find positive evidence.
Anything uncertain becomes NEEDS_REVIEW; anything out of scope becomes REJECTED.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from matcher import JobMatcher, normalize_for_match, _SPAIN_LOCATION
from models import Job, JobBoard

# --- Job / application state model -----------------------------------------
DISCOVERED = "DISCOVERED"
MATCHED = "MATCHED"
REJECTED = "REJECTED"
NEEDS_REVIEW = "NEEDS_REVIEW"
QUALIFIED = "QUALIFIED"
APPLICATION_PREPARED = "APPLICATION_PREPARED"
READY_TO_SUBMIT = "READY_TO_SUBMIT"
SUBMITTED = "SUBMITTED"
SUBMISSION_FAILED = "SUBMISSION_FAILED"
MANUAL_REQUIRED = "MANUAL_REQUIRED"

APPLICATION_STATES = (APPLICATION_PREPARED, READY_TO_SUBMIT, SUBMITTED,
                      SUBMISSION_FAILED, MANUAL_REQUIRED)

# Spain: country name, or Indeed-style "City, XX, ES" country code.
_SPAIN_TEXT = re.compile(r"\b(spain|espana|espanya|barcelona|madrid|valencia|sevilla|"
                         r"malaga|bilbao|zaragoza|catalunya|cataluna)\b")

# Fashion: genuine design role + women's clothing evidence.
_DESIGN_ROLE = re.compile(r"\b(design(er)?s?|disenador(a|es)?|dissenyador(a)?|diseno|disseny)\b")
_WOMENSWEAR = re.compile(r"\b(women|womens|woman|ladies|lady|womenswear|ladieswear|mujer|mujeres|"
                         r"senora|senoras|femenin[oa]s?|dona|dones)\b|\bwomen.?s\b")

# Driver: actual vehicle / passenger / delivery-driving evidence.
_VEHICLE_EVIDENCE = re.compile(
    r"\b(coche|car|cars|turismo|furgoneta|furgonetas|vehiculo|vehiculos|vehicle|vehicles|"
    r"taxi|vtc|pasajeros?|passengers?|carnet (de conducir|b)|carne (de conducir|b)|"
    r"permiso (de conducir|b)|driving licen[cs]e|driver.?s licen[cs]e|conducir|conduccion|conduir)\b"
    # "van" alone is also Spanish for "they go": only English van phrases count.
    r"|\b(delivery|company|cargo|work) vans?\b|\bvan (driver|delivery|driving)\b")
# Professional/authorisation licences the CV does not list (it lists Pakistan &
# Oman licences only). These never auto-qualify; a human must confirm eligibility.
# ("cap" alone is Catalan for "none", so only CAP-certificate phrases count.)
_PROFESSIONAL_LICENCE = re.compile(
    r"\b(taxi|vtc|btp|cap (de )?(mercancias|viajeros|vigente)|(certificado|tarjeta) cap|tacografo|tachograph|adr|ambulancias?|autobus|autocar|bus|camion|"
    r"camio|truck|lorry|trailer|hgv|pcv|carnet (c|c1|d|d1)|carne (c|c1|d|d1)|permiso (c|c1|d|d1)|"
    r"transporte adaptado|transport adaptat|licencia municipal)\b|c ?\+ ?e\b")


@dataclass
class QualificationResult:
    status: str
    category: str = ""
    reasons: List[str] = field(default_factory=list)
    match_score: float = 0.0

    @property
    def qualified(self) -> bool:
        return self.status == QUALIFIED

    def to_dict(self) -> Dict:
        return {"status": self.status, "category": self.category,
                "reasons": list(self.reasons), "match_score": self.match_score}


def is_spain(location: str) -> bool:
    location = location or ""
    return bool(_SPAIN_LOCATION.search(location) or _SPAIN_TEXT.search(normalize_for_match(location)))


def _job_from_row(job: Dict) -> Job:
    try:
        board = JobBoard(job.get("board") or "indeed")
    except ValueError:
        board = JobBoard.INDEED
    return Job(title=job.get("title") or "", company=job.get("company") or "",
               location=job.get("location") or "", url=job.get("url") or "",
               board=board, description=job.get("description") or "",
               date_posted=job.get("date_posted") or "", job_type=job.get("job_type") or "")


def qualify_job(job: Dict, profile: Dict, matcher: Optional[JobMatcher] = None) -> QualificationResult:
    """Apply the hard qualification rules to a stored job row (dict).

    The job is re-scored with the current matcher so stale match_details from
    older rule versions can never qualify a job.
    """
    matcher = matcher or JobMatcher(profile)
    score, details = matcher.score(_job_from_row(job))
    category = details.get("category") or ""
    result = QualificationResult(status=QUALIFIED, category=category, match_score=score)

    if details.get("rejected_reason"):
        return QualificationResult(REJECTED, "", [details["rejected_reason"]], score)
    if not category:
        return QualificationResult(REJECTED, "", ["Not a target role (no category)"], score)
    if not is_spain(job.get("location") or ""):
        return QualificationResult(REJECTED, category,
                                   [f"Not in Spain (location: {job.get('location') or 'unknown'})"], score)

    title_norm = normalize_for_match(job.get("title") or "")
    text_norm = normalize_for_match(f"{job.get('title') or ''} {job.get('description') or ''}")
    reasons = list(details.get("requirement_flags") or [])

    if category == "fashion":
        if not _DESIGN_ROLE.search(title_norm):
            return QualificationResult(REJECTED, category, ["Title is not a fashion design role"], score)
        if not _WOMENSWEAR.search(text_norm):
            reasons.append("No ladies/womenswear evidence")
    elif category == "driver":
        if not _VEHICLE_EVIDENCE.search(text_norm):
            reasons.append("No evidence of car/van/passenger vehicle driving")
        if _PROFESSIONAL_LICENCE.search(text_norm):
            reasons.append("Professional/authorisation licence role (taxi/VTC/BTP/C/D/CAP/"
                           "tachograph/adapted transport) - CV lists Pakistan & Oman licences only")
    else:
        return QualificationResult(REJECTED, category, [f"Unsupported category '{category}'"], score)

    if reasons:
        # De-duplicate while keeping order.
        result.status = NEEDS_REVIEW
        result.reasons = list(dict.fromkeys(reasons))
    return result
