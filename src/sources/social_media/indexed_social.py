"""Public social media posts (LinkedIn posts, X, Facebook, Instagram, Threads) found through
indexed public pages via the search provider.

Only content that is public and indexed by the search provider is used. No login,
no private groups/profiles, no CAPTCHA or anti-bot circumvention. Post URLs are kept
as the original source; the post date is recorded separately from any job date.
"""

from __future__ import annotations

from src.core.models import SourceType
from src.sources.search_engine.web_search import SearchBackedSource


class IndexedSocialMediaSource(SearchBackedSource):
    name = "Social Media (indexed public posts)"
    category = SourceType.SOCIAL_MEDIA
    description = "Recruiter / HR / company hiring posts found via site: queries"
