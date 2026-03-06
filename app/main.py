"""
FastAPI app — approval dashboard + API.
"""
import os
from datetime import datetime, timedelta

from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.db import (
    init_db, get_db, WeeklyBatch, Account, Contact,
    OutreachVariant, TouchTask, ReplyEvent
)
from app.config import settings
from app.scheduler import start_scheduler, run_weekly_discovery, run_daily_touch_sequence
import app.apollo as apollo
import app.ai as ai
import app.email_client as emailer

os.makedirs("data", exist_ok=True)

app = FastAPI(title="SDR Bot", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="app/templates")


@app.on_event("startup")
def startup():
    init_db()
    start_scheduler()


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    batches = (
        db.query(WeeklyBatch)
        .order_by(desc(WeeklyBatch.created_at))
        .limit(10)
        .all()
    )
    total_approved = db.query(Account).filter_by(status="approved").count()
    total_sent = db.query(TouchTask).filter_by(status="sent").count()
    hot_leads = db.query(ReplyEvent).filter_by(sentiment="hot").count()
    li_pending = db.query(TouchTask).filter_by(status="manual_pending").count()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "batches": batches,
        "stats": {
            "approved": total_approved,
            "emails_sent": total_sent,
            "hot_leads": hot_leads,
            "li_pending": li_pending,
        },
    })


# ── Batch Review ──────────────────────────────────────────────────────────────

@app.get("/batch/{token}", response_class=HTMLResponse)
def batch_review(token: str, request: Request, db: Session = Depends(get_db)):
    batch = db.query(WeeklyBatch).filter_by(token=token).first()
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    accounts = (
        db.query(Account)
        .filter_by(batch_id=batch.id)
        .order_by(desc(Account.propensity_score))
        .all()
    )
    return templates.TemplateResponse("batch_review.html", {
        "request": request,
        "batch": batch,
        "accounts": accounts,
        "pending_count": sum(1 for a in accounts if a.status == "pending"),
        "approved_count": sum(1 for a in accounts if a.status == "approved"),
        "rejected_count": sum(1 for a in accounts if a.status == "rejected"),
    })


# ── Account Detail ────────────────────────────────────────────────────────────

