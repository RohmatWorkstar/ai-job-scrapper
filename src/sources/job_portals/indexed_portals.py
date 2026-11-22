"""Job portals (LinkedIn Jobs, JobStreet, Indeed, Glints, Kalibrr, Glassdoor, Wellfound, ...)
discovered through indexed public pages via the configured search provider.

The portals are not scraped directly: most prohibit automated access in their
terms and/or robots.txt. Portal pages that permit it may be fetched later for
verification (robots.txt is enforced; login-walled domains are never fetched).
"""

from __future__ import annotations

from src.core.models import SourceType
from src.sources.search_engine.web_search import SearchBackedSource


class IndexedJobPortalSource(SearchBackedSource):
    name = "Job Portals (indexed)"
    category = SourceType.JOB_PORTAL
    description = "site: queries against job portals through the search provider"
