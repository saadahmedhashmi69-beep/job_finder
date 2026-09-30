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
# SMTP accepted the email: delivery attempted, NOT a verified application.
SMTP_ACCEPTED = "SMTP_ACCEPTED"
# Legacy/test row claiming SUBMITTED without the current qualification/evidence contract.
UNVERIFIED_LEGACY = "UNVERIFIED_LEGACY"

# Claimed by exactly one submitter (storage.claim_application); outcome not recorded yet.
SUBMITTING = "SUBMITTING"

APPLICATION_STATES = (APPLICATION_PREPARED, READY_TO_SUBMIT, SUBMITTING, SUBMITTED, SMTP_ACCEPTED,
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


# --- Candidate eligibility (checked against profile.candidate_facts) -----------
# All patterns run on normalize_for_match() text: lower-case, accents folded.

# A driving licence is stated as a requirement ("carnet de conducir", "permiso B", "(B)"...).
_LICENCE_REQUIRED = re.compile(
    r"\b(?:carnet|carne|permiso|permis|permisos|licencia)s?\s+(?:de\s+)?(?:conducir|conduccion|conduir)\b"
    r"|\b(?:carnet|carne|permiso|permis|licencia)\s+(?:tipo\s+|clase\s+|categoria\s+)?\(?\s*b1?\s*\)?(?![a-z0-9+])"
    r"|\(\s*b\s*\)|\b(?:tipo|clase|categoria)\s+b\b"
    r"|\bdriving licen[cs]e\b|\bdriver.?s licen[cs]e\b|\bclean licen[cs]e\b|\bfull licen[cs]e\b")
# A specific professional class is named: C, C1, CE, C+E, C + E, C/E, D, D1 (also "conductor C").
_LICENCE_CLASS = re.compile(
    r"\b(?:carnet|carne|permiso|permis|licencia|licence|license)s?\s+(?:de\s+)?(?:(?:conducir|conduccion|conduir)\s+)?"
    r"(?:(?:tipo|clase|categoria)\s+)?\(?\s*(c\s*\+\s*e|c\s*/\s*e|ce|c1|c|d\s*\+\s*e|d1|d)\s*\)?(?![a-z0-9])"
    r"|\b(c\s*\+\s*e|c\s*/\s*e)\b"
    r"|\b(?:conductor|conductora|chofer|xofer|repartidor)(?:es|s)?\s+(?:de\s+)?(ce|c1|c|d)(?![a-z0-9])")
# Prior driving/delivery experience stated as a requirement (not "se valorará" / "no necesaria").
_EXPERIENCE_REQUIRED = re.compile(
    r"\b(?:minimo|minima|al menos)\s+(?:de\s+)?\d+\s+(?:anos?|meses)\b[^.\n]{0,30}\bexperiencia\b"
    r"|\b\d+\s+(?:anos?|meses)\s+de\s+experiencia\b"
    r"|\bexperiencia\s+(?:previa\s+|minima\s+|demostrable\s+|comprobable\s+)?(?:de\s+\d+\s+anos?\s+)?"
    r"(?:en|como|de)\s+(?:la\s+|el\s+)?(?:conduccion|conducir|reparto|repartidor\w*|conductor\w*|chofer|"
    r"manejo|transporte|puestos? similar(?:es)?|sector)\b"
    r"|\bcon experiencia\s+(?:en|como)\b|\bexperiencia\s+(?:imprescindible|requerida|necesaria|obligatoria)\b"
    r"|\b\d+\+?\s+years?(?:\s+of)?\s+experience\b|\bexperience\s+(?:is\s+)?required\b|\bprevious experience as\b")
_EXPERIENCE_OPTIONAL = re.compile(
    r"\bno\s+(?:es\s+)?(?:necesari\w+|imprescindible|requerid\w+)|\bsin\s+(?:necesidad de\s+)?experiencia|"
    r"se valorara|valorable|valoramos|no te preocupes|deseable|preferible|not required|no experience|"
    r"nice to have|a plus\b")
_OWN_VEHICLE = re.compile(
    r"\b(autonom[oa]s?|freelance|self.?employed|por cuenta propia|alta en autonomos|"
    r"(?:vehiculo|furgoneta|furgon|coche|moto|camion|transporte) propi[oa]|"
    r"propi[oa] (?:vehiculo|furgoneta|furgon|coche|moto|camion)|own (?:van|vehicle|car|motorbike))\b")
_MOTORBIKE_TITLE = re.compile(r"\b(moto|motos|motocicleta|motorbike|ciclomotor|scooter|bici|bicicleta|bike)\b")
# Title names another primary occupation (sales, warehouse, car washing, maintenance...).
_NON_DRIVING_ROLE = re.compile(
    r"\b(vendedor\w*|autoventa|preventa|comercial\w*|mozo|moza|mozos|almacen\w*|lavador\w*|lavacoches|"
    r"limpieza|mantenimiento|auxiliar|dependient\w*|operari\w*|peon|carretiller\w*|montador\w*|"
    r"instalador\w*|tecnic\w*|cociner\w*|camarer\w*|warehouse|cleaner|collector|stock)\b")
_INTERNSHIP = re.compile(r"\b(practicas|practiques|becari[oa]s?|beca|intern|interns|internship|trainee|"
                         r"aprendiz|apprentice\w*|estudiantes?)\b")
_PUBLIC_SECTOR = re.compile(
    r"\b(oposicion(?:es)?|funcionari[oa]s?|concurso[- ]oposicion|convocatoria publica|oferta publica de empleo|"
    r"bolsa de (?:trabajo|empleo) (?:publica|municipal)|regio policial|policia|mossos|guardia civil|"
    r"guardia urbana|ajuntament|ayuntamiento|generalitat|administracion publica|administracio publica|cido)\b"
    r"|^\s*pla[cz]a de\b")
# Strong signs the vacancy is in another country despite a Spain-looking location field.
_FOREIGN_POSTING = re.compile(
    r"£\s?\d|\bgbp\b|[\w-]+\.(?:co|org|ac|gov)\.uk\b|\bright to work in the (?:uk|united kingdom|us|usa)\b"
    r"|\b(?:based|located) in (?:the )?(?:uk|united kingdom|england|scotland|wales|ireland|usa|united states)\b")
_FOREIGN_COMPANY = re.compile(r"\b(uk|u k|usa)\b")
_LANGUAGES = {"spanish": r"espanol|castellano|spanish|lengua espanola", "catalan": r"catalan|catala|catalana"}
_LANG_LEVEL = (r"nivel|dominio|imprescindible|indispensable|requisito|requerid[oa]|necesari[oa]|obligatori[oa]|"
               r"fluid[oa]|fluidez|nativ[oa]|bilingue|alto|avanzado|hablar|hablado|fluent|native|proficien\w+|"
               r"required|must speak|c1|c2|b2")


def candidate_eligibility(profile: Dict) -> Dict:
    """Eligibility facts stated in the profile. Anything not stated is NOT assumed."""
    facts = (profile or {}).get("candidate_facts") or {}

    def lowered(key):
        return {normalize_for_match(str(x)).strip() for x in (facts.get(key) or []) if str(x).strip()}

    try:
        driving_years = float(facts.get("driving_experience_years") or 0)
    except (TypeError, ValueError):
        driving_years = 0.0
    return {"licences": lowered("licences_valid_in_spain"), "languages": lowered("languages"),
            "driving_experience_years": driving_years,
            "accepts_internships": facts.get("accepts_internships") is True,
            "own_vehicle": facts.get("has_own_vehicle") is True,
            "self_employed_ok": facts.get("accepts_self_employment") is True}


def _licence_classes(text_norm: str) -> List[str]:
    found = []
    for m in _LICENCE_CLASS.finditer(text_norm):
        cls = re.sub(r"[^a-z0-9]", "", next(g for g in m.groups() if g))
        found.append({"ce": "c+e", "de": "d+e"}.get(cls, cls))
    return list(dict.fromkeys(found))


def _experience_required(text_norm: str) -> bool:
    for m in _EXPERIENCE_REQUIRED.finditer(text_norm):
        start = max(text_norm.rfind(".", 0, m.start()), text_norm.rfind("\n", 0, m.start())) + 1
        ends = [i for i in (text_norm.find(".", m.end()), text_norm.find("\n", m.end())) if i != -1]
        sentence = text_norm[start:min(ends) if ends else len(text_norm)]
        if not _EXPERIENCE_OPTIONAL.search(sentence):
            return True
    return False


def eligibility_flags(job: Dict, category: str, profile: Dict) -> List[str]:
    """Reasons this vacancy cannot be auto-qualified for this candidate (empty = none).

    Conservative by design: a stated requirement the profile does not establish
    is a flag (-> NEEDS_REVIEW), never an assumption in the candidate's favour."""
    facts = candidate_eligibility(profile)
    title_norm = normalize_for_match(job.get("title") or "")
    text_norm = normalize_for_match(f"{job.get('title') or ''} {job.get('description') or ''}")
    company_norm = normalize_for_match(job.get("company") or "")
    raw = f"{job.get('title') or ''} {job.get('company') or ''} {job.get('description') or ''}".lower()
    flags: List[str] = []

    if _FOREIGN_POSTING.search(raw) or _FOREIGN_COMPANY.search(company_norm):
        flags.append("Posting text points to another country (foreign employer/currency/domain) "
                     "despite the location field")
    if _INTERNSHIP.search(title_norm) and not facts["accepts_internships"]:
        flags.append("Internship/trainee position - not accepted by the profile")
    if _PUBLIC_SECTOR.search(f"{title_norm} {company_norm}") or _PUBLIC_SECTOR.search(text_norm):
        flags.append("Public-sector / police / civil-service post (competition and nationality rules) "
                     "- eligibility not established")
    for lang, names in _LANGUAGES.items():
        required = re.search(rf"\b(?:{_LANG_LEVEL})\b[^.\n]{{0,40}}\b(?:{names})\b"
                             rf"|\b(?:{names})\b[^.\n]{{0,30}}\b(?:{_LANG_LEVEL})\b", text_norm)
        if required and not any(re.fullmatch(names, known) for known in facts["languages"]):
            flags.append(f"Posting requires {lang.capitalize()} - not among the profile's languages")

    if category == "driver":
        classes = _licence_classes(text_norm)
        missing = [c for c in classes if c not in facts["licences"]]
        if missing:
            flags.append(f"Requires a class {'/'.join(c.upper() for c in missing)} driving licence "
                         "- profile establishes no such licence valid in Spain")
        elif "b" not in facts["licences"]:
            # Driving a vehicle needs a licence valid in Spain whether or not the posting spells it out.
            flags.append("Requires a driving licence (carnet/permiso de conducir, class B) - profile "
                         "establishes no driving licence valid in Spain" if _LICENCE_REQUIRED.search(text_norm)
                         else "Driving role: needs a driving licence valid in Spain - profile establishes no "
                              "driving licence valid in Spain")
        if _experience_required(text_norm) and not facts["driving_experience_years"]:
            flags.append("Requires prior driving/delivery experience - none established by the profile")
        if _OWN_VEHICLE.search(text_norm) and not (facts["own_vehicle"] and facts["self_employed_ok"]):
            flags.append("Self-employed / own-vehicle role - not supported by the profile")
        if _MOTORBIKE_TITLE.search(title_norm) and "a" not in facts["licences"]:
            flags.append("Motorbike/bicycle delivery named in the title - no such licence/vehicle in the profile")
        if _NON_DRIVING_ROLE.search(title_norm):
            flags.append("Title names a non-driving primary role (sales/warehouse/washing/maintenance...) "
                         "- category mismatch")
    return flags


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

    # Candidate eligibility: stated requirements the profile does not establish.
    reasons += eligibility_flags(job, category, profile)

    if reasons:
        # De-duplicate while keeping order.
        result.status = NEEDS_REVIEW
        result.reasons = list(dict.fromkeys(reasons))
    return result
