"""
Thin SMTP sender, supporting both port 465 (implicit SSL) and 587
(STARTTLS) — SiteGround-hosted mailboxes commonly use 465. Callers should
check smtp_configured() first; if it's False, send_email() isn't called at
all, so the trigger pipeline can run (and be tested) before real mail
credentials exist.
"""

import os
import smtplib
import ssl
from email.message import EmailMessage


def smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER"))


def send_email(to: str, subject: str, body: str) -> None:
    """Raises on failure; caller is responsible for catching and logging."""
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    from_addr = os.environ.get("SMTP_FROM", user)

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    context = ssl.create_default_context()
    if port == 465:
        # Implicit SSL (SMTPS) — the connection is encrypted from the start.
        with smtplib.SMTP_SSL(host, port, context=context) as server:
            server.login(user, password)
            server.send_message(msg)
    else:
        # 587 (or anything else) — plaintext connect, then upgrade via STARTTLS.
        with smtplib.SMTP(host, port) as server:
            server.starttls(context=context)
            server.login(user, password)
            server.send_message(msg)