@app.get("/account/{account_id}", response_class=HTMLResponse)
def account_detail(account_id: int, request: Request, db: Session = Depends(get_db)):
    acc = db.get(Account, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    contacts = db.query(Contact).filter_by(account_id=account_id).order_by(Contact.rank).all()
    email_variants = (
        db.query(OutreachVariant)
        .filter_by(account_id=account_id, channel="email")
        .order_by(OutreachVariant.variant_index)
        .all()
    )
    li_variants = (
        db.query(OutreachVariant)
        .filter_by(account_id=account_id, channel="linkedin")
        .order_by(OutreachVariant.variant_index)
        .all()
    )
    touches = (
        db.query(TouchTask)
        .filter_by(account_id=account_id)
        .order_by(TouchTask.touch_number)
        .all()
    )
    replies = (
        db.query(ReplyEvent)
        .filter_by(account_id=account_id)
        .order_by(desc(ReplyEvent.flagged_at))
        .all()
    )
    batch = db.get(WeeklyBatch, acc.batch_id)

    return templates.TemplateResponse("account_detail.html", {
        "request": request,
        "account": acc,
        "contacts": contacts,
        "email_variants": email_variants,
        "li_variants": li_variants,
        "touches": touches,
        "replies": replies,
        "batch": batch,
    })


# ── Approve ───────────────────────────────────────────────────────────────────

@app.post("/approve/{account_id}")
def approve_account(
    account_id: int,
    email_variant: int = Form(...),
    li_variant: int = Form(...),
    db: Session = Depends(get_db),
):
    acc = db.get(Account, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    if acc.status == "approved":
        return RedirectResponse(f"/account/{account_id}", status_code=303)

    # Reveal contact email — costs 1 Apollo credit
    primary = db.query(Contact).filter_by(account_id=account_id, rank=1).first()
    if primary and primary.apollo_person_id and not primary.revealed:
        email = apollo.reveal_contact_email(primary.apollo_person_id)
        if email:
            primary.email = email
            primary.revealed = True

    acc.status = "approved"
    acc.approved_at = datetime.utcnow()
    acc.selected_email_variant = email_variant
    acc.selected_li_variant = li_variant

    _create_touch_sequence(acc, db)
    db.commit()

    batch = db.get(WeeklyBatch, acc.batch_id)
    return RedirectResponse(f"/batch/{batch.token}" if batch else f"/account/{account_id}", status_code=303)


def _create_touch_sequence(acc: Account, db: Session):
    base = datetime.utcnow()
    for touch_num, channel, delta_days in [
        (1, "email",    0),
        (2, "linkedin", 2),
        (3, "email",    6),
        (4, "linkedin", 13),
        (5, "email",    20),
    ]:
        db.add(TouchTask(
            account_id=acc.id,
            touch_number=touch_num,
            channel=channel,
            scheduled_date=base + timedelta(days=delta_days),
            status="pending",
        ))


# ── Reject ────────────────────────────────────────────────────────────────────

@app.post("/reject/{account_id}")
def reject_account(account_id: int, db: Session = Depends(get_db)):
    acc = db.get(Account, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    acc.status = "rejected"
    acc.rejected_at = datetime.utcnow()
    db.commit()
    batch = db.get(WeeklyBatch, acc.batch_id)
    return RedirectResponse(f"/batch/{batch.token}" if batch else "/", status_code=303)


# ── Mark LI Touch Done ────────────────────────────────────────────────────────

@app.post("/touch/{touch_id}/done")
def mark_touch_done(touch_id: int, db: Session = Depends(get_db)):
    task = db.get(TouchTask, touch_id)
    if not task:
        raise HTTPException(status_code=404, detail="Touch not found")
    task.status = "manual_done"
    task.sent_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(f"/account/{task.account_id}", status_code=303)


# ── Log Reply (manual — user pastes snippet from their inbox) ─────────────────

@app.post("/account/{account_id}/log-reply")
def log_reply(
    account_id: int,
    from_address: str = Form(...),
    snippet: str = Form(...),
    sentiment: str = Form(...),
    db: Session = Depends(get_db),
):
    acc = db.get(Account, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    # If sentiment not provided, classify with AI
    if sentiment == "auto":
        sentiment = ai.classify_reply(snippet, from_address)

    event = ReplyEvent(
        account_id=account_id,
        from_address=from_address,
        snippet=snippet,
        sentiment=sentiment,
    )
    db.add(event)

    # Send hot lead alert email
    if sentiment == "hot" and settings.digest_email_recipient:
        contact = db.query(Contact).filter_by(account_id=account_id, rank=1).first()
        contact_str = f"{contact.first_name} {contact.last_name}, {contact.title}" if contact else "Unknown"
        try:
            emailer.send_email(
                to=settings.digest_email_recipient,
                subject=f"[HOT LEAD] {acc.name} replied — book the meeting",
                body=f"""They replied. Go get it.

Company: {acc.name}
Contact: {contact_str}
From: {from_address}
Their message: "{snippet}"

Score: {int(acc.propensity_score)} | Signal: {acc.trigger_signal}

Send your booking link:
{settings.your_calendly_link}

— SDR Bot""",
            )
            event.alert_sent = True
        except Exception as e:
            print(f"[App] Hot lead alert error: {e}")

    db.commit()
    return RedirectResponse(f"/account/{account_id}", status_code=303)


# ── Hot Leads ─────────────────────────────────────────────────────────────────

@app.get("/hot-leads", response_class=HTMLResponse)
def hot_leads(request: Request, db: Session = Depends(get_db)):
    events = (
        db.query(ReplyEvent)
        .filter_by(sentiment="hot")
        .order_by(desc(ReplyEvent.flagged_at))
        .all()
    )
    enriched = [
        {
            "event": e,
            "account": db.get(Account, e.account_id),
            "contact": db.query(Contact).filter_by(account_id=e.account_id, rank=1).first(),
        }
        for e in events
    ]
    return templates.TemplateResponse("hot_leads.html", {
        "request": request,
        "leads": enriched,
    })


# ── LinkedIn Queue ────────────────────────────────────────────────────────────

@app.get("/linkedin-queue", response_class=HTMLResponse)
def linkedin_queue(request: Request, db: Session = Depends(get_db)):
    tasks = (
        db.query(TouchTask)
        .filter_by(status="manual_pending", channel="linkedin")
        .order_by(TouchTask.scheduled_date)
        .all()
    )
    enriched = []
    for t in tasks:
        acc = db.get(Account, t.account_id)
        contact = db.query(Contact).filter_by(account_id=t.account_id, rank=1).first()
        li_variant = db.query(OutreachVariant).filter_by(
            account_id=t.account_id, channel="linkedin",
            variant_index=acc.selected_li_variant or 0,
        ).first()
        enriched.append({
            "task": t, "account": acc, "contact": contact,
            "script": li_variant.body if li_variant else "(No script generated)",
        })

    return templates.TemplateResponse("linkedin_queue.html", {
        "request": request,
        "tasks": enriched,
    })


# ── Admin: Manual Triggers ────────────────────────────────────────────────────

@app.post("/admin/run-discovery")
def manual_discovery():
    import threading
    threading.Thread(target=run_weekly_discovery, daemon=True).start()
    return RedirectResponse("/", status_code=303)


@app.post("/admin/run-touches")
def manual_touches():
    import threading
    threading.Thread(target=run_daily_touch_sequence, daemon=True).start()
    return RedirectResponse("/", status_code=303)
