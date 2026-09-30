"""Send a prepared job application by email (Gmail SMTP over SSL).

Called only from the explicit submit path (submitter.submit_application).
Every SMTP operation has a finite timeout, errors are classified, and the
result says whether anything can have left this machine. Credentials come from
the environment and are never logged.
"""

from __future__ import annotations

import logging
import os
import smtplib
import socket
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Dict, Optional

import safety

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
DEFAULT_SMTP_TIMEOUT = 30.0

# error classes
CREDENTIALS = "credentials"
ATTACHMENT = "attachment"
CONNECTION = "connection"
AUTHENTICATION = "authentication"
RECIPIENT_REJECTED = "recipient_rejected"
TIMEOUT = "timeout"
SMTP_ERROR = "smtp_error"
FORBIDDEN = "forbidden"


def smtp_timeout() -> float:
    """Seconds allowed for each SMTP network operation (SMTP_TIMEOUT_SECONDS, 5-120, default 30)."""
    try:
        value = float(os.environ.get("SMTP_TIMEOUT_SECONDS", "") or DEFAULT_SMTP_TIMEOUT)
    except ValueError:
        value = DEFAULT_SMTP_TIMEOUT
    return min(max(value, 5.0), 120.0)


def prepare_application_package(app_dir: Path, cv_path: Path) -> dict:
    """Build the attachment set for an application.

    The CV is always the explicitly supplied fixed CV path; PDFs found in
    `app_dir` are never used as the CV. Only the cover letter comes from `app_dir`.
    """
    cv_path = Path(cv_path)
    if not cv_path.is_file():
        raise FileNotFoundError(f"Fixed CV PDF not found: {cv_path}")
    cl_path = Path(app_dir) / "cover-letter.pdf"

    return {
        "cv": cv_path,
        "cover_letter": cl_path if cl_path.exists() else None,
    }


def _fail(error_class: str, sent_possible: bool, detail: str) -> Dict:
    logger.error("Application email not confirmed (%s): %s", error_class, detail)
    return {"ok": False, "error_class": error_class, "sent_possible": sent_possible, "detail": detail}


def _attach(msg: MIMEMultipart, path: Path) -> None:
    subtype = "pdf" if path.suffix.lower() == ".pdf" else "octet-stream"
    part = MIMEApplication(path.read_bytes(), _subtype=subtype, Name=path.name)
    part["Content-Disposition"] = f'attachment; filename="{path.name}"'
    msg.attach(part)


def send_application_email_detailed(
    *,
    to_email: str,
    subject: str,
    body: str,
    cv_path: Path,
    cover_letter_path: Optional[Path] = None,
    gmail_user: Optional[str] = None,
    gmail_app_password: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Dict:
    """Send one application email. Returns
    {"ok", "error_class", "sent_possible", "detail"}.

    `sent_possible` is False only when the failure provably happened before the
    message was handed over (missing credentials, connection/login failure,
    rejected recipient); True means the outcome is unknown and the message must
    never be re-sent automatically."""
    if safety.live_sends_forbidden():
        return _fail(FORBIDDEN, False, "sending is disabled inside the pipeline/daemon; use an explicit submit")
    gmail_user = gmail_user or os.environ.get("GMAIL_USER")
    gmail_app_password = gmail_app_password or os.environ.get("GMAIL_APP_PASSWORD")
    if not gmail_user or not gmail_app_password:
        return _fail(CREDENTIALS, False, "GMAIL_USER or GMAIL_APP_PASSWORD not set")

    cv_path = Path(cv_path)
    if not cv_path.is_file():
        return _fail(ATTACHMENT, False, f"CV not found: {cv_path}")
    to_email = (to_email or "").strip()
    if not to_email or any(c in to_email for c in "\r\n ,;"):
        return _fail(RECIPIENT_REJECTED, False, "recipient is not a single plain address")

    msg = MIMEMultipart()
    msg["From"] = gmail_user
    msg["To"] = to_email
    msg["Subject"] = " ".join(str(subject or "").split())
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=gmail_user.rpartition("@")[2] or None)
    msg.attach(MIMEText(body or "", "plain", "utf-8"))
    try:
        _attach(msg, cv_path)
        if cover_letter_path and Path(cover_letter_path).is_file():
            _attach(msg, Path(cover_letter_path))
    except OSError as e:
        return _fail(ATTACHMENT, False, f"attachment unreadable: {e.__class__.__name__}")

    timeout = timeout or smtp_timeout()
    try:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=timeout)
    except (socket.timeout, TimeoutError):
        return _fail(TIMEOUT, False, f"no connection to the SMTP server within {timeout:.0f}s")
    except (OSError, smtplib.SMTPException) as e:
        return _fail(CONNECTION, False, f"could not connect to the SMTP server ({e.__class__.__name__})")

    try:
        try:
            server.login(gmail_user, gmail_app_password)
        except smtplib.SMTPAuthenticationError:
            return _fail(AUTHENTICATION, False, "Gmail rejected the login (check the app password)")
        except (socket.timeout, TimeoutError):
            return _fail(TIMEOUT, False, f"SMTP login timed out after {timeout:.0f}s")
        except (OSError, smtplib.SMTPException) as e:
            return _fail(CONNECTION, False, f"SMTP login failed ({e.__class__.__name__})")

        try:
            server.send_message(msg)
        except smtplib.SMTPRecipientsRefused:
            return _fail(RECIPIENT_REJECTED, False, f"the SMTP server refused the recipient {to_email}")
        except smtplib.SMTPSenderRefused:
            return _fail(SMTP_ERROR, False, "the SMTP server refused the sender address")
        except smtplib.SMTPDataError as e:
            return _fail(SMTP_ERROR, False, f"the SMTP server rejected the message (code {e.smtp_code})")
        except (socket.timeout, TimeoutError):
            return _fail(TIMEOUT, True, f"SMTP send timed out after {timeout:.0f}s - delivery state unknown")
        except (OSError, smtplib.SMTPException) as e:
            return _fail(SMTP_ERROR, True, f"SMTP send failed ({e.__class__.__name__}) - delivery state unknown")
    finally:
        try:
            server.quit()
        except Exception:
            try:
                server.close()
            except Exception:
                pass

    logger.info("Application email accepted by the SMTP server for %s", to_email)
    return {"ok": True, "error_class": "", "sent_possible": True, "detail": ""}


def send_application_email(**kwargs) -> bool:
    """Boolean wrapper around send_application_email_detailed (legacy callers)."""
    return bool(send_application_email_detailed(**kwargs)["ok"])
