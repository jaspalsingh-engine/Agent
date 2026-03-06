"""
APScheduler jobs:
- Monday 7 AM: weekly discovery + scoring + digest email
- Daily 8 AM:  touch sequence runner (send due emails, flag due LI touches)
- Every 2 hrs: reply monitor
"""
import secrets
from datetime import datetime, timedelta
from typing import List, Dict, Any

from sqlalchemy.orm import Session
from apscheduler.schedulers.background import BackgroundScheduler

from app.db import engine, WeeklyBatch, Account, Contact, OutreachVariant, TouchTask, ReplyEvent
from app.config import settings
import app.apollo as apollo
import app.ai as ai
import app.gmail as gmail


scheduler = BackgroundScheduler()


# ── Weekly Discovery ──────────────────────────────────────────────────────────

def run_weekly_discovery():
    """
    1. Search Apollo for companies
    2. Score with Claude
    3. Select top N by score
    4. Generate outreach variants
    5. Persist to DB
    6. Send digest email
    """
    print(f"[Scheduler] Weekly discovery starting at {datetime.utcnow()}")
    with Session(engine) as db:
        # ── Fetch companies from Apollo ──────────────────────────────────────
        raw_companies: List[Dict[str, Any]] = []
        page = 1
        while len(raw_companies) < 200:
            batch = apollo.search_companies(page=page, per_page=100)
            if not batch:
                break
            # Exclude companies already in our DB
            existing_ids = {r[0] for r in db.query(Account.apollo_org_id).all()}
            new = [c for c in batch if c.get("id") not in existing_ids]
            raw_companies.extend(new)
            page += 1
            if page > 3:  # max 3 pages to stay within rate limits
                break

        print(f"[Scheduler] Found {len(raw_companies)} candidate companies")

        if not raw_companies:
            print("[Scheduler] No new companies found — skipping batch")
            return

        # ── Score with Claude ────────────────────────────────────────────────
        scores = ai.score_all_companies(raw_companies)
        score_map = {s["apollo_org_id"]: s for s in scores}

        # Attach scores to raw companies
        for c in raw_companies:
            cid = c.get("id", "")
            if cid in score_map:
                c["_score"] = score_map[cid]["score"]
                c["_reasoning"] = score_map[cid]["reasoning"]
                c["_trigger_signal"] = score_map[cid]["trigger_signal"]
            else:
                c["_score"] = 0

        # Sort by score, take top N
        top = sorted(raw_companies, key=lambda x: x["_score"], reverse=True)
        top = [c for c in top if c["_score"] >= 40][:settings.accounts_per_week]
        print(f"[Scheduler] Selected {len(top)} accounts above threshold")

        if not top:
            print("[Scheduler] No accounts above score threshold")
            return

        # ── Create weekly batch ──────────────────────────────────────────────
        batch_token = secrets.token_urlsafe(32)
        batch = WeeklyBatch(
            token=batch_token,
            week_start=datetime.utcnow(),
        )
        db.add(batch)
        db.flush()

        # ── Persist accounts + search for contacts (no credit use) ───────────
        accounts_created = []
        for c in top:
            # Get raw contacts (emails obfuscated, no credits used)
            raw_people = apollo.search_people_for_company(
                org_domain=c.get("primary_domain", ""),
                org_name=c.get("name", ""),
                industry=c.get("industry", ""),
            )

            acc = Account(
                batch_id=batch.id,
                apollo_org_id=c.get("id", ""),
                name=c.get("name", ""),
                domain=c.get("primary_domain", ""),
                industry=c.get("industry", ""),
                employee_count=c.get("estimated_num_employees"),
                annual_revenue=c.get("annual_revenue_printed", ""),
                city=c.get("city", "") or (c.get("locations") or [{}])[0].get("city", ""),
                state=c.get("state", "") or (c.get("locations") or [{}])[0].get("state", ""),
                linkedin_url=c.get("linkedin_url", ""),
                description=(c.get("short_description") or "")[:500],
                propensity_score=c["_score"],
                score_reasoning=c["_reasoning"],
                trigger_signal=c["_trigger_signal"],
            )
            db.add(acc)
            db.flush()

            # Persist contact stubs (no email revealed yet)
            from app.apollo import _title_rank
            sorted_people = sorted(
                raw_people,
                key=lambda p: _title_rank(p.get("title", ""), c.get("industry", ""))
            )
            for rank_idx, person in enumerate(sorted_people[:2], start=1):
                contact = Contact(
                    account_id=acc.id,
                    apollo_person_id=person.get("id", ""),
                    first_name=person.get("first_name", ""),
                    last_name=person.get("last_name", ""),
                    title=person.get("title", ""),
                    linkedin_url=person.get("linkedin_url", ""),
                    rank=rank_idx,
                    rank_reason=_rank_reason(person.get("title", ""), c.get("industry", "")),
                    revealed=False,
                )
                db.add(contact)

            # ── Generate outreach variants ───────────────────────────────────
            contact_data = [
                {"name": p.get("first_name", ""), "title": p.get("title", "")}
                for p in sorted_people[:2]
            ]
            outreach = ai.generate_outreach(
                account={
                    "name": c.get("name", ""),
                    "industry": c.get("industry", ""),
                    "employee_count": c.get("estimated_num_employees"),
                    "city": acc.city,
                    "state": acc.state,
                    "trigger_signal": c["_trigger_signal"],
                },
                ranked_contacts=contact_data,
            )

            for email_v in outreach.get("emails", []):
                db.add(OutreachVariant(
                    account_id=acc.id,
                    channel="email",
                    variant_index=email_v["variant_index"],
                    style_label=email_v["style_label"],
                    subject=email_v.get("subject", ""),
                    body=email_v.get("body", ""),
                ))

            for li_v in outreach.get("linkedin", []):
                db.add(OutreachVariant(
                    account_id=acc.id,
                    channel="linkedin",
                    variant_index=li_v["variant_index"],
                    style_label=li_v["style_label"],
                    body=li_v.get("body", ""),
                ))

            accounts_created.append(acc)

        db.commit()
        print(f"[Scheduler] Persisted {len(accounts_created)} accounts")

        # ── Send digest email ────────────────────────────────────────────────
        _send_weekly_digest(batch, accounts_created)
        batch.digest_sent = True
        db.commit()
        print("[Scheduler] Weekly discovery complete")


