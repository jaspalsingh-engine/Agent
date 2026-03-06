"""
APScheduler jobs:
- Monday 7 AM: weekly discovery + scoring + digest email
- Daily 8 AM:  touch sequence runner (sends due emails, flags due LI touches)

Contact finding is manual — Apollo free API doesn't expose contact details.
Each account card links to Apollo web + LinkedIn search to find the right person.
"""
import secrets
from datetime import datetime, timedelta
from typing import List, Dict, Any

from sqlalchemy.orm import Session
from apscheduler.schedulers.background import BackgroundScheduler

from app.db import engine, WeeklyBatch, Account, OutreachVariant, TouchTask, Contact
from app.config import settings
import app.apollo as apollo
import app.ai as ai
import app.email_client as emailer

scheduler = BackgroundScheduler()


def run_weekly_discovery():
    print(f"[Scheduler] Weekly discovery starting at {datetime.utcnow()}")
    with Session(engine) as db:
        existing_ids = {r[0] for r in db.query(Account.apollo_org_id).all()}

        # Pull across all industry keyword groups (7 groups × 25 = up to 175 candidates)
        raw_companies: List[Dict[str, Any]] = []
        for page in range(1, len(apollo.INDUSTRY_KEYWORD_GROUPS) + 1):
            batch = apollo.search_companies(page=page, per_page=25)
            new = [c for c in batch if c.get("id") and c["id"] not in existing_ids]
            raw_companies.extend(new)

        # Deduplicate within batch (same company can appear in multiple keyword groups)
        seen = {}
        for c in raw_companies:
            if c["id"] not in seen:
                seen[c["id"]] = c
        raw_companies = list(seen.values())

        print(f"[Scheduler] {len(raw_companies)} new candidate companies (deduplicated)")
        if not raw_companies:
            print("[Scheduler] No new companies — skipping batch")
            return

        # Score with OpenAI
        scores = ai.score_all_companies(raw_companies)
        score_map = {s["apollo_org_id"]: s for s in scores}
        for c in raw_companies:
            s = score_map.get(c["id"], {})
            c["_score"]          = s.get("score", 0)
            c["_reasoning"]      = s.get("reasoning", "")
            c["_trigger_signal"] = s.get("trigger_signal", "")

        # Take top N above threshold
        top = sorted(raw_companies, key=lambda x: x["_score"], reverse=True)
        top = [c for c in top if c["_score"] >= 40][:settings.accounts_per_week]
        print(f"[Scheduler] {len(top)} accounts above score threshold")
        if not top:
            return

        # Create batch
        batch_token = secrets.token_urlsafe(32)
        weekly_batch = WeeklyBatch(token=batch_token, week_start=datetime.utcnow())
        db.add(weekly_batch)
        db.flush()

        accounts_created = []
        # Refresh existing_ids — background thread may have inserted some already
        existing_ids = {r[0] for r in db.query(Account.apollo_org_id).all()}
        top = [c for c in top if c["id"] not in existing_ids]

        for c in top:
            acc = Account(
                batch_id=weekly_batch.id,
                apollo_org_id=c["id"],
                name=c.get("name", ""),
                domain=c.get("primary_domain", ""),
                industry=c.get("industry", ""),
                employee_count=c.get("estimated_num_employees"),
                annual_revenue=c.get("annual_revenue_printed", ""),
                city=c.get("city", ""),
                state=c.get("state", ""),
                linkedin_url=c.get("linkedin_url", ""),
                description=(c.get("short_description") or "")[:500],
                propensity_score=c["_score"],
                score_reasoning=c["_reasoning"],
                trigger_signal=c["_trigger_signal"],
            )
            db.add(acc)
            db.flush()

            # Generate outreach variants (no contact name yet — user adds that)
            outreach = ai.generate_outreach(
                account={
                    "name": c.get("name", ""),
                    "industry": c.get("industry", ""),
                    "employee_count": c.get("estimated_num_employees"),
                    "city": acc.city,
                    "state": acc.state,
                    "trigger_signal": c["_trigger_signal"],
                },
                ranked_contacts=[],
            )
            for v in outreach.get("emails", []):
                db.add(OutreachVariant(
                    account_id=acc.id, channel="email",
                    variant_index=v["variant_index"], style_label=v["style_label"],
                    subject=v.get("subject", ""), body=v.get("body", ""),
                ))
            for v in outreach.get("linkedin", []):
                db.add(OutreachVariant(
                    account_id=acc.id, channel="linkedin",
                    variant_index=v["variant_index"], style_label=v["style_label"],
                    body=v.get("body", ""),
                ))

            accounts_created.append(acc)

        db.commit()
        print(f"[Scheduler] Persisted {len(accounts_created)} accounts")
        _send_weekly_digest(weekly_batch, accounts_created)
        weekly_batch.digest_sent = True
        db.commit()
        print("[Scheduler] Weekly discovery complete")


