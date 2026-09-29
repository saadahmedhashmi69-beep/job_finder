"""Send tailored job applications via email.

This is used by the UI "Approve & Send" flow.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


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


def send_application_email(
    *,
    to_email: str,
    subject: str,
    body: str,
    cv_path: Path,
    cover_letter_path: Optional[Path] = None,
    gmail_user: Optional[str] = None,
    gmail_app_password: Optional[str] = None,
) -> bool:
    """Send an application email with attachments via Gmail SMTP."""
    gmail_user = gmail_user or os.environ.get("GMAIL_USER")
    gmail_app_password = gmail_app_password or os.environ.get("GMAIL_APP_PASSWORD")

    if not gmail_user or not gmail_app_password:
        logger.error("GMAIL_USER or GMAIL_APP_PASSWORD not set.")
        return False

    cv_path = Path(cv_path)
    if not cv_path.exists():
        logger.error("CV not found: %s", cv_path)
        return False

    msg = MIMEMultipart()
    msg["From"] = gmail_user
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body or "", "plain"))

    # Attach CV
    with open(cv_path, "rb") as f:
        part = MIMEApplication(f.read(), Name=cv_path.name)
    part["Content-Disposition"] = f'attachment; filename="{cv_path.name}"'
    msg.attach(part)

    # Attach cover letter if present
    if cover_letter_path and cover_letter_path.exists():
        with open(cover_letter_path, "rb") as f:
            part = MIMEApplication(f.read(), Name=cover_letter_path.name)
        part["Content-Disposition"] = f'attachment; filename="{cover_letter_path.name}"'
        msg.attach(part)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_app_password)
            server.send_message(msg)
        logger.info("Application email sent to %s", to_email)
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("Gmail auth failed (check app password).")
        return False
    except Exception as e:
        logger.error("Failed to send application email: %s", e)
        return False

