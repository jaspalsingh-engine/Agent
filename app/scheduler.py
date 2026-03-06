"""
APScheduler jobs:
- Monday 7 AM: weekly discovery + scoring + digest email
- Daily 8 AM:  touch sequence runner (sends due emails, flags due LI touches)

Reply monitoring is manual — user logs replies via the dashboard.
"""
import secrets
from datetime import datetime, timedelta
from typing import List, Dict, Any

from sqlalchemy.orm import Session
from apscheduler.schedulers.background import BackgroundScheduler

from app.db import engine, WeeklyBatch, Account, Contact, OutreachVariant, TouchTask
from app.config import settings
import app.apollo as apollo
import app.ai as ai
import app.email_client as emailer

scheduler = BackgroundScheduler()


# ── Weekly Discovery ──────────────────────────────────────────────────────────

def run_weekly_discovery():
    print(f"[Scheduler] Weekly discovery starting at {datetime.utcnow()}")
    with Session(engine) as db:
        # ── Fetch companies from Apollo ──────────────────────────────────────
        raw_companies: List[Dict[str, Any]] = []
        page = 1
        while len(raw_companies) < 200:
            batch = apollo.search_companies(page=page, per_page=100)
            if not batch:
                break
            existing_ids = {r[0] for r in db.query(Account.apollo_org_id).all()}
            new = [c for c in batch if c.get("id") not in existing_ids]
            raw_companies.extend(new)
            page += 1
            if page > 3:
                break

        print(f"[Scheduler] Found {len(raw_companies)} candidate companies")
        if not raw_companies:
            print("[Scheduler] No new companies found — skipping batch")
            return

        # ── Score with OpenAI ────────────────────────────────────────────────
        scores = ai.score_all_companies(raw_companies)
        score_map = {s["apollo_org_id"]: s for s in scores}

        for c in raw_companies:
            cid = c.get("id", "")
            if cid in score_map:
                c["_score"] = score_map[cid]["score"]
                c["_reasoning"] = score_map[cid]["reasoning"]
                c["_trigger_signal"] = score_map[cid]["trigger_signal"]
            else:
                c["_score"] = 0

        top = sorted(raw_companies, key=lambda x: x["_score"], reverse=True)
        top = [c for c in top if c["_score"] >= 40][:settings.accounts_per_week]
        print(f"[Scheduler] Selected {len(top)} accounts above threshold")

        if not top:
            print("[Scheduler] No accounts above score threshold")
            return

        # ── Create weekly batch ──────────────────────────────────────────────
        batch_token = secrets.token_urlsafe(32)
        weekly_batch = WeeklyBatch(token=batch_token, week_start=datetime.utcnow())
        db.add(weekly_batch)
        db.flush()

        # ── Persist accounts + contacts + outreach variants ──────────────────
        accounts_created = []
        for c in top:
            raw_people = apollo.search_people_for_company(
                org_domain=c.get("primary_domain", ""),
                org_name=c.get("name", ""),
                industry=c.get("industry", ""),
            )

            acc = Account(
                batch_id=weekly_batch.id,
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

            from app.apollo import _title_rank
            sorted_people = sorted(
                raw_people,
                key=lambda p: _title_rank(p.get("title", ""), c.get("industry", ""))
            )
            for rank_idx, person in enumerate(sorted_people[:2], start=1):
                db.add(Contact(
                    account_id=acc.id,
                    apollo_person_id=person.get("id", ""),
                    first_name=person.get("first_name", ""),
                    last_name=person.get("last_name", ""),
                    title=person.get("title", ""),
                    linkedin_url=person.get("linkedin_url", ""),
                    rank=rank_idx,
                    rank_reason=_rank_reason(person.get("title", ""), c.get("industry", "")),
                    revealed=False,
                ))

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


def _rank_reason(title: str, industry: str) -> str:
    if not title:
        return "Available contact"
    t = title.lower()
    if any(k in t for k in ["travel manager", "corporate travel"]):
        return "Travel decision maker"
    if any(k in t for k in ["cfo", "vp finance", "finance director", "controller"]):
        return "Finance owner of T&E spend"
    if any(k in t for k in ["ceo", "founder", "coo"]):
        return "Senior decision maker"
    if any(k in t for k in ["vp operations", "operations director"]):
        return "Ops owner — controls field travel"
    return "Senior contact"


def _send_weekly_digest(batch: WeeklyBatch, accounts: List[Account]):
    if not settings.digest_email_recipient:
        print("[Scheduler] No digest recipient configured — skipping digest email")
        return

    review_url = f"{settings.dashboard_url}/batch/{batch.token}"
    rows_text = "\n".join(
        f"  {i}. {acc.name} | {acc.industry} | Score: {int(acc.propensity_score)} | {acc.trigger_signal}"
        for i, acc in enumerate(accounts[:10], 1)
    )
    subject = f"[SDR Bot] {len(accounts)} accounts ready — week of {batch.week_start.strftime('%b %d')}"
    plain = f"""Hi {settings.your_name.split()[0]},

Your weekly prospect batch is ready. {len(accounts)} accounts scored, outreach drafted.

Top 10 this week:
{rows_text}
{"..." if len(accounts) > 10 else ""}

REVIEW + APPROVE:
{review_url}

For each account: propensity signal, ranked contacts, 3 email variants, 2 LinkedIn scripts.
Approve → email fires automatically. LinkedIn scripts ready to copy.

— SDR Bot"""

    try:
        emailer.send_html_email(
            to=settings.digest_email_recipient,
            subject=subject,
            html=_digest_html(batch, accounts, review_url),
            plain=plain,
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
            f'<td style="padding:10px 8px;color:#6b7280;">{acc.industry}</td>'
            f'<td style="padding:10px 8px;color:{color};font-weight:700;">{int(acc.propensity_score)}</td>'
            f'<td style="padding:10px 8px;font-size:13px;">{acc.trigger_signal or ""}</td>'
            f'</tr>'
        )
    overflow = f'<p style="color:#6b7280;font-size:13px;margin-top:12px;">+ {len(accounts)-20} more in dashboard</p>' if len(accounts) > 20 else ""
    return f"""<!DOCTYPE html><html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f9fafb;margin:0;padding:20px;">
<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1);">
  <div style="background:#0f172a;padding:28px 32px;">
    <h1 style="color:#fff;margin:0;font-size:20px;">SDR Bot — Weekly Prospects</h1>
    <p style="color:#94a3b8;margin:6px 0 0;font-size:14px;">Week of {batch.week_start.strftime('%B %d, %Y')} · {len(accounts)} accounts</p>
  </div>
  <div style="padding:28px 32px;">
    <a href="{review_url}" style="display:inline-block;background:#2563eb;color:#fff;padding:14px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:15px;margin-bottom:28px;">Review &amp; Approve Accounts →</a>
    <table style="width:100%;border-collapse:collapse;font-size:14px;">
      <thead><tr style="background:#f1f5f9;">
        <th style="padding:10px 8px;text-align:left;">Company</th>
        <th style="padding:10px 8px;text-align:left;">Industry</th>
        <th style="padding:10px 8px;text-align:left;">Score</th>
        <th style="padding:10px 8px;text-align:left;">Top Signal</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    {overflow}
  </div>
  <div style="padding:16px 32px;background:#f8fafc;border-top:1px solid #e2e8f0;">
    <p style="color:#94a3b8;font-size:12px;margin:0;">Generated by your SDR Bot. Approve accounts to trigger outreach.</p>
  </div>
</div></body></html>"""


# ── Daily Touch Sequence ──────────────────────────────────────────────────────

def run_daily_touch_sequence():
    print(f"[Scheduler] Daily touch sequence at {datetime.utcnow()}")
    now = datetime.utcnow()

    with Session(engine) as db:
        due_tasks = (
            db.query(TouchTask)
            .filter(TouchTask.status == "pending", TouchTask.scheduled_date <= now)
            .all()
        )
        print(f"[Scheduler] {len(due_tasks)} due touch tasks")

        for task in due_tasks:
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
                first_variant = db.query(OutreachVariant).filter_by(
                    account_id=acc.id, channel="email",
                    variant_index=acc.selected_email_variant or 0,
                ).first()
                orig_subject = first_variant.subject if first_variant else ""
                subject, body = ai.generate_followup_email(
                    account={"name": acc.name, "industry": acc.industry, "trigger_signal": acc.trigger_signal},
                    contact_name=primary.first_name or "",
                    touch_number=task.touch_number,
                    original_subject=orig_subject,
                )
                body = body.replace("[First Name]", primary.first_name or "there")

            try:
                result = emailer.send_email(to=primary.email, subject=subject, body=body)
                task.resend_email_id = result.get("id")
                task.status = "sent"
                task.sent_at = datetime.utcnow()
                print(f"[Scheduler] Touch {task.touch_number} sent to {primary.email} ({acc.name})")
            except Exception as e:
                print(f"[Scheduler] Send error for {acc.name}: {e}")

        db.commit()


# ── Register Jobs ─────────────────────────────────────────────────────────────

def start_scheduler():
    scheduler.add_job(
        run_weekly_discovery,
        trigger="cron", day_of_week="mon", hour=7, minute=0,
        id="weekly_discovery", replace_existing=True,
    )
    scheduler.add_job(
        run_daily_touch_sequence,
        trigger="cron", hour=8, minute=0,
        id="daily_touches", replace_existing=True,
    )
    scheduler.start()
    print("[Scheduler] Started — weekly discovery Mon 7 AM, daily touches 8 AM")
