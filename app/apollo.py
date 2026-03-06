"""
Apollo.io API client.

Credit strategy:
- Company search: FREE (no credits consumed)
- People search (obfuscated emails): FREE
- Email reveal (full email): 1 credit per contact — only called on account approval
"""
import httpx
from typing import List, Dict, Any, Optional
from app.config import settings

BASE_URL = "https://api.apollo.io/v1"
HEADERS = {
    "Content-Type": "application/json",
    "Cache-Control": "no-cache",
    "X-Api-Key": settings.apollo_api_key,
}

# Title priority matrix used both here and by the AI layer
TITLE_PRIORITY = {
    "travel_manager":  ["travel manager", "corporate travel", "travel coordinator", "travel director"],
    "construction":    ["vp operations", "operations director", "project manager", "vp engineering", "controller", "cfo"],
    "tech":            ["vp finance", "head of finance", "finance director", "vp of finance",
                        "office manager", "workplace manager", "chief of staff"],
    "smb":             ["ceo", "founder", "co-founder", "coo", "cfo", "office manager"],
    "default":         ["cfo", "vp finance", "finance director", "hr director",
                        "people operations", "office manager", "chief financial officer"],
}

CONSTRUCTION_KEYWORDS = ["construction", "engineering", "oil", "gas", "energy", "contracting"]
TECH_KEYWORDS = ["software", "technology", "saas", "information technology"]


def _title_rank(title: str, industry: str) -> int:
    """Lower number = higher priority contact."""
    if not title:
        return 99
    t = title.lower()
    # Travel manager always wins regardless of industry
    for kw in TITLE_PRIORITY["travel_manager"]:
        if kw in t:
            return 0
    ind = (industry or "").lower()
    if any(k in ind for k in CONSTRUCTION_KEYWORDS):
        tier = TITLE_PRIORITY["construction"]
    elif any(k in ind for k in TECH_KEYWORDS):
        tier = TITLE_PRIORITY["tech"]
    else:
        tier = TITLE_PRIORITY["default"]
    for i, kw in enumerate(tier):
        if kw in t:
            return i + 1
    return 50


def search_companies(page: int = 1, per_page: int = 100) -> List[Dict[str, Any]]:
    """
    Search Apollo for companies matching targeting criteria.
    No credits consumed — returns basic org data only.
    """
    payload = {
        "page": page,
        "per_page": per_page,
        "organization_industry_tag_ids": [],   # Apollo uses tag IDs; we filter by industry name client-side
        "organization_locations": ["United States"],
        "organization_num_employees_ranges": [f"{settings.min_employees},100000"],
    }
    try:
        resp = httpx.post(
            f"{BASE_URL}/mixed_companies/search",
            headers=HEADERS,
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        orgs = data.get("organizations", [])
        # Filter to target industries client-side
        target = [i.lower() for i in settings.industry_list]
        filtered = []
        for org in orgs:
            ind = (org.get("industry") or "").lower()
            if any(t in ind for t in target):
                filtered.append(org)
        return filtered
    except Exception as e:
        print(f"[Apollo] company search error: {e}")
        return []


def search_people_for_company(
    org_domain: str,
    org_name: str,
    industry: str,
) -> List[Dict[str, Any]]:
    """
    Search for people at a company. Emails are obfuscated — no credits consumed.
    Returns ranked list of contacts (max 5).
    """
    payload = {
        "page": 1,
        "per_page": 10,
        "organization_domains": [org_domain] if org_domain else [],
        "q_organization_name": org_name if not org_domain else None,
        "contact_email_status": ["verified", "guessed", "unavailable"],
        "person_seniorities": ["owner", "founder", "c_suite", "vp", "director", "manager"],
    }
    try:
        resp = httpx.post(
            f"{BASE_URL}/mixed_people/search",
            headers=HEADERS,
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        people = resp.json().get("people", [])
        # Rank by title priority
        ranked = sorted(people, key=lambda p: _title_rank(p.get("title", ""), industry))
        return ranked[:5]
    except Exception as e:
        print(f"[Apollo] people search error for {org_name}: {e}")
        return []


def reveal_contact_email(apollo_person_id: str) -> Optional[str]:
    """
    Reveal full email for a contact. Costs 1 Apollo credit.
    Only called when user approves an account.
    """
    payload = {
        "id": apollo_person_id,
        "reveal_personal_emails": False,   # work email only
    }
    try:
        resp = httpx.post(
            f"{BASE_URL}/people/match",
            headers=HEADERS,
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        person = resp.json().get("person", {})
        email = person.get("email")
        # Fall back to email from contact list
        if not email:
            for e in person.get("email_addresses", []):
                if e.get("email"):
                    email = e["email"]
                    break
        return email
    except Exception as e:
        print(f"[Apollo] reveal email error for {apollo_person_id}: {e}")
        return None


def enrich_org(domain: str) -> Optional[Dict[str, Any]]:
    """Enrich a single org by domain. Free."""
    try:
        resp = httpx.get(
            f"{BASE_URL}/organizations/enrich",
            headers=HEADERS,
            params={"domain": domain},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("organization")
    except Exception:
        return None
