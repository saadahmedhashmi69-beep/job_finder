"""Pipeline orchestrator — background daemon for automated job application.

Runs: scrape → dedupe → match → hard qualification → application preparation
(fixed CV, truthful cover letter + form answers) → READY_TO_SUBMIT / MANUAL_REQUIRED.

The pipeline and the daemon NEVER submit or send an application, whatever the
configuration says: the whole run executes inside safety.no_live_sends(), and
the only thing done with READY_TO_SUBMIT applications is a DRY_RUN inspection.
Submitting is a separate, explicit action (`python main.py submit --app-id N`).

The CV attachment is always the fixed PDF from pipeline.fixed_cv_path (see fixed_cv.py).
Can run as a one-shot or as a daemon on a 2-day interval. Only one pipeline run
(and one daemon) can be active at a time (runlock.py).
"""

import logging
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import safety
import storage
from fingerprint import dedupe_jobs
from models import Job, JobBoard, SearchQuery
from runlock import FileLock
from scrapers import SCRAPERS
from matcher import JobMatcher
from storage import (
    save_jobs, get_db, get_top_jobs, find_existing_application,
    start_pipeline_run, finish_pipeline_run,
    get_new_jobs_since, get_last_email_sent,
    get_pipeline_candidates, recover_stale_runs, recover_stale_submissions,
)
from fixed_cv import resolve_fixed_cv_path
from application_prep import prepare_application
from notifier import send_digest_email, should_send_digest

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).parent
CONFIG_PATH = _ROOT / "profile.yaml"
LOCK_PATH = _ROOT / ".pipeline.lock"
DAEMON_LOCK_PATH = _ROOT / ".daemon.lock"
LOCATION_AGNOSTIC_BOARDS = {"remotive", "arbeitnow", "himalayas", "greenhouse", "lever", "linkedin_posts", "internet"}

# Graceful shutdown flag
_shutdown = False


def _signal_handler(signum, frame):
    global _shutdown
    logger.info("Shutdown signal received. Finishing current cycle...")
    _shutdown = True


def load_profile() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _rules_changed_at() -> str:
    """When the qualification rules last changed (profile or rule code), as the
    same local ISO format used for jobs.qualified_at."""
    stamps = []
    for path in (CONFIG_PATH, _ROOT / "qualification.py", _ROOT / "matcher.py"):
        try:
            stamps.append(path.stat().st_mtime)
        except OSError:
            pass
    return datetime.fromtimestamp(max(stamps)).isoformat() if stamps else ""


