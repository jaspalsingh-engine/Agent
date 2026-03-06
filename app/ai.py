"""
Claude API client.
- Batch-scores companies (10 at a time) for propensity
- Generates 3 email variants + 2 LinkedIn variants per account
- Classifies reply sentiment
"""
import json
from typing import List, Dict, Any, Tuple
import anthropic
from app.config import settings

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
MODEL = "claude-sonnet-4-6"

SCORING_SYSTEM = """You are a B2B sales intelligence analyst specializing in corporate travel and hotel spend.
Your job: score companies on their propensity to spend $250K+ annually on business hotel lodging in the United States.

HIGH propensity signals:
- Multiple office locations or geographically distributed workforce
- Industries with heavy field travel: Construction, Engineering, O&G, Consulting, Staffing, Financial Services field roles, Tech (field sales/SE teams)
- Hiring field-facing roles (Account Executives, Project Managers, Field Engineers, Consultants, Territory Managers)
- Recent expansion, new office, or relocation news
- Revenue/employee ratio suggesting well-funded travel-eligible workforce
- ZI Intent signals for travel, lodging, T&E topics

LOW propensity signals:
- Fully remote-first companies with no field roles
- Tiny single-office companies in non-travel industries
- Retail, restaurants, non-profits without field operations

Return ONLY valid JSON. No markdown, no explanation outside the JSON."""

OUTREACH_SYSTEM = """You are a sharp B2B SDR writing cold outreach for a free corporate hotel booking platform.

Platform facts (ALWAYS accurate):
- 100% free — no contracts, no fees, no commitments
- Access to negotiated hotel rates at 700,000+ properties worldwide
- Centralized visibility on team hotel spend
- The company earns 13% commission per booking — zero cost to the prospect

Tone guidelines:
- Variant A (Direct & Punchy): Short, confident, 3–4 sentences max. No fluff.
- Variant B (Story-Led): Open with a relatable pain point, then pivot to solution. Conversational.
- Variant C (Question Hook): Lead with a sharp question that creates curiosity. Build intrigue before the CTA.

LinkedIn variants:
- Variant A (Connection Note): Under 300 characters, punchy, no CTA link.
- Variant B (Direct DM): 2–3 short sentences, one clear ask.

Rules:
- Use [First Name] as the placeholder — never fill in a real name
- Include the Calendly link: {calendly}
- Every email must have a Subject line
- Never sound like a robot or use buzzwords like "synergies", "leverage", "circle back"
- Do NOT mention competitors
Return ONLY valid JSON.""".format(calendly=settings.your_calendly_link)


