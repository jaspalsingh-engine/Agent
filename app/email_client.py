"""
Resend email client — outbound only.
Reply monitoring is manual via the dashboard Log Reply flow.
"""
import resend
from typing import Dict
from app.config import settings

resend.api_key = settings.resend_api_key


def send_email(to: str, subject: str, body: str, reply_to_subject: str = "") -> Dict[str, str]:
    """
    Send a plain-text email via Resend.
    Returns {id} from Resend.
    """
    params: resend.Emails.SendParams = {
        "from": settings.resend_from_email,
        "to": [to],
        "subject": subject,
        "text": body,
    }
    # Add In-Reply-To header for threading if this is a follow-up
    if reply_to_subject:
        params["headers"] = {"In-Reply-To": reply_to_subject}

    resp = resend.Emails.send(params)
    return {"id": resp["id"]}


def send_html_email(to: str, subject: str, html: str, plain: str) -> Dict[str, str]:
    """Send email with both HTML and plain text."""
    params: resend.Emails.SendParams = {
        "from": settings.resend_from_email,
        "to": [to],
        "subject": subject,
        "html": html,
        "text": plain,
    }
    resp = resend.Emails.send(params)
    return {"id": resp["id"]}
