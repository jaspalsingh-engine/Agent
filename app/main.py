"""
FastAPI app — approval dashboard + API.
"""
import os
from datetime import datetime, timedelta
from typing import Optional

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

os.makedirs("data", exist_ok=True)
os.makedirs("credentials", exist_ok=True)

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
    # Stats
    total_approved = db.query(Account).filter_by(status="approved").count()
    total_sent = db.query(TouchTask).filter_by(status="sent").count()
    hot_leads = db.query(ReplyEvent).filter_by(sentiment="hot").count()
    manual_pending = db.query(TouchTask).filter_by(status="manual_pending").count()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "batches": batches,
        "stats": {
            "approved": total_approved,
            "emails_sent": total_sent,
            "hot_leads": hot_leads,
            "li_pending": manual_pending,
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

    # Reveal contact email (costs 1 Apollo credit)
    primary_contact = db.query(Contact).filter_by(account_id=account_id, rank=1).first()
    if primary_contact and primary_contact.apollo_person_id and not primary_contact.revealed:
        email = apollo.reveal_contact_email(primary_contact.apollo_person_id)
        if email:
            primary_contact.email = email
            primary_contact.revealed = True

    acc.status = "approved"
    acc.approved_at = datetime.utcnow()
    acc.selected_email_variant = email_variant
    acc.selected_li_variant = li_variant

    # Create touch sequence
    _create_touch_sequence(acc, db)
    db.commit()

    batch = db.get(WeeklyBatch, acc.batch_id)
    redirect_url = f"/batch/{batch.token}" if batch else f"/account/{account_id}"
    return RedirectResponse(redirect_url, status_code=303)


def _create_touch_sequence(acc: Account, db: Session):
    """Create the 5-touch sequence for an approved account."""
    base = datetime.utcnow()
    schedule = [
        (1, "email",    base),                          # Day 1 — initial email
        (2, "linkedin", base + timedelta(days=2)),      # Day 3 — LI connection note
        (3, "email",    base + timedelta(days=6)),      # Day 7 — follow-up email
        (4, "linkedin", base + timedelta(days=13)),     # Day 14 — LI follow-up
        (5, "email",    base + timedelta(days=20)),     # Day 21 — breakup email
    ]
    for touch_num, channel, scheduled_date in schedule:
        db.add(TouchTask(
            account_id=acc.id,
            touch_number=touch_num,
            channel=channel,
            scheduled_date=scheduled_date,
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
    redirect_url = f"/batch/{batch.token}" if batch else "/"
    return RedirectResponse(redirect_url, status_code=303)


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


# ── Hot Leads ─────────────────────────────────────────────────────────────────

@app.get("/hot-leads", response_class=HTMLResponse)
def hot_leads(request: Request, db: Session = Depends(get_db)):
    events = (
        db.query(ReplyEvent)
        .filter_by(sentiment="hot")
        .order_by(desc(ReplyEvent.flagged_at))
        .all()
    )
    enriched = []
    for e in events:
        acc = db.get(Account, e.account_id)
        contact = db.query(Contact).filter_by(account_id=e.account_id, rank=1).first()
        enriched.append({"event": e, "account": acc, "contact": contact})

    return templates.TemplateResponse("hot_leads.html", {
        "request": request,
        "leads": enriched,
    })


# ── LinkedIn Pending ──────────────────────────────────────────────────────────

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
        # Get selected LI variant
        li_variant = (
            db.query(OutreachVariant)
            .filter_by(
                account_id=t.account_id,
                channel="linkedin",
                variant_index=acc.selected_li_variant or 0,
            )
            .first()
        )
        enriched.append({
            "task": t,
            "account": acc,
            "contact": contact,
            "script": li_variant.body if li_variant else "(No script generated)",
        })

    return templates.TemplateResponse("linkedin_queue.html", {
        "request": request,
        "tasks": enriched,
    })


# ── Admin: Manual Triggers ────────────────────────────────────────────────────

@app.post("/admin/run-discovery")
def manual_discovery():
    """Trigger weekly discovery manually (for testing)."""
    import threading
    threading.Thread(target=run_weekly_discovery, daemon=True).start()
    return RedirectResponse("/", status_code=303)


@app.post("/admin/run-touches")
def manual_touches():
    """Trigger touch sequence manually (for testing)."""
    import threading
    threading.Thread(target=run_daily_touch_sequence, daemon=True).start()
    return RedirectResponse("/", status_code=303)