def score_companies_batch(companies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Score a batch of up to 10 companies.
    Returns list of {apollo_org_id, score, reasoning, trigger_signal} dicts.
    """
    prompt = f"""Score these {len(companies)} companies for hotel lodging spend propensity.

Companies:
{json.dumps(companies, indent=2)}

Return a JSON array (same order as input):
[
  {{
    "apollo_org_id": "<id>",
    "score": <0-100 integer>,
    "reasoning": "<2 sentences max explaining the score>",
    "trigger_signal": "<single most compelling signal, e.g. '12 US offices + construction industry'>"
  }}
]"""

    msg = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SCORING_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        return json.loads(msg.content[0].text)
    except Exception as e:
        print(f"[AI] scoring parse error: {e}\nRaw: {msg.content[0].text[:500]}")
        return []


def score_all_companies(companies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Score all companies in batches of 10. Returns merged results."""
    results = []
    for i in range(0, len(companies), 10):
        batch = companies[i:i + 10]
        slim = [
            {
                "apollo_org_id": c.get("id", c.get("apollo_org_id", "")),
                "name": c.get("name", ""),
                "industry": c.get("industry", ""),
                "employee_count": c.get("estimated_num_employees") or c.get("employee_count"),
                "annual_revenue": c.get("annual_revenue_printed") or c.get("annual_revenue", ""),
                "num_locations": len(c.get("locations", [])) or 1,
                "description": (c.get("short_description") or c.get("description") or "")[:300],
                "city": (c.get("primary_domain") and "") or c.get("city", ""),
                "state": c.get("state", ""),
            }
            for c in batch
        ]
        batch_results = score_companies_batch(slim)
        results.extend(batch_results)
    return results


def generate_outreach(
    account: Dict[str, Any],
    ranked_contacts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Generate 3 email variants + 2 LI variants for an account.
    Returns dict with 'emails' (list of 3) and 'linkedin' (list of 2).
    """
    contact_context = ""
    if ranked_contacts:
        primary = ranked_contacts[0]
        contact_context = (
            f"Primary contact: {primary.get('name', '[Name]')}, "
            f"{primary.get('title', '[Title]')} at {account.get('name', '')}"
        )

    prompt = f"""Generate outreach for this company:

Company: {account.get('name')}
Industry: {account.get('industry')}
Employees: {account.get('employee_count')}
Location: {account.get('city')}, {account.get('state')}
Why they were selected: {account.get('trigger_signal')}
{contact_context}
Sender: {settings.your_name}, {settings.your_title} at {settings.your_company}

Generate:
{{
  "emails": [
    {{
      "variant_index": 0,
      "style_label": "Direct & Punchy",
      "subject": "...",
      "body": "Hi [First Name],\\n\\n...\\n\\n{settings.your_name}\\n{settings.your_title} | {settings.your_company}"
    }},
    {{
      "variant_index": 1,
      "style_label": "Story-Led",
      "subject": "...",
      "body": "..."
    }},
    {{
      "variant_index": 2,
      "style_label": "Question Hook",
      "subject": "...",
      "body": "..."
    }}
  ],
  "linkedin": [
    {{
      "variant_index": 0,
      "style_label": "Connection Note",
      "body": "..."
    }},
    {{
      "variant_index": 1,
      "style_label": "Direct DM",
      "body": "..."
    }}
  ]
}}"""

    msg = client.messages.create(
        model=MODEL,
        max_tokens=2500,
        system=OUTREACH_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        return json.loads(msg.content[0].text)
    except Exception as e:
        print(f"[AI] outreach parse error for {account.get('name')}: {e}")
        return {"emails": [], "linkedin": []}


def generate_followup_email(
    account: Dict[str, Any],
    contact_name: str,
    touch_number: int,
    original_subject: str,
) -> Tuple[str, str]:
    """Generate follow-up or breakup email. Returns (subject, body)."""
    touch_labels = {
        3: "soft follow-up (reference the first email, add a new angle)",
        5: "breakup email (short, honest, leave door open)",
    }
    label = touch_labels.get(touch_number, "follow-up email")
    prompt = f"""Write a {label} for this account.

Company: {account.get('name')}, {account.get('industry')}
Contact first name: {contact_name or '[First Name]'}
Original subject: {original_subject}
Sender: {settings.your_name}, {settings.your_title} at {settings.your_company}
Calendly: {settings.your_calendly_link}
Platform: free corporate hotel booking, no contracts

Return JSON: {{"subject": "Re: {original_subject}", "body": "..."}}"""

    msg = client.messages.create(
        model=MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        data = json.loads(msg.content[0].text)
        return data["subject"], data["body"]
    except Exception:
        return f"Re: {original_subject}", "Following up on my last note — still worth a quick chat?"


def classify_reply(snippet: str, from_address: str) -> str:
    """
    Classify a reply email.
    Returns: hot | neutral | unsubscribe | out_of_office
    """
    prompt = f"""Classify this cold email reply:

From: {from_address}
Snippet: {snippet}

Categories:
- hot: interested, asking for more info, wants to meet, asking questions about the product
- out_of_office: automated OOO message
- unsubscribe: asking to be removed, not interested, stop emailing
- neutral: unclear, could be interested, needs follow-up

Return ONLY one word: hot, out_of_office, unsubscribe, or neutral"""

    msg = client.messages.create(
        model=MODEL,
        max_tokens=10,
        messages=[{"role": "user", "content": prompt}],
    )
    result = msg.content[0].text.strip().lower()
    if result not in ("hot", "out_of_office", "unsubscribe", "neutral"):
        return "neutral"
    return result
