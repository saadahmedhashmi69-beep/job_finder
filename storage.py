"""SQLite storage for scraped jobs and applications."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

from fingerprint import canonical_url, job_fingerprint, sanitize_job
from models import Job, JobBoard

DB_PATH = Path(__file__).parent / "jobs.db"

# Application is claimed by one submitter; nothing has been confirmed sent yet.
SUBMITTING = "SUBMITTING"
READY_TO_SUBMIT = "READY_TO_SUBMIT"
# Job qualification outcomes that are final until the rules change (see get_pipeline_candidates).
DECIDED_JOB_STATES = ("REJECTED", "NEEDS_REVIEW", "DUPLICATE", "PREP_ERROR")


def _resolve_db(db_path: Optional[Path]) -> Path:
    """Default DB resolved at call time (so tests can point DB_PATH elsewhere)."""
    return Path(db_path) if db_path else DB_PATH


def get_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    # 30 s busy timeout: concurrent processes wait for the write lock instead of failing.
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            url TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            location TEXT,
            board TEXT,
            description TEXT,
            salary TEXT,
            date_posted TEXT,
            job_type TEXT,
            is_remote INTEGER DEFAULT 0,
            scraped_at TEXT,
            match_score REAL DEFAULT 0,
            match_details TEXT DEFAULT '{}',
            applied INTEGER DEFAULT 0,
            hidden INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_url TEXT REFERENCES jobs(url),
            slug TEXT UNIQUE,
            status TEXT DEFAULT 'pending',
            cv_pdf_path TEXT,
            cover_letter_pdf_path TEXT,
            form_answers_json TEXT DEFAULT '{}',
            recruiter_email TEXT DEFAULT '',
            email_subject TEXT DEFAULT '',
            email_body TEXT DEFAULT '',
            approved_at TEXT DEFAULT '',
            sent_at TEXT DEFAULT '',
            created_at TEXT,
            updated_at TEXT
        )
    """)
    # Lightweight schema migration for existing DBs
    _ensure_columns(
        conn,
        "applications",
        {
            "recruiter_email": "TEXT DEFAULT ''",
            "email_subject": "TEXT DEFAULT ''",
            "email_body": "TEXT DEFAULT ''",
            "approved_at": "TEXT DEFAULT ''",
            "sent_at": "TEXT DEFAULT ''",
            # Qualification-gated application workflow (see qualification.py).
            "application_method": "TEXT DEFAULT ''",
            "submission_status": "TEXT DEFAULT ''",
            "submission_mode": "TEXT DEFAULT ''",
            "status_reason": "TEXT DEFAULT ''",
            "cv_sha256": "TEXT DEFAULT ''",
            "submitted_at": "TEXT DEFAULT ''",
            "submission_evidence": "TEXT DEFAULT ''",
            # Application route (see application_prep.discover_application_route).
            "application_url": "TEXT DEFAULT ''",
            "route_type": "TEXT DEFAULT ''",
            "route_source": "TEXT DEFAULT ''",
            "submission_report_json": "TEXT DEFAULT '{}'",
        },
    )
    _ensure_columns(
        conn,
        "jobs",
        {
            "qualification_status": "TEXT DEFAULT ''",
            "qualification_category": "TEXT DEFAULT ''",
            "qualification_reasons": "TEXT DEFAULT '[]'",
            "qualified_at": "TEXT DEFAULT ''",
            "apply_url": "TEXT DEFAULT ''",
        },
    )
    if _ensure_columns(conn, "jobs", {"is_remote": "INTEGER DEFAULT 0"}):
        _backfill_is_remote(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            finished_at TEXT,
            jobs_scraped INTEGER DEFAULT 0,
            jobs_matched INTEGER DEFAULT 0,
            applications_created INTEGER DEFAULT 0,
            emails_sent INTEGER DEFAULT 0,
            status TEXT DEFAULT 'running',
            log TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at TEXT,
            subject TEXT,
            job_count INTEGER DEFAULT 0,
            recipient TEXT
        )
    """)
    conn.commit()
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> bool:
    """Add columns if missing (SQLite ALTER TABLE ADD COLUMN).

    Returns True if any column was added (useful for triggering backfills).
    """
    try:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return False
    added = False
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            added = True
    conn.commit()
    return added


def _backfill_is_remote(conn: sqlite3.Connection) -> None:
    """Classify remote/home-office for rows that predate the is_remote column."""
    from models import classify_remote

    rows = conn.execute(
        "SELECT url, title, description, location, job_type FROM jobs"
    ).fetchall()
    for r in rows:
        is_remote = classify_remote(
            r["title"] or "", r["description"] or "",
            r["location"] or "", r["job_type"] or "",
        ) == "remote"
        conn.execute(
            "UPDATE jobs SET is_remote = ? WHERE url = ?",
            (1 if is_remote else 0, r["url"]),
        )
    conn.commit()


# --- Application CRUD ---

def create_application(job_url: str, slug: str, db_path: Path = DB_PATH) -> int:
    """Create a new application record. Returns the application ID."""
    conn = get_db(db_path)
    now = datetime.now().isoformat()
    cursor = conn.execute(
        """INSERT INTO applications (job_url, slug, status, created_at, updated_at)
           VALUES (?, ?, 'pending', ?, ?)""",
        (job_url, slug, now, now),
    )
    app_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return app_id


def update_application(app_id: int, db_path: Path = DB_PATH, **kwargs):
    """Update application fields. Pass field=value as keyword args."""
    conn = get_db(db_path)
    kwargs["updated_at"] = datetime.now().isoformat()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [app_id]
    conn.execute(f"UPDATE applications SET {sets} WHERE id = ?", values)
    conn.commit()
    conn.close()


def get_applications(
    status: Optional[str] = None,
    limit: int = 50,
    db_path: Path = DB_PATH,
) -> List[Dict]:
    """Get applications, optionally filtered by status."""
    conn = get_db(db_path)
    if status:
        rows = conn.execute(
            """SELECT a.*, j.title, j.company, j.location, j.match_score, j.board,
                      j.qualification_status, j.qualification_reasons
               FROM applications a JOIN jobs j ON a.job_url = j.url
               WHERE a.status = ? ORDER BY a.created_at DESC LIMIT ?""",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT a.*, j.title, j.company, j.location, j.match_score, j.board,
                      j.qualification_status, j.qualification_reasons
               FROM applications a JOIN jobs j ON a.job_url = j.url
               ORDER BY a.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_application_by_job(job_url: str, db_path: Path = DB_PATH) -> Optional[Dict]:
    """Get application for a specific job, or None."""
    conn = get_db(db_path)
    row = conn.execute(
        "SELECT * FROM applications WHERE job_url = ?", (job_url,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def find_existing_application(job_url: str, title: str = "", company: str = "", location: str = "",
                              description: str = "", db_path: Optional[Path] = None) -> Optional[Dict]:
    """Duplicate protection: an application for the same posting (exact or
    canonical URL) or for the same vacancy fingerprint (normalised
    title + employer + city; see fingerprint.py). Compared in Python, so
    accents/case/spacing/tracking parameters cannot hide a duplicate."""
    db_path = _resolve_db(db_path)
    app = get_application_by_job(job_url, db_path=db_path)
    if app:
        return app
    conn = get_db(db_path)
    rows = conn.execute(
        """SELECT a.*, j.url AS _url, j.title AS _title, j.company AS _company,
                  j.location AS _location, j.description AS _description
           FROM applications a JOIN jobs j ON a.job_url = j.url ORDER BY a.id""").fetchall()
    conn.close()
    wanted_url = canonical_url(job_url)
    wanted_fp = job_fingerprint(title, company, location, description) if (title and company) else None
    for row in rows:
        row = dict(row)
        if canonical_url(row["_url"]) == wanted_url or (
                wanted_fp and job_fingerprint(row["_title"], row["_company"], row["_location"],
                                              row["_description"]) == wanted_fp):
            return {k: v for k, v in row.items() if not k.startswith("_")}
    return None


# --- Atomic submission claim ---------------------------------------------------

def claim_application(app_id: int, db_path: Optional[Path] = None) -> bool:
    """Atomically move one READY_TO_SUBMIT application to SUBMITTING.

    A single conditional UPDATE: SQLite serialises writers, so across threads
    and separate processes exactly one caller gets True. It refuses whenever
    any earlier send/submission evidence exists (sent_at, submitted_at,
    submission evidence, a SUBMITTED/SMTP_ACCEPTED/SUBMITTING submission
    state), even if the status was manually reset to READY_TO_SUBMIT."""
    conn = get_db(_resolve_db(db_path))
    try:
        cur = conn.execute(
            """UPDATE applications
                  SET status = ?, submission_status = ?, updated_at = ?,
                      status_reason = 'Claimed for submission; outcome not recorded yet'
                WHERE id = ? AND status = ?
                  AND COALESCE(sent_at, '') = '' AND COALESCE(submitted_at, '') = ''
                  AND COALESCE(submission_evidence, '') = ''
                  AND COALESCE(submission_status, '') NOT IN
                      ('SUBMITTED', 'SMTP_ACCEPTED', 'SUBMITTING', 'UNVERIFIED_LEGACY')""",
            (SUBMITTING, SUBMITTING, datetime.now().isoformat(), app_id, READY_TO_SUBMIT))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def update_application_if_status(app_id: int, expected_statuses, db_path: Optional[Path] = None, **kwargs) -> bool:
    """update_application, but only while the row is still in one of
    `expected_statuses`. A worker can therefore never overwrite the outcome
    another worker recorded in the meantime (e.g. a slow DRY_RUN inspection
    finishing after an explicit LIVE submit). True when the row was updated."""
    kwargs["updated_at"] = datetime.now().isoformat()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    marks = ",".join("?" for _ in expected_statuses)
    conn = get_db(_resolve_db(db_path))
    try:
        cur = conn.execute(f"UPDATE applications SET {sets} WHERE id = ? AND status IN ({marks})",
                           [*kwargs.values(), app_id, *expected_statuses])
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def recover_stale_submissions(max_age_minutes: float = 15, db_path: Optional[Path] = None) -> int:
    """A claim whose process died stays SUBMITTING. After `max_age_minutes` it is
    closed as SUBMISSION_FAILED with an explicit "outcome unknown" reason — it is
    never returned to READY_TO_SUBMIT, so it can never be re-sent automatically."""
    cutoff = datetime.fromtimestamp(datetime.now().timestamp() - max_age_minutes * 60).isoformat()
    conn = get_db(_resolve_db(db_path))
    try:
        cur = conn.execute(
            """UPDATE applications
                  SET status = 'SUBMISSION_FAILED', submission_status = 'SUBMISSION_FAILED', updated_at = ?,
                      status_reason = 'Interrupted while submitting (the process stopped after claiming this '
                                      || 'application). Outcome unknown: check the mailbox/employer site before '
                                      || 'any manual retry. Never retried automatically.'
                WHERE status = ? AND COALESCE(updated_at, '') < ?""",
            (datetime.now().isoformat(), SUBMITTING, cutoff))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def recipient_already_emailed(email: str, exclude_app_id: int, db_path: Optional[Path] = None,
                              include_in_progress: bool = False) -> Optional[int]:
    """Id of another application already emailed to this address, if any. A
    terminal SUBMISSION_FAILED (send outcome unknown, may have been delivered)
    counts as emailed. With `include_in_progress`, an application another worker
    is sending right now (SUBMITTING) counts as well."""
    wanted = (email or "").strip().lower()
    if not wanted:
        return None
    conn = get_db(_resolve_db(db_path))
    rows = conn.execute(
        "SELECT id, recruiter_email FROM applications WHERE id <> ? AND (COALESCE(sent_at, '') <> ''"
        " OR status = 'SUBMISSION_FAILED'"
        + (" OR status = 'SUBMITTING')" if include_in_progress else ")"),
        (exclude_app_id,)).fetchall()
    conn.close()
    return next((r["id"] for r in rows if (r["recruiter_email"] or "").strip().lower() == wanted), None)


# --- Pipeline candidate selection ---------------------------------------------

def get_pipeline_candidates(min_score: float = 0.0, limit: int = 50, rules_changed_at: str = "",
                            db_path: Optional[Path] = None) -> List[Dict]:
    """Jobs the pipeline should still look at, best first.

    Only rows that can actually progress occupy the window: score >= threshold,
    a usable description, no application yet, and not already decided
    (REJECTED / NEEDS_REVIEW / DUPLICATE / PREP_ERROR). A decided job is looked
    at again only if it was decided before `rules_changed_at` (profile or rule
    code changed since), after all never-examined jobs."""
    marks = ",".join("?" for _ in DECIDED_JOB_STATES)
    conn = get_db(_resolve_db(db_path))
    rows = conn.execute(
        f"""SELECT j.* FROM jobs j
            WHERE j.match_score >= ? AND j.hidden = 0 AND TRIM(COALESCE(j.description, '')) <> ''
              AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_url = j.url)
              AND (COALESCE(j.qualification_status, '') NOT IN ({marks})
                   OR COALESCE(j.qualified_at, '') < ?)
            ORDER BY CASE WHEN COALESCE(j.qualification_status, '') IN ({marks}) THEN 1 ELSE 0 END,
                     j.match_score DESC, j.date_posted DESC
            LIMIT ?""",
        (min_score, *DECIDED_JOB_STATES, rules_changed_at or "", *DECIDED_JOB_STATES, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_job_qualification(job_url: str, status: str, category: str = "",
                          reasons: Optional[List[str]] = None, db_path: Path = DB_PATH):
    conn = get_db(db_path)
    conn.execute(
        """UPDATE jobs SET qualification_status = ?, qualification_category = ?,
           qualification_reasons = ?, qualified_at = ? WHERE url = ?""",
        (status, category, json.dumps(reasons or []), datetime.now().isoformat(), job_url),
    )
    conn.commit()
    conn.close()


def get_jobs_by_qualification(statuses: List[str], limit: int = 100,
                              db_path: Path = DB_PATH) -> List[Dict]:
    conn = get_db(db_path)
    marks = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"""SELECT * FROM jobs WHERE qualification_status IN ({marks}) AND hidden = 0
            ORDER BY match_score DESC LIMIT ?""",
        (*statuses, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Pipeline Run Logging ---

def start_pipeline_run(db_path: Path = DB_PATH) -> int:
    """Start a new pipeline run. Returns run ID."""
    conn = get_db(db_path)
    now = datetime.now().isoformat()
    cursor = conn.execute(
        "INSERT INTO pipeline_runs (started_at, status) VALUES (?, 'running')",
        (now,),
    )
    run_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return run_id


def finish_pipeline_run(
    run_id: int,
    jobs_scraped: int = 0,
    jobs_matched: int = 0,
    applications_created: int = 0,
    emails_sent: int = 0,
    status: str = "completed",
    log: str = "",
    db_path: Path = DB_PATH,
):
    """Finish a pipeline run with results."""
    conn = get_db(db_path)
    now = datetime.now().isoformat()
    conn.execute(
        """UPDATE pipeline_runs
           SET finished_at=?, jobs_scraped=?, jobs_matched=?,
               applications_created=?, emails_sent=?, status=?, log=?
           WHERE id=?""",
        (now, jobs_scraped, jobs_matched, applications_created, emails_sent, status, log, run_id),
    )
    conn.commit()
    conn.close()


def recover_stale_runs(db_path: Optional[Path] = None) -> int:
    """Close pipeline runs left 'running' by a crashed/killed process. Call only
    while holding the pipeline lock (then no other run can be in progress)."""
    now = datetime.now().isoformat()
    conn = get_db(_resolve_db(db_path))
    try:
        cur = conn.execute(
            """UPDATE pipeline_runs
                  SET status = 'interrupted', finished_at = ?,
                      log = TRIM(COALESCE(log, '') || char(10) || 'Recovered at ' || ? ||
                                 ': the run never finished (process stopped); marked interrupted.')
                WHERE status = 'running'""", (now, now))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_pipeline_runs(limit: int = 20, db_path: Path = DB_PATH) -> List[Dict]:
    conn = get_db(db_path)
    rows = conn.execute(
        "SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Email Log ---

def log_email_sent(subject: str, job_count: int, recipient: str, db_path: Path = DB_PATH):
    conn = get_db(db_path)
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO email_log (sent_at, subject, job_count, recipient) VALUES (?, ?, ?, ?)",
        (now, subject, job_count, recipient),
    )
    conn.commit()
    conn.close()


def get_last_email_sent(db_path: Path = DB_PATH) -> Optional[Dict]:
    conn = get_db(db_path)
    row = conn.execute(
        "SELECT * FROM email_log ORDER BY sent_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_new_jobs_since(since_iso: str, min_score: float = 0.0, db_path: Path = DB_PATH) -> List[Dict]:
    """Get jobs scraped after a given ISO timestamp."""
    conn = get_db(db_path)
    rows = conn.execute(
        """SELECT * FROM jobs
           WHERE scraped_at > ? AND match_score >= ? AND hidden = 0
           ORDER BY match_score DESC""",
        (since_iso, min_score),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_jobs(jobs: List[Job], db_path: Path = DB_PATH) -> int:
    """Save jobs to SQLite. Returns the number of rows actually inserted.
    Deduplicates by canonical URL and by vacancy fingerprint (normalised
    title + employer + city; see fingerprint.py). Unusable jobs are skipped."""
    conn = get_db(db_path)
    existing = conn.execute("SELECT url, title, company, location, description FROM jobs").fetchall()
    seen_urls = {canonical_url(r[0]) for r in existing}
    seen_fingerprints = {job_fingerprint(r[1], r[2], r[3], r[4]) for r in existing}

    inserted = 0
    for job in jobs:
        if not sanitize_job(job):
            continue
        url_key = canonical_url(job.url)
        fingerprint = job_fingerprint(job.title, job.company, job.location, job.description)
        if url_key in seen_urls or fingerprint in seen_fingerprints:
            continue
        try:
            cur = conn.execute(
                """INSERT OR IGNORE INTO jobs
                   (url, title, company, location, board, description,
                    salary, date_posted, job_type, is_remote, scraped_at,
                    match_score, match_details, apply_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.url, job.title, job.company, job.location,
                    job.board.value, job.description, job.salary,
                    job.date_posted, job.job_type, 1 if job.is_remote else 0,
                    job.scraped_at, job.match_score, json.dumps(job.match_details),
                    job.apply_url,
                ),
            )
            seen_urls.add(url_key)
            seen_fingerprints.add(fingerprint)
            inserted += cur.rowcount if cur.rowcount > 0 else 0
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    conn.close()
    return inserted


