"""
Gmail SMTP email client — uses an app password, no OAuth required.
Sends from your personal Gmail. Replies go back to that inbox.

Setup:
  1. myaccount.google.com → Security → 2-Step Verification → turn on
  2. myaccount.google.com → Security → App passwords → Mail → generate
  3. Set GMAIL_SMTP_USER and GMAIL_SMTP_PASSWORD in .env
"""
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict

from app.config import settings

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def _send(msg: MIMEMultipart) -> Dict[str, str]:
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(settings.gmail_smtp_user, settings.gmail_smtp_password)
        server.sendmail(
            from_addr=settings.gmail_smtp_user,
            to_addrs=msg["to"],
            msg=msg.as_string(),
        )
    return {"id": "smtp-ok"}


def send_email(to: str, subject: str, body: str, reply_to_subject: str = "") -> Dict[str, str]:
    msg = MIMEMultipart("alternative")
    msg["from"] = settings.gmail_smtp_user
    msg["to"] = to
    msg["subject"] = subject
    if reply_to_subject:
        msg["In-Reply-To"] = reply_to_subject
        msg["References"] = reply_to_subject
    msg.attach(MIMEText(body, "plain"))
    return _send(msg)


def send_html_email(to: str, subject: str, html: str, plain: str) -> Dict[str, str]:
    msg = MIMEMultipart("alternative")
    msg["from"] = settings.gmail_smtp_user
    msg["to"] = to
    msg["subject"] = subject
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))
    return _send(msg)
