"""IEEE Xplore reference scraper using Playwright.

Extracts all cited DOIs / metadata from a survey paper's "References"
section on IEEE Xplore.  Also provides a Semantic Scholar API fallback
for when scraping is blocked.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import httpx
from playwright.async_api import Browser, Page, async_playwright

from .models import Article

DOI_REGEX = re.compile(r"10\.\d{4,9}/[^\s<>\"{}|\\^`]+", re.IGNORECASE)

# ── Semantic Scholar (free, no key) ──────────────────────────


async def fetch_references_semantic_scholar(doi: str) -> list[Article]:
    """Use the Semantic Scholar API to get references for *doi*."""
    fields = "externalIds,title,authors,year,venue,abstract"
    url = (
        f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
        f"/references?fields={fields}&limit=1000"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    articles: list[Article] = []
    for entry in data.get("data", []):
        paper = entry.get("citedPaper", {})
        if not paper:
            continue
        ext = paper.get("externalIds") or {}
        ref_doi = ext.get("DOI", "")
        if not ref_doi:
            continue

        authors = [
            a.get("name", "") for a in (paper.get("authors") or []) if a.get("name")
        ]
        articles.append(
            Article(
                doi=ref_doi,
                title=paper.get("title") or "",
                authors=authors,
                year=paper.get("year"),
                venue=paper.get("venue") or "",
                abstract=paper.get("abstract") or "",
                accessed_date=date.today(),
            )
        )
    return articles


# ── IEEE Xplore scraper (Playwright) ────────────────────────


async def _launch_browser() -> tuple[Browser, Page]:
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=False,
        channel="chrome",
    )
    ctx = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="America/New_York",
    )
    page = await ctx.new_page()
    return browser, page


async def fetch_references_ieee(url: str) -> list[Article]:
    """Scrape the References tab of an IEEE Xplore paper.

    Opens headed Chrome, clicks "References", extracts every reference
    entry with its DOI, title, and authors when available.
    """
    browser, page = await _launch_browser()
    articles: list[Article] = []

    try:
        await page.goto(url, wait_until="networkidle", timeout=60_000)

        # Click the "References" tab / section header if present
        for selector in [
            "a:has-text('References')",
            "button:has-text('References')",
            "#references",
        ]:
            try:
                loc = page.locator(selector).first
                await loc.wait_for(state="visible", timeout=5_000)
                await loc.click()
                await page.wait_for_timeout(2_000)
                break
            except Exception:
                continue

        # Wait for reference items to render
        await page.wait_for_timeout(3_000)

        # Gather reference items — IEEE uses <div class="reference-container"> or similar
        ref_selectors = [
            ".reference-container",
            "[class*='reference'] li",
            ".refs-container .reference",
            "#ref-list li",
        ]

        ref_elements = []
        for sel in ref_selectors:
            ref_elements = await page.locator(sel).all()
            if ref_elements:
                break

        if not ref_elements:
            # Fallback: just scan the whole page for DOIs
            body = await page.inner_text("body")
            for m in DOI_REGEX.finditer(body):
                doi = m.group(0).rstrip(".,;)")
                if not any(a.doi.lower() == doi.lower() for a in articles):
                    articles.append(
                        Article(doi=doi, accessed_date=date.today())
                    )
            return articles

        for el in ref_elements:
            text = await el.inner_text()

            # Try to extract DOI from the element
            doi_match = DOI_REGEX.search(text)
            if not doi_match:
                # Check for a doi.org link inside the element
                try:
                    href = await el.locator('a[href*="doi.org"]').first.get_attribute("href")
                    if href:
                        doi_match = DOI_REGEX.search(href)
                except Exception:
                    pass

            if not doi_match:
                continue

            doi = doi_match.group(0).rstrip(".,;)")

            # Skip duplicates
            if any(a.doi.lower() == doi.lower() for a in articles):
                continue

            # Try to get the title — usually the first bold / link text
            title = ""
            try:
                title_el = el.locator("a").first
                title = (await title_el.inner_text()).strip().strip('"').strip(""").strip(""")
            except Exception:
                pass

            articles.append(
                Article(
                    doi=doi,
                    title=title,
                    accessed_date=date.today(),
                )
            )

    finally:
        await browser.close()

    return articles


# ── Public API ───────────────────────────────────────────────


async def fetch_references(
    source: str,
    *,
    prefer_api: bool = True,
    progress_callback: Any = None,
) -> list[Article]:
    """Fetch references for a survey given its URL or DOI.

    Parameters
    ----------
    source:
        An IEEE Xplore URL or a raw DOI string.
    prefer_api:
        When True, try Semantic Scholar first (faster / more reliable).
        Falls back to IEEE scraping either way.
    progress_callback:
        Optional async callable(message: str) for progress updates.
    """
    doi: str | None = None
    doi_match = DOI_REGEX.search(source)
    if doi_match:
        doi = doi_match.group(0).rstrip(".,;)")

    if prefer_api and doi:
        if progress_callback:
            await progress_callback("Querying Semantic Scholar API…")
        try:
            refs = await fetch_references_semantic_scholar(doi)
            if refs:
                if progress_callback:
                    await progress_callback(f"Found {len(refs)} references via API")
                return refs
        except Exception:
            if progress_callback:
                await progress_callback("API failed, falling back to scraper…")

    # Scrape IEEE directly
    url = source if source.startswith("http") else f"https://doi.org/{source}"
    if progress_callback:
        await progress_callback(f"Scraping {url}…")
    refs = await fetch_references_ieee(url)
    if progress_callback:
        await progress_callback(f"Found {len(refs)} references via scraping")
    return refs
