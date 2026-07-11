"""
site_catalog.py — turns (site name, job_type, experience_level) into a real
search-results URL the bot can open directly.

This is what makes the backend the "brain" of navigation instead of the
bot: the bot never decides where to go — it asks (implicitly, by being
told) and the backend hands it a concrete URL with the right query params
already applied. Site-specific quirks (param names, experience-level
encoding) live here, in exactly one place, instead of being duplicated
inside the Electron bot's code.

Kept intentionally simple: one search URL per site per request. Sites that
need multi-step filter UIs (click a dropdown, then a checkbox) instead of
URL params are a known gap — see the "needs_ui_filters" flag below. For
those, the backend still opens the base search URL, and a later
apply_filters round-trip (bot reports the page loaded, backend sends
concrete click targets) is what the STEP 2 protocol in ws.py's
handle_open_site_ack is designed to support once that per-site filter
mapping is written.
"""

from urllib.parse import quote_plus

EXPERIENCE_QUERY = {
    # experience_level -> per-site query fragment. "any" means don't filter.
    "linkedin": {"fresher": "&f_E=1,2", "experienced": "&f_E=3,4,5,6", "any": ""},
    "indeed": {"fresher": "&explvl=entry_level", "experienced": "&explvl=mid_level", "any": ""},
    "naukri": {"fresher": "&experience=0", "experienced": "&experience=3", "any": ""},
}


def _site_key(site_name: str) -> str:
    return site_name.strip().lower().replace(" ", "")


SITE_BUILDERS = {
    "linkedin": lambda q, exp: f"https://www.linkedin.com/jobs/search/?keywords={q}{EXPERIENCE_QUERY['linkedin'][exp]}",
    "indeed": lambda q, exp: f"https://www.indeed.com/jobs?q={q}{EXPERIENCE_QUERY['indeed'][exp]}",
    "naukri": lambda q, exp: f"https://www.naukri.com/{q.replace('+', '-')}-jobs{EXPERIENCE_QUERY['naukri'][exp]}",
    # Sites below don't have a documented stable URL-param filter scheme —
    # the bot opens the plain search page and the backend will need a
    # per-site apply_filters click-map to go further (not yet implemented,
    # so experience_level is currently ignored for these three).
    "glassdoor": lambda q, exp: f"https://www.glassdoor.com/Job/jobs.htm?sc.keyword={q}",
    "monster": lambda q, exp: f"https://www.monster.com/jobs/search?q={q}",
    "ziprecruiter": lambda q, exp: f"https://www.ziprecruiter.com/candidate/search?search={q}",
    "wellfound": lambda q, exp: f"https://wellfound.com/jobs?query={q}",
    "dice": lambda q, exp: f"https://www.dice.com/jobs?q={q}",
    "simplyhired": lambda q, exp: f"https://www.simplyhired.com/search?q={q}",
}

# Sites where experience filtering needs real UI interaction (dropdown/
# checkbox clicks) rather than a URL param — the bot should expect a
# follow-up apply_filters command after the page loads for these.
NEEDS_UI_FILTERS = {"glassdoor", "monster", "ziprecruiter", "wellfound", "dice", "simplyhired"}


def build_search_url(site_name: str, job_type: str, experience_level: str) -> dict:
    """
    Returns {"url": ..., "needs_ui_filters": bool}. Unknown sites fall back
    to a Google site-search as a safe default rather than erroring out and
    stalling the whole job-search session over one bad/unrecognized site
    name (e.g. a typo the AI extracted from free-text chat).
    """
    key = _site_key(site_name)
    exp = experience_level if experience_level in ("fresher", "experienced") else "any"
    query = quote_plus(job_type or "jobs")

    builder = SITE_BUILDERS.get(key)
    if builder is None:
        return {
            "url": f"https://www.google.com/search?q={query}+jobs+site:{quote_plus(site_name)}",
            "needs_ui_filters": True,
        }

    return {"url": builder(query, exp), "needs_ui_filters": key in NEEDS_UI_FILTERS}
