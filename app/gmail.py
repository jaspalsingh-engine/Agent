"""
Gmail API client — send emails + monitor replies.
Uses OAuth2 with local credential storage.
"""
import base64
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional, Dict, Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app.config import settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]


def _get_service():
    creds = None
    token_path = settings.gmail_token_path
    creds_path = settings.gmail_credentials_path

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        os.makedirs(os.path.dirname(token_path), exist_ok=True)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def send_email(
    to: str,
    subject: str,
    body: str,
    thread_id: Optional[str] = None,
    reply_to_message_id: Optional[str] = None,
) -> Dict[str, str]:
    """
    Send an email from the configured Gmail account.
    Returns {message_id, thread_id}.
    """
    service = _get_service()

    msg = MIMEMultipart("alternative")
    msg["to"] = to
    msg["from"] = settings.gmail_sender_address
    msg["subject"] = subject
    if reply_to_message_id:
        msg["In-Reply-To"] = reply_to_message_id
        msg["References"] = reply_to_message_id

    msg.attach(MIMEText(body, "plain"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    send_kwargs: Dict[str, Any] = {"userId": "me", "body": {"raw": raw}}
    if thread_id:
        send_kwargs["body"]["threadId"] = thread_id

    sent = service.users().messages().send(**send_kwargs).execute()
    return {"message_id": sent["id"], "thread_id": sent.get("threadId", "")}


def send_html_email(to: str, subject: str, html: str, plain: str) -> Dict[str, str]:
    """Send an email with both HTML and plain text parts."""
    service = _get_service()

    msg = MIMEMultipart("alternative")
    msg["to"] = to
    msg["from"] = settings.gmail_sender_address
    msg["subject"] = subject
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()
    return {"message_id": sent["id"], "thread_id": sent.get("threadId", "")}


def get_unread_replies_since(after_timestamp: int) -> List[Dict[str, Any]]:
    """
    Find unread replies to emails we sent (in INBOX, after a given timestamp).
    Returns list of message metadata dicts.
    """
    service = _get_service()
    query = f"in:inbox is:unread after:{after_timestamp}"
    try:
        result = service.users().messages().list(
            userId="me", q=query, maxResults=50
        ).execute()
        messages = result.get("messages", [])
        enriched = []
        for m in messages:
            detail = service.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["From", "Subject", "In-Reply-To", "References"],
            ).execute()
            headers = {
                h["name"]: h["value"]
                for h in detail.get("payload", {}).get("headers", [])
            }
            enriched.append({
                "message_id": detail["id"],
                "thread_id": detail.get("threadId", ""),
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "in_reply_to": headers.get("In-Reply-To", ""),
                "snippet": detail.get("snippet", ""),
            })
        return enriched
    except Exception as e:
        print(f"[Gmail] get_unread_replies error: {e}")
        return []


def mark_as_read(message_id: str):
    try:
        service = _get_service()
        service.users().messages().modify(
            userId="me", id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
    except Exception as e:
        print(f"[Gmail] mark_as_read error: {e}")