def _scrape_all(profile: dict, max_per_query: int = 50) -> List[Job]:
    """Scrape all configured boards. Returns deduplicated job list."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    search = profile.get("search", {})
    board_names = search.get("boards", ["remotive", "adzuna", "linkedin"])
    boards = []
    for b in board_names:
        try:
            boards.append(JobBoard(b))
        except ValueError:
            logger.warning("Unknown board in profile: %s", b)

    queries = []
    for kw in search.get("queries", ["machine learning engineer"]):
        for loc in search.get("locations", [""]):
            queries.append(SearchQuery(
                keywords=kw, location=loc,
                remote=search.get("remote", False),
                max_age_days=search.get("max_age_days", 14),
                boards=boards,
            ))

    all_jobs = []
    seen_combos = set()
    futures = {}
    per_board: Dict[str, Dict[str, int]] = {}

    with ThreadPoolExecutor(max_workers=8) as pool:
        for query in queries:
            for board in query.boards:
                board_name = board.value
                if board_name in LOCATION_AGNOSTIC_BOARDS:
                    combo = (board_name, query.keywords)
                    if combo in seen_combos:
                        continue
                    seen_combos.add(combo)

                scraper_cls = SCRAPERS.get(board_name)
                if not scraper_cls:
                    continue
                fut = pool.submit(_scrape_one, scraper_cls, query, max_per_query)
                futures[fut] = board_name

        for fut in as_completed(futures):
            board_name = futures[fut]
            tally = per_board.setdefault(board_name, {"queries": 0, "jobs": 0, "errors": 0})
            tally["queries"] += 1
            try:
                found = fut.result()
                tally["jobs"] += len(found)
                all_jobs.extend(found)
            except Exception as e:
                tally["errors"] += 1
                logger.error("Scraper error (%s): %s", board_name, e)

    for board_name, tally in sorted(per_board.items()):
        logger.info("Scrape source %s: %d jobs from %d queries (%d failed)",
                    board_name, tally["jobs"], tally["queries"], tally["errors"])

    # Deduplicate (canonical URL + vacancy fingerprint); malformed jobs are dropped.
    unique = dedupe_jobs(all_jobs)
    logger.info("Scrape total: %d raw, %d unique", len(all_jobs), len(unique))
    return unique


def _scrape_one(scraper_cls, query, max_results):
    scraper = scraper_cls()
    return scraper.scrape(query, max_results=max_results)


def run_pipeline(
    profile: Optional[dict] = None,
    dry_run: bool = False,
    max_applications: Optional[int] = None,
    threshold: Optional[float] = None,
    model: str = "qwen3.5:9b",
) -> Dict:
    """Run one full pipeline cycle (never submits or sends an application).

    `max_applications` / `threshold` given explicitly (CLI flags) take precedence
    over profile.yaml; otherwise the profile values, then 10 / 0.5, are used.
    `dry_run` is a preview: no scraping and no preparation.

    Returns dict with stats: jobs_scraped, jobs_matched, applications_created,
    emails_sent (+ "skipped" when another run holds the lock).
    """
    stats = {"jobs_scraped": 0, "jobs_matched": 0, "applications_created": 0, "emails_sent": 0}
    lock = FileLock(LOCK_PATH)
    if not lock.acquire():
        logger.warning("Another pipeline run is active (lock %s) - this run is skipped", LOCK_PATH.name)
        return dict(stats, skipped="another pipeline run is active")
    try:
        # Nothing in here may send or submit, whatever the configuration says.
        with safety.no_live_sends():
            return _run_pipeline_locked(profile, dry_run, max_applications, threshold, model, stats)
    finally:
        lock.release()


def _run_pipeline_locked(profile, dry_run, max_applications, threshold, model, stats) -> Dict:
    if profile is None:
        profile = load_profile()

    pipeline_config = profile.get("pipeline", {})
    if threshold is None:
        threshold = pipeline_config.get("auto_apply_threshold", 0.5)
    if max_applications is None:
        max_applications = pipeline_config.get("max_applications_per_run", 10)
    recipient = pipeline_config.get("email_recipient", "")
    interval_days = pipeline_config.get("email_digest_interval_days", 2)

    # We hold the pipeline lock, so any run still marked 'running' belongs to a dead process.
    try:
        stale_runs = recover_stale_runs()
        stale_subs = recover_stale_submissions()
        if stale_runs or stale_subs:
            logger.warning("Recovery: %d stale pipeline run(s) closed, %d interrupted submission(s) closed",
                           stale_runs, stale_subs)
    except Exception as e:
        logger.error("Stale-state recovery failed: %s", e)

    run_id = start_pipeline_run(db_path=storage.DB_PATH)
    logger.info("=== Pipeline run #%s started (preview=%s, max_applications=%s, threshold=%.2f) ===",
                run_id, dry_run, max_applications, threshold)
    log_lines = []

    try:
        # --- Step 1: Scrape ---
        logger.info("=== Pipeline Step 1: Scraping ===")
        if not dry_run:
            jobs = _scrape_all(profile)
            matcher = JobMatcher(profile)
            ranked = matcher.rank(jobs)
            n_saved = save_jobs(ranked, db_path=storage.DB_PATH)
            stats["jobs_scraped"] = len(ranked)
            log_lines.append(f"Scraped {len(ranked)} jobs, {n_saved} new")
            logger.info("Scraped %d jobs, %d new saved", len(ranked), n_saved)
        else:
            logger.info("[PREVIEW] Would scrape jobs")

        # --- Step 2: MATCHED jobs (score only ranks; it never qualifies) ---
        # Only jobs that can still progress are selected: no application yet, a
        # usable description, and not already decided under the current rules.
        logger.info("=== Pipeline Step 2: Selecting matched jobs ===")
        top_jobs = get_pipeline_candidates(min_score=threshold, limit=max(max_applications * 5, 50),
                                           rules_changed_at=_rules_changed_at())
        candidates = []
        for job in top_jobs:
            # Duplicate protection: the same vacancy under another URL/board already has an application.
            existing = find_existing_application(job["url"], job.get("title", ""), job.get("company", ""),
                                                 location=job.get("location", ""),
                                                 description=job.get("description", ""))
            if existing:
                if not dry_run:
                    storage.set_job_qualification(
                        job["url"], "DUPLICATE", "",
                        [f"Same vacancy as application #{existing.get('id')}"], db_path=storage.DB_PATH)
                continue
            if job.get("description"):
                candidates.append(job)
        stats["jobs_matched"] = len(candidates)
        logger.info("Found %d matched jobs (score >= %.2f) to qualify", len(candidates), threshold)

        # --- Step 3: Hard qualification -> application preparation ---
        # Only QUALIFIED jobs get an application (fixed CV, letter, answers).
        logger.info("=== Pipeline Step 3: Qualification + application preparation ===")
        # Fail clearly up front if the fixed CV is missing.
        fixed_cv = resolve_fixed_cv_path(profile)
        logger.info("Using fixed CV for all applications: %s", fixed_cv)
        matcher = JobMatcher(profile)
        qual_counts = {}
        route_checked = set()  # application ids route-checked during this run

        for i, job in enumerate(candidates):
            if _shutdown:
                logger.info("Shutdown requested, stopping pipeline")
                break
            if stats["applications_created"] >= max_applications:
                break
            if dry_run:
                logger.info("[PREVIEW] Would qualify %s at %s", job["title"], job["company"])
                continue
            try:
                result = prepare_application(job, profile, matcher=matcher)
                qual_counts[result["status"]] = qual_counts.get(result["status"], 0) + 1
                if result["app_id"] and result["status"] != "DUPLICATE":
                    route_checked.add(result["app_id"])
                    stats["applications_created"] += 1
                    log_lines.append(f"{result['status']}: {job['title']} at {job['company']}")
                else:
                    if result["status"] == "DUPLICATE":
                        storage.set_job_qualification(job["url"], "DUPLICATE", "", result["reasons"],
                                                      db_path=storage.DB_PATH)
                    log_lines.append(f"{result['status']}: {job['title']} at {job['company']} "
                                     f"({'; '.join(result['reasons'])})")
            except Exception as e:
                logger.exception("Failed to process job %s: %s", job.get("url"), e)
                log_lines.append(f"ERROR: {job['title']} at {job['company']}: {e}")
                try:  # a job that cannot be prepared must not occupy the window forever
                    storage.set_job_qualification(job["url"], "PREP_ERROR", "", [f"Preparation error: {e}"],
                                                  db_path=storage.DB_PATH)
                except Exception:
                    pass

        if qual_counts:
            log_lines.append(f"Qualification/application outcomes: {qual_counts}")
            logger.info("Qualification/application outcomes: %s", qual_counts)
        if not dry_run and not _shutdown:
            # Earlier route-less MANUAL_REQUIRED applications: look for a public
            # employer/ATS route again (discovery only; nothing is submitted).
            # Applications already route-checked above are not discovered twice.
            try:
                import application_prep
                needs_route = application_prep.needs_route
                application_prep.needs_route = (
                    lambda app: app.get("id") not in route_checked and needs_route(app))
                try:
                    rerouted = application_prep.reroute_manual_applications(profile=profile, matcher=matcher)
                finally:
                    application_prep.needs_route = needs_route
                if rerouted:
                    log_lines.append(f"Re-routed {rerouted} MANUAL_REQUIRED application(s) to READY_TO_SUBMIT")
                    logger.info("Re-routed %d application(s) to READY_TO_SUBMIT", rerouted)
            except Exception as e:
                logger.exception("Route re-discovery failed: %s", e)
            # READY_TO_SUBMIT applications: DRY_RUN inspection ONLY. The pipeline never
            # submits; a real submission needs `python main.py submit --app-id N`.
            try:
                import submitter
                outcomes = submitter.process_ready_applications(
                    profile, limit=max_applications, mode=submitter.DRY_RUN, interactive=False)
                if outcomes:
                    log_lines.append(f"Validation step (DRY_RUN, nothing submitted or sent): {outcomes}")
                    logger.info("Validation step (DRY_RUN, nothing submitted or sent): %s", outcomes)
            except Exception as e:
                logger.exception("Validation step failed: %s", e)

        # --- Step 4: Send email digest ---
        logger.info("=== Pipeline Step 4: Email digest ===")
        if should_send_digest(interval_days) and not dry_run:
            last_email = get_last_email_sent(db_path=storage.DB_PATH)
            since = last_email["sent_at"] if last_email else "2000-01-01T00:00:00"
            new_jobs = get_new_jobs_since(since, min_score=threshold, db_path=storage.DB_PATH)

            if new_jobs and recipient:
                success = send_digest_email(new_jobs, recipient)
                if success:
                    stats["emails_sent"] = 1
                    log_lines.append(f"Email sent: {len(new_jobs)} jobs")
            else:
                logger.info("No new jobs since last digest")
        else:
            logger.info("Digest not due yet or preview")

        # --- Done ---
        finish_pipeline_run(
            run_id,
            status="completed",
            log="\n".join(log_lines),
            db_path=storage.DB_PATH,
            **stats,
        )
        logger.info("=== Pipeline run #%s complete: %s ===", run_id, stats)

    except Exception as e:
        logger.exception("Pipeline run #%s failed: %s", run_id, e)
        finish_pipeline_run(run_id, status="failed", log="\n".join(log_lines + [f"FAILED: {e}"]),
                            db_path=storage.DB_PATH, **stats)

    return stats


def run_daemon(interval_hours: float = 48.0, max_cycles: Optional[int] = None) -> bool:
    """Run the pipeline in a loop. Default: every 48 hours (2 days).

    Only one daemon may run: a second start returns False immediately. The
    daemon never submits or sends applications (see run_pipeline).
    `max_cycles` stops after that many cycles (used by tests)."""
    global _shutdown
    lock = FileLock(DAEMON_LOCK_PATH, stale_after_seconds=30 * 24 * 3600)
    if not lock.acquire():
        logger.error("Another daemon is already running (lock %s) - not starting a second one",
                     DAEMON_LOCK_PATH.name)
        return False
    try:
        try:
            signal.signal(signal.SIGINT, _signal_handler)
            signal.signal(signal.SIGTERM, _signal_handler)
        except ValueError:
            pass  # not the main thread: the caller handles shutdown

        interval_seconds = interval_hours * 3600
        logger.info("Daemon started (interval: %.1f hours). It never submits applications. "
                    "Press Ctrl+C to stop.", interval_hours)
        cycles = 0
        while not _shutdown:
            logger.info("=== Daemon cycle %d starting at %s ===", cycles + 1, datetime.now().isoformat())
            try:
                run_pipeline()
            except Exception as e:
                logger.exception("Pipeline cycle failed: %s", e)
            cycles += 1
            if _shutdown or (max_cycles is not None and cycles >= max_cycles):
                break

            logger.info("Next cycle in %.1f hours. Sleeping...", interval_hours)
            # Sleep in small increments to allow graceful shutdown
            deadline = time.monotonic() + interval_seconds
            try:
                while not _shutdown and time.monotonic() < deadline:
                    time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            except KeyboardInterrupt:
                _shutdown = True
        logger.info("Daemon stopped after %d cycle(s).", cycles)
        return True
    finally:
        lock.release()
