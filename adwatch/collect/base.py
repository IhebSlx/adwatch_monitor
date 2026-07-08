"""Source-agnostic contracts. A future GoogleAdSource / LinkedInAdSource just
implements this same interface — nothing downstream (db, classifier, aggregator,
report) needs to change."""
from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class RawAd:
    external_ad_id: str | None = None
    ad_text: str | None = None
    cta: str | None = None
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    is_active: bool = True
    media_type: str | None = None
    reach: int | None = None
    real_spend: float | None = None
    country: str = "DE"
    source_raw: dict | None = None


@dataclass
class PageCandidate:
    page_id: str
    name: str
    category: str | None = None
    verified: bool | None = None
    likes: int | None = None
    ig_handle: str | None = None
    ad_library_url: str | None = None
    has_any_ads: bool | None = None
    extra: dict = field(default_factory=dict)


class AdSource(ABC):
    """Common interface for every ad platform adapter."""

    name: str = "base"

    @abstractmethod
    def resolve_company(self, name: str, country: str = "DE") -> list[PageCandidate]:
        """Return candidate pages for a company name (for Stage-A verification)."""

    @abstractmethod
    def fetch_ads(self, page_id: str, country: str = "DE", active_only: bool = True) -> list[RawAd]:
        """Return ads for a confirmed page id."""