def _send_weekly_digest(batch: WeeklyBatch, accounts: List[Account]):
    if not settings.digest_email_recipient or not settings.gmail_smtp_user:
        print("[Scheduler] Email not configured — skipping digest (check dashboard instead)")
        return
    review_url = f"{settings.dashboard_url}/batch/{batch.token}"
    rows_text = "\n".join(
        f"  {i}. {a.name} | {a.industry} | Score: {int(a.propensity_score)} | {a.trigger_signal}"
        for i, a in enumerate(accounts[:10], 1)
    )
    subject = f"[SDR Bot] {len(accounts)} accounts ready — week of {batch.week_start.strftime('%b %d')}"
    plain = (
        f"Hi {settings.your_name.split()[0]},\n\n"
        f"{len(accounts)} accounts scored and outreach drafted.\n\n"
        f"Top 10:\n{rows_text}\n\n"
        f"Review + approve here:\n{review_url}\n\n— SDR Bot"
    )
    try:
        emailer.send_html_email(
            to=settings.digest_email_recipient, subject=subject,
            html=_digest_html(batch, accounts, review_url), plain=plain,
        )
        print(f"[Scheduler] Digest sent to {settings.digest_email_recipient}")
    except Exception as e:
        print(f"[Scheduler] Digest email error: {e}")


def _digest_html(batch: WeeklyBatch, accounts: List[Account], review_url: str) -> str:
    rows = ""
    for acc in accounts[:20]:
        color = "#16a34a" if acc.propensity_score >= 75 else "#d97706" if acc.propensity_score >= 55 else "#6b7280"
        rows += (
            f'<tr style="border-bottom:1px solid #e5e7eb;">'
            f'<td style="padding:10px 8px;font-weight:600;">{acc.name}</td>'
            f'<td style="padding:10px 8px;color:#6b7280;">{acc.industry or "—"}</td>'
            f'<td style="padding:10px 8px;color:{color};font-weight:700;">{int(acc.propensity_score)}</td>'
            f'<td style="padding:10px 8px;font-size:13px;">{acc.trigger_signal or "—"}</td>'
            f'</tr>'
        )
    return (
        f'<!DOCTYPE html><html><body style="font-family:sans-serif;background:#f9fafb;padding:20px;">'
        f'<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;">'
        f'<div style="background:#0f172a;padding:28px 32px;">'
        f'<h1 style="color:#fff;margin:0;font-size:20px;">SDR Bot — Weekly Prospects</h1>'
        f'<p style="color:#94a3b8;margin:6px 0 0;font-size:14px;">Week of {batch.week_start.strftime("%B %d, %Y")} · {len(accounts)} accounts</p>'
        f'</div><div style="padding:28px 32px;">'
        f'<a href="{review_url}" style="display:inline-block;background:#2563eb;color:#fff;padding:14px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:15px;margin-bottom:28px;">Review &amp; Approve →</a>'
        f'<table style="width:100%;border-collapse:collapse;font-size:14px;">'
        f'<thead><tr style="background:#f1f5f9;"><th style="padding:10px 8px;text-align:left;">Company</th><th style="padding:10px 8px;text-align:left;">Industry</th><th style="padding:10px 8px;text-align:left;">Score</th><th style="padding:10px 8px;text-align:left;">Signal</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
        f'{"<p style=color:#6b7280;font-size:13px;margin-top:12px;>+ " + str(len(accounts)-20) + " more in dashboard</p>" if len(accounts) > 20 else ""}'
        f'</div></div></body></html>'
    )


def run_daily_touch_sequence():
    print(f"[Scheduler] Daily touch sequence at {datetime.utcnow()}")
    now = datetime.utcnow()
    with Session(engine) as db:
        due = db.query(TouchTask).filter(
            TouchTask.status == "pending",
            TouchTask.scheduled_date <= now,
        ).all()
        print(f"[Scheduler] {len(due)} due touch tasks")
        for task in due:
            acc = db.get(Account, task.account_id)
            if not acc or acc.status != "approved":
                task.status = "skipped"
                continue
            if task.channel == "linkedin":
                task.status = "manual_pending"
                continue
            primary = db.query(Contact).filter_by(account_id=acc.id, rank=1).first()
            if not primary or not primary.email:
                task.status = "skipped"
                continue
            if task.touch_number == 1:
                variant = db.query(OutreachVariant).filter_by(
                    account_id=acc.id, channel="email",
                    variant_index=acc.selected_email_variant or 0,
                ).first()
                if not variant:
                    task.status = "skipped"
                    continue
                subject = variant.subject
                body = variant.body.replace("[First Name]", primary.first_name or "there")
            else:
                fv = db.query(OutreachVariant).filter_by(
                    account_id=acc.id, channel="email",
                    variant_index=acc.selected_email_variant or 0,
                ).first()
                subject, body = ai.generate_followup_email(
                    account={"name": acc.name, "industry": acc.industry, "trigger_signal": acc.trigger_signal},
                    contact_name=primary.first_name or "",
                    touch_number=task.touch_number,
                    original_subject=fv.subject if fv else "",
                )
                body = body.replace("[First Name]", primary.first_name or "there")
            try:
                result = emailer.send_email(to=primary.email, subject=subject, body=body)
                task.resend_email_id = result.get("id")
                task.status = "sent"
                task.sent_at = datetime.utcnow()
                print(f"[Scheduler] Touch {task.touch_number} → {primary.email} ({acc.name})")
            except Exception as e:
                print(f"[Scheduler] Send error {acc.name}: {e}")
        db.commit()


def start_scheduler():
    scheduler.add_job(run_weekly_discovery, "cron", day_of_week="mon", hour=7, minute=0,
                      id="weekly_discovery", replace_existing=True)
    scheduler.add_job(run_daily_touch_sequence, "cron", hour=8, minute=0,
                      id="daily_touches", replace_existing=True)
    scheduler.start()
    print("[Scheduler] Started — weekly discovery Mon 7 AM, daily touches 8 AM")
