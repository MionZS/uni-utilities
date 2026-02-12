"""Pydantic models for the bibliography manager.

Strict schema for the JSON file that lives inside the Obsidian vault.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


class Article(BaseModel):
    """A single referenced paper."""

    doi: str
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    year: Optional[int] = None
    venue: str = ""
    abstract: str = ""
    crossref_url: str = ""
    google_scholar_url: str = ""
    ieee_url: str = ""
    pdf_url: str = ""
    local_path: str = ""
    accessed_date: Optional[date] = None
    relevance_score: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: str = ""
    manually_edited: bool = False


class Survey(BaseModel):
    """A survey paper whose references we track."""

    id: str = Field(description="Unique id — typically the DOI or a slug")
    name: str = ""
    source: str = Field(description="IEEE Xplore URL or DOI")
    date_added: date = Field(default_factory=date.today)
    total_references_expected: int = 0
    relevance_score: float = Field(default=0.0, ge=0.0, le=1.0)
    articles: list[Article] = Field(default_factory=list)

    # ── helpers ────────────────────────────────────────────────
    @property
    def fetched_count(self) -> int:
        return len(self.articles)

    @property
    def completeness(self) -> float:
        if self.total_references_expected <= 0:
            return 0.0
        return min(self.fetched_count / self.total_references_expected, 1.0)

    def has_doi(self, doi: str) -> bool:
        return any(a.doi.lower() == doi.lower() for a in self.articles)


class Project(BaseModel):
    """Top-level metadata about the user's own article / thesis."""

    title: str = ""
    author: str = ""
    year: Optional[int] = None
    abstract: str = ""
    keywords: list[str] = Field(default_factory=list)
    my_article_doi: str = ""


class Bibliography(BaseModel):
    """Root object persisted as JSON."""

    project: Project = Field(default_factory=Project)
    surveys: list[Survey] = Field(default_factory=list)

    # ── helpers ────────────────────────────────────────────────
    @property
    def unique_articles(self) -> dict[str, Article]:
        """De-duplicated map of DOI → first-seen Article across all surveys."""
        seen: dict[str, Article] = {}
        for survey in self.surveys:
            for art in survey.articles:
                key = art.doi.lower()
                if key not in seen:
                    seen[key] = art
        return seen

    @property
    def total_unique_articles(self) -> int:
        return len(self.unique_articles)

    def find_survey(self, survey_id: str) -> Survey | None:
        for s in self.surveys:
            if s.id == survey_id:
                return s
        return None