def _rank_reason(title: str, industry: str) -> str:
    if not title:
        return "Available contact"
    t = title.lower()
    for kw in ["travel manager", "corporate travel"]:
        if kw in t:
            return "Travel decision maker"
    for kw in ["cfo", "vp finance", "finance director", "controller"]:
        if kw in t:
            return "Finance owner of T&E spend"
    for kw in ["ceo", "founder", "coo"]:
        if kw in t:
            return "Senior decision maker"
    for kw in ["vp operations", "operations director"]:
        if kw in t:
            return "Ops owner — controls field travel"
    return "Senior contact"


def _send_weekly_digest(batch: WeeklyBatch, accounts: List[Account]):
    """Send the weekly digest email to the user."""
    review_url = f"{settings.dashboard_url}/batch/{batch.token}"
    plain_rows = []
    for i, acc in enumerate(accounts[:10], start=1):
        plain_rows.append(
            f"  {i}. {acc.name} | {acc.industry} | Score: {int(acc.propensity_score)} | {acc.trigger_signal}"
        )

    subject = f"[SDR Bot] {len(accounts)} accounts ready for your review — week of {batch.week_start.strftime('%b %d')}"
    body = f"""Hi {settings.your_name.split()[0]},

Your weekly prospect batch is ready. {len(accounts)} accounts scored and outreach drafted.

Top 10 this week:
{chr(10).join(plain_rows)}
{'...' if len(accounts) > 10 else ''}

REVIEW + APPROVE HERE:
{review_url}

For each account you'll see:
  • Why it was selected (propensity signal)
  • Ranked contacts
  • 3 email variants to choose from
  • 2 LinkedIn message variants
  • Approve or Reject with one click

Once you approve, emails go out automatically. LinkedIn scripts are ready to copy.

—
SDR Bot (built to get you more than 6 meetings/month)"""

    try:
        gmail.send_html_email(
            to=settings.digest_email_recipient,
            subject=subject,
            html=_digest_html(batch, accounts, review_url),
            plain=body,
        )
        print(f"[Scheduler] Digest sent to {settings.digest_email_recipient}")
    except Exception as e:
        print(f"[Scheduler] Digest email error: {e}")


