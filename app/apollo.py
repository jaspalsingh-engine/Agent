"""
Apollo.io API client.

Free tier reality:
- /v1/organizations/search  → full company data ✓
- People API                → returns null on free tier ✗

Strategy:
- Discover + score companies via org search
- Surface Apollo web link + LinkedIn search URL per company
- User manually identifies contact and enters email in dashboard
"""
import httpx
from typing import List, Dict, Any
from app.config import settings

BASE_URL = "https://api.apollo.io/v1"
HEADERS = {
    "Content-Type": "application/json",
    "X-Api-Key": settings.apollo_api_key,
}

# Industry keyword groups — each maps to one Apollo search call
INDUSTRY_KEYWORD_GROUPS = [
    ["construction", "general contractor", "civil engineering"],
    ["consulting", "management consulting", "professional services"],
    ["staffing", "recruiting", "workforce solutions"],
    ["oil and gas", "energy", "oilfield services"],
    ["financial services", "wealth management", "investment banking"],
    ["information technology", "managed services", "it services"],
    ["computer software", "saas", "enterprise software"],
]


def search_companies(page: int = 1, per_page: int = 25) -> List[Dict[str, Any]]:
    """
    Search Apollo for companies matching travel-heavy industries.
    Rotates through industry keyword groups across pages.
    Returns deduplicated list with full firmographic data.
    """
    # Rotate keyword group based on page number
    group = INDUSTRY_KEYWORD_GROUPS[(page - 1) % len(INDUSTRY_KEYWORD_GROUPS)]
    results = []

    try:
        resp = httpx.post(
            f"{BASE_URL}/organizations/search",
            headers=HEADERS,
            json={
                "page": ((page - 1) // len(INDUSTRY_KEYWORD_GROUPS)) + 1,
                "per_page": per_page,
                "organization_locations": ["United States"],
                "organization_num_employees_ranges": [f"{settings.min_employees},50000"],
                "q_organization_keyword_tags": group,
            },
            timeout=30,
        )
        resp.raise_for_status()
        orgs = resp.json().get("organizations", [])
        for o in orgs:
            results.append(_normalize_org(o))
        return results
    except Exception as e:
        print(f"[Apollo] org search error (group={group}): {e}")
        return []


def _normalize_org(o: Dict) -> Dict:
    """Flatten Apollo org response into a consistent shape."""
    return {
        "id": o.get("id", ""),
        "name": o.get("name", ""),
        "primary_domain": o.get("primary_domain", ""),
        "industry": o.get("industry", ""),
        "estimated_num_employees": o.get("estimated_num_employees"),
        "annual_revenue_printed": o.get("annual_revenue_printed", ""),
        "city": o.get("city", ""),
        "state": o.get("state", ""),
        "country": o.get("country", ""),
        "linkedin_url": o.get("linkedin_url", ""),
        "short_description": o.get("short_description", ""),
        "keywords": o.get("keywords", []),
        "sic_codes": o.get("sic_codes", []),
        "naics_codes": o.get("naics_codes", []),
        "locations": o.get("locations", []),
    }


def apollo_contact_search_url(org_name: str, org_domain: str) -> str:
    """
    Direct link to Apollo web app to find contacts at this company.
    User clicks this, logs into Apollo, sees contacts to choose from.
    """
    from urllib.parse import quote
    if org_domain:
        return f"https://app.apollo.io/#/people?organization_domains[]={quote(org_domain)}&person_seniorities[]=c_suite&person_seniorities[]=vp&person_seniorities[]=director&person_seniorities[]=manager"
    return f"https://app.apollo.io/#/people?q_organization_name={quote(org_name)}&person_seniorities[]=c_suite&person_seniorities[]=vp"


def linkedin_contact_search_url(org_name: str, title_hint: str = "") -> str:
    """LinkedIn people search URL for finding the right contact."""
    from urllib.parse import quote
    query = f"{title_hint} {org_name}".strip() if title_hint else org_name
    return f"https://www.linkedin.com/search/results/people/?keywords={quote(query)}&origin=GLOBAL_SEARCH_HEADER"