def update_scores(jobs: List[Job], db_path: Path = DB_PATH):
    """Update match scores for existing jobs."""
    conn = get_db(db_path)
    for job in jobs:
        conn.execute(
            "UPDATE jobs SET match_score = ?, match_details = ?, is_remote = ? WHERE url = ?",
            (job.match_score, json.dumps(job.match_details), 1 if job.is_remote else 0, job.url),
        )
    conn.commit()
    conn.close()


def get_top_jobs(limit: int = 20, min_score: float = 0.0, db_path: Path = DB_PATH) -> list[dict]:
    """Get top-scored jobs from the database."""
    conn = get_db(db_path)
    rows = conn.execute(
        """SELECT * FROM jobs
           WHERE match_score >= ? AND hidden = 0
           ORDER BY match_score DESC, CASE WHEN date_posted IS NULL OR date_posted = '' OR LOWER(date_posted) IN ('nan','nat','none','null') THEN 0 ELSE 1 END DESC, date_posted DESC
           LIMIT ?""",
        (min_score, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_applied(url: str, db_path: Path = DB_PATH):
    conn = get_db(db_path)
    conn.execute("UPDATE jobs SET applied = 1 WHERE url = ?", (url,))
    conn.commit()
    conn.close()


def mark_hidden(url: str, db_path: Path = DB_PATH):
    conn = get_db(db_path)
    conn.execute("UPDATE jobs SET hidden = 1 WHERE url = ?", (url,))
    conn.commit()
    conn.close()