def _digest_html(batch: WeeklyBatch, accounts: List[Account], review_url: str) -> str:
    rows = ""
    for acc in accounts[:20]:
        score_color = "#16a34a" if acc.propensity_score >= 75 else "#d97706" if acc.propensity_score >= 55 else "#6b7280"
        rows += f"""
        <tr style="border-bottom:1px solid #e5e7eb;">
          <td style="padding:10px 8px;font-weight:600;">{acc.name}</td>
          <td style="padding:10px 8px;color:#6b7280;">{acc.industry}</td>
          <td style="padding:10px 8px;color:{score_color};font-weight:700;">{int(acc.propensity_score)}</td>
          <td style="padding:10px 8px;font-size:13px;">{acc.trigger_signal or ''}</td>
        </tr>"""

    return f"""<!DOCTYPE html><html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f9fafb;margin:0;padding:20px;">
<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1);">
  <div style="background:#0f172a;padding:28px 32px;">
    <h1 style="color:#fff;margin:0;font-size:20px;">SDR Bot — Weekly Prospects</h1>
    <p style="color:#94a3b8;margin:6px 0 0;font-size:14px;">Week of {batch.week_start.strftime('%B %d, %Y')} &nbsp;·&nbsp; {len(accounts)} accounts</p>
  </div>
  <div style="padding:28px 32px;">
    <a href="{review_url}" style="display:inline-block;background:#2563eb;color:#fff;padding:14px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:15px;margin-bottom:28px;">Review &amp; Approve Accounts →</a>
    <table style="width:100%;border-collapse:collapse;font-size:14px;">
      <thead>
        <tr style="background:#f1f5f9;">
          <th style="padding:10px 8px;text-align:left;">Company</th>
          <th style="padding:10px 8px;text-align:left;">Industry</th>
          <th style="padding:10px 8px;text-align:left;">Score</th>
          <th style="padding:10px 8px;text-align:left;">Top Signal</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    {'<p style="color:#6b7280;font-size:13px;margin-top:12px;">+ ' + str(len(accounts)-20) + ' more in the dashboard</p>' if len(accounts) > 20 else ''}
  </div>
  <div style="padding:16px 32px;background:#f8fafc;border-top:1px solid #e2e8f0;">
    <p style="color:#94a3b8;font-size:12px;margin:0;">This email was generated by your SDR Bot. Approve accounts to trigger outreach.</p>
  </div>
</div>
</body></html>"""


# ── Daily Touch Sequence ──────────────────────────────────────────────────────

def run_daily_touch_sequence():
    """
    Check all approved accounts for due touch tasks.
    - Email touches: send automatically
    - LinkedIn touches: flag as manual_pending (user copies from dashboard)
    """
    print(f"[Scheduler] Daily touch sequence at {datetime.utcnow()}")
    now = datetime.utcnow()

    with Session(engine) as db:
        due_tasks = (
            db.query(TouchTask)
            .filter(
                TouchTask.status == "pending",
                TouchTask.scheduled_date <= now,
            )
            .all()
        )
        print(f"[Scheduler] {len(due_tasks)} due touch tasks")

        for task in due_tasks:
            acc = db.get(Account, task.account_id)
            if not acc or acc.status != "approved":
                task.status = "skipped"
                continue

            primary_contact = (
                db.query(Contact)
                .filter_by(account_id=acc.id, rank=1)
                .first()
            )

            if task.channel == "linkedin":
                task.status = "manual_pending"
                continue

            # Email touch
            if not primary_contact or not primary_contact.email:
                task.status = "skipped"
                continue

            # Get the right email body
            if task.touch_number == 1:
                variant = (
                    db.query(OutreachVariant)
                    .filter_by(
                        account_id=acc.id,
                        channel="email",
                        variant_index=acc.selected_email_variant or 0,
                    )
                    .first()
                )
                if not variant:
                    task.status = "skipped"
                    continue
                subject = variant.subject
                body = variant.body.replace("[First Name]", primary_contact.first_name or "there")
            else:
                # Follow-up or breakup
                first_touch = (
                    db.query(TouchTask)
                    .filter_by(account_id=acc.id, touch_number=1)
                    .first()
                )
                orig_subject = ""
                if first_touch:
                    first_variant = (
                        db.query(OutreachVariant)
                        .filter_by(
                            account_id=acc.id,
                            channel="email",
                            variant_index=acc.selected_email_variant or 0,
                        )
                        .first()
                    )
                    orig_subject = first_variant.subject if first_variant else ""

                subject, body = ai.generate_followup_email(
                    account={"name": acc.name, "industry": acc.industry, "trigger_signal": acc.trigger_signal},
                    contact_name=primary_contact.first_name or "",
                    touch_number=task.touch_number,
                    original_subject=orig_subject,
                )
                body = body.replace("[First Name]", primary_contact.first_name or "there")

            try:
                # Find thread_id from touch 1 for reply threading
                thread_id = None
                if task.touch_number > 1:
                    t1 = db.query(TouchTask).filter_by(account_id=acc.id, touch_number=1).first()
                    if t1:
                        thread_id = t1.gmail_thread_id

                result = gmail.send_email(
                    to=primary_contact.email,
                    subject=subject,
                    body=body,
                    thread_id=thread_id,
                )
                task.gmail_message_id = result["message_id"]
                task.gmail_thread_id = result["thread_id"]
                task.status = "sent"
                task.sent_at = datetime.utcnow()
                print(f"[Scheduler] Sent touch {task.touch_number} to {primary_contact.email}")
            except Exception as e:
                print(f"[Scheduler] Email send error for {acc.name}: {e}")

        db.commit()


