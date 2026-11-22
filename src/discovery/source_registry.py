"""Loads source connectors listed in ``discovery.connectors`` (``module:Class``)."""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from src.core.configuration import Settings
from src.core.exceptions import ConfigurationError
from src.core.models import SourceType
from src.sources.base import BaseJobSource

CATEGORY_FLAGS = {
    SourceType.SEARCH_ENGINE: "discovery.enable_search_engines",
    SourceType.JOB_PORTAL: "discovery.enable_job_portals",
    SourceType.COMPANY_CAREERS: "discovery.enable_company_careers",
    SourceType.ATS: "discovery.enable_ats",
    SourceType.SOCIAL_MEDIA: "discovery.enable_social_media",
    SourceType.COMMUNITY: "discovery.enable_communities",
    SourceType.OTHER: "discovery.enable_other_sources",
}


@dataclass
class RegisteredSource:
    source: BaseJobSource
    enabled: bool
    disabled_reason: str = ""


def load_connector(spec: str) -> type[BaseJobSource]:
    if ":" not in spec:
        raise ConfigurationError(f"Connector '{spec}' must be written as 'module.path:ClassName'")
    module_name, cls_name = spec.split(":", 1)
    if not module_name.startswith("src."):
        raise ConfigurationError(f"Connector '{spec}' must live inside the src package")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigurationError(f"Cannot import connector module '{module_name}': {exc}") from exc
    cls = getattr(module, cls_name, None)
    if cls is None or not isinstance(cls, type) or not issubclass(cls, BaseJobSource):
        raise ConfigurationError(f"'{spec}' is not a BaseJobSource subclass")
    return cls


class SourceRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.entries: list[RegisteredSource] = []
        for spec in settings.get("discovery.connectors", []) or []:
            cls = load_connector(str(spec))
            src = cls(settings)
            flag = CATEGORY_FLAGS.get(src.category)
            enabled = bool(settings.get(flag, True)) if flag else True
            self.entries.append(RegisteredSource(src, enabled, "" if enabled else f"disabled by {flag}"))

    def sources(self) -> list[BaseJobSource]:
        return [e.source for e in self.entries]

    def by_category(self) -> dict[SourceType, list[RegisteredSource]]:
        out: dict[SourceType, list[RegisteredSource]] = {}
        for e in self.entries:
            out.setdefault(e.source.category, []).append(e)
        return out