# ── Reply Monitor ─────────────────────────────────────────────────────────────

def run_reply_monitor():
    """
    Check Gmail for replies to our sent emails.
    Classify them and alert on hot replies.
    """
    print(f"[Scheduler] Reply monitor at {datetime.utcnow()}")
    cutoff = int((datetime.utcnow() - timedelta(days=30)).timestamp())

    with Session(engine) as db:
        # Build map of known thread_ids → account_id
        sent_tasks = (
            db.query(TouchTask)
            .filter(
                TouchTask.status == "sent",
                TouchTask.gmail_thread_id.isnot(None),
            )
            .all()
        )
        thread_map = {t.gmail_thread_id: t for t in sent_tasks}

        if not thread_map:
            return

        unread = gmail.get_unread_replies_since(cutoff)
        for msg in unread:
            thread_id = msg["thread_id"]
            if thread_id not in thread_map:
                continue

            task = thread_map[thread_id]
            existing = (
                db.query(ReplyEvent)
                .filter_by(gmail_message_id=msg["message_id"])
                .first()
            )
            if existing:
                continue

            sentiment = ai.classify_reply(msg["snippet"], msg["from"])
            event = ReplyEvent(
                account_id=task.account_id,
                touch_task_id=task.id,
                gmail_message_id=msg["message_id"],
                gmail_thread_id=thread_id,
                from_address=msg["from"],
                subject=msg["subject"],
                snippet=msg["snippet"],
                sentiment=sentiment,
            )
            db.add(event)
            gmail.mark_as_read(msg["message_id"])

            if sentiment == "hot":
                _send_hot_lead_alert(task.account_id, msg, db)
                event.alert_sent = True

        db.commit()


def _send_hot_lead_alert(account_id: int, msg: Dict, db: Session):
    acc = db.get(Account, account_id)
    if not acc:
        return
    contact = db.query(Contact).filter_by(account_id=account_id, rank=1).first()
    contact_str = f"{contact.first_name} {contact.last_name}, {contact.title}" if contact else "Unknown contact"

    subject = f"[HOT LEAD] {acc.name} replied — book the meeting"
    body = f"""They replied. Go get it.

Company: {acc.name}
Contact: {contact_str}
From: {msg['from']}
Their message: "{msg['snippet']}"

Propensity score: {int(acc.propensity_score)}
Why they were targeted: {acc.trigger_signal}

Reply to their email and send your Calendly:
{settings.your_calendly_link}

Once booked, log it in the dashboard and pass to your AE.

— SDR Bot"""
    try:
        gmail.send_email(to=settings.digest_email_recipient, subject=subject, body=body)
        print(f"[Scheduler] Hot lead alert sent for {acc.name}")
    except Exception as e:
        print(f"[Scheduler] Hot lead alert error: {e}")


# ── Register Jobs ─────────────────────────────────────────────────────────────

def start_scheduler():
    scheduler.add_job(
        run_weekly_discovery,
        trigger="cron",
        day_of_week="mon",
        hour=7,
        minute=0,
        id="weekly_discovery",
        replace_existing=True,
    )
    scheduler.add_job(
        run_daily_touch_sequence,
        trigger="cron",
        hour=8,
        minute=0,
        id="daily_touches",
        replace_existing=True,
    )
    scheduler.add_job(
        run_reply_monitor,
        trigger="interval",
        hours=2,
        id="reply_monitor",
        replace_existing=True,
    )
    scheduler.start()
    print("[Scheduler] Started — weekly discovery Mon 7 AM, daily touches 8 AM, reply monitor every 2 hrs")
