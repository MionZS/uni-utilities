"""IEEE Xplore reference scraper using Playwright.

Three-phase extraction:
  Phase 1 — "skeleton":  Collect every reference entry from the
            References tab (title, authors text, outbound links).
  Phase 2 — "muscle":    Visit each reference's link (Crossref,
            Google Scholar, or IEEE "View Article") to resolve
            the actual DOI.
  Phase 3 — "enrich":    Call Crossref API for each DOI to get
            clean title, authors, year, venue, abstract, and
            PDF URL.  Download PDFs when available.

Also provides a Semantic Scholar API path that skips the browser
entirely when possible.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Coroutine
from urllib.parse import urlparse

import httpx
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .models import Article

# ── Constants ────────────────────────────────────────────────

DOI_REGEX = re.compile(r"10\.\d{4,9}/[^\s<>\"{}|\\^`]+", re.IGNORECASE)
_FRAC_RE = re.compile(r"(\d+)/(\d+)")
_UNSAFE_PATH_CHARS = re.compile(r"[\\/:*?\"<>|\s]+")

# IEEE robots.txt specifies Crawl-delay: 10.  We fully honour it.
_IEEE_CRAWL_DELAY: float = 10.0

# Directory for debug artefacts (HTML dumps)
_DEBUG_DIR = Path("datalake/debug")

# PDF download directory (relative; resolved at runtime)
_DEFAULT_PDF_DIR = Path("bibliography/pdfs")

# HTTP User-Agent for polite crawling
_USER_AGENT = "BibManager/1.0 (mailto:student@example.com)"

# Link-text constants (avoids S1192 duplication warnings)
_LT_CROSSREF = "crossref"
_LT_VIEW_ARTICLE = "view article"
_LT_SHOW_ARTICLE = "show article"
_LT_GOOGLE_SCHOLAR = "google scholar"

_SKIP_LINK_TEXTS: frozenset[str] = frozenset({
    _LT_CROSSREF, "cross ref", _LT_VIEW_ARTICLE, _LT_SHOW_ARTICLE,
    _LT_GOOGLE_SCHOLAR, "view in scopus", "scopus", "pdf",
    "download", "doi", "web of science", "pubmed",
})

# Titles that are artefacts, not real paper titles
_JUNK_TITLES: frozenset[str] = frozenset({
    _LT_CROSSREF, _LT_VIEW_ARTICLE, _LT_GOOGLE_SCHOLAR, "",
})

ProgressCB = Callable[[str], Coroutine[Any, Any, None]] | None

# Hosts that the scraper is allowed to visit (SSRF-prevention allowlist).
_ALLOWED_HOSTS: frozenset[str] = frozenset({
    "ieeexplore.ieee.org",
    "doi.org",
    "dx.doi.org",
    "www.doi.org",
    "crossref.org",
    "www.crossref.org",
    "scholar.google.com",
    "api.crossref.org",
    "api.semanticscholar.org",
    "api.unpaywall.org",
})

_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


def _validate_source_url(url: str) -> str:
    """Validate *url* against an allowlist of schemes and hosts.

    Raises ``ValueError`` for anything that smells like SSRF
    (file://, javascript:, internal IPs, unknown hosts, etc.).
    """
    parsed = urlparse(url)

    # 1. Scheme check
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"Blocked scheme '{parsed.scheme}' — only http/https allowed"
        )

    # 2. Host check
    hostname = (parsed.hostname or "").lower()
    if not any(hostname == h or hostname.endswith(f".{h}") for h in _ALLOWED_HOSTS):
        raise ValueError(
            f"Host '{hostname}' is not in the allow-list. "
            "Add it to _ALLOWED_HOSTS if this is a legitimate academic source."
        )

    return url


# ── Skeleton: raw data scraped from the reference list ───────


@dataclass
class _RefSkeleton:
    """Intermediate representation before DOI is resolved."""

    index: int
    title: str = ""
    authors_text: str = ""
    year: int | None = None
    crossref_url: str = ""
    google_scholar_url: str = ""
    ieee_url: str = ""
    doi: str = ""


# ── Utilities ────────────────────────────────────────────────


def _clean_doi(raw: str) -> str:
    """Strip trailing punctuation that leaks into DOI matches."""
    return raw.rstrip(".,;)]\u201d\u201c\"'")


def _strip_html_tags(text: str) -> str:
    """Remove JATS / HTML tags from Crossref abstracts."""
    clean = re.sub(r"<[^>]+>", "", text)
    return html_mod.unescape(clean).strip()


def _safe_filename(doi: str) -> str:
    """Sanitise a DOI for use as a filename.

    Uses a *whitelist* approach: only alphanumeric, dot, hyphen, and
    underscore survive.  Directory-traversal sequences (``..``) and any
    path separators are eliminated *before* the whitelist pass so they
    can never leak through.
    """
    # 1. Remove directory traversal sequences first
    sanitized = doi.replace("..", "").replace("./", "").replace(".\\", "")
    # 2. Whitelist: keep only safe characters
    sanitized = re.sub(r"[^a-zA-Z0-9._-]", "_", sanitized)
    # 3. Collapse runs of underscores
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    # 4. Fallback for empty result
    if not sanitized:
        sanitized = "unknown_doi"
    # 5. Truncate to a reasonable filesystem limit
    return sanitized[:200]


def _extract_doi_from_text(text: str) -> str | None:
    """Return the first DOI found in *text*, or None."""
    m = DOI_REGEX.search(text)
    return _clean_doi(m.group(0)) if m else None


# ── Semantic Scholar (free, no key) ──────────────────────────


async def fetch_references_semantic_scholar(
    doi: str,
    progress: ProgressCB = None,
) -> list[Article]:
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
    entries = data.get("data", [])
    for i, entry in enumerate(entries, 1):
        art = _semantic_scholar_entry_to_article(entry)
        if art:
            articles.append(art)
        if progress and i % 10 == 0:
            await progress(f"  API: processed {i}/{len(entries)} entries...")

    return articles


def _semantic_scholar_entry_to_article(entry: dict[str, Any]) -> Article | None:
    """Convert one Semantic Scholar reference entry into an Article."""
    paper = entry.get("citedPaper", {})
    if not paper:
        return None
    ext = paper.get("externalIds") or {}
    ref_doi = ext.get("DOI", "")
    if not ref_doi:
        return None
    authors = [
        a.get("name", "") for a in (paper.get("authors") or []) if a.get("name")
    ]
    return Article(
        doi=ref_doi,
        title=paper.get("title") or "",
        authors=authors,
        year=paper.get("year"),
        venue=paper.get("venue") or "",
        abstract=paper.get("abstract") or "",
        accessed_date=date.today(),
    )


# ── Browser helpers ──────────────────────────────────────────


async def _launch_browser() -> tuple[Any, Browser, BrowserContext, Page]:
    """Start Playwright **and** return its handle so callers can stop it."""
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False, channel="chrome")
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
    return pw, browser, ctx, page


# ── Phase 1 helpers ──────────────────────────────────────────


async def _click_references_tab(page: Page) -> None:
    """Click the 'References' tab if present on the IEEE page."""
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
            return
        except Exception:
            continue


async def _find_ref_elements(page: Page) -> list[Any]:
    """Locate reference containers on the page."""
    for sel in [
        ".reference-container",
        "[class*='reference'] li",
        ".refs-container .reference",
        "#ref-list li",
    ]:
        elements = await page.locator(sel).all()
        if elements:
            return elements
    return []


async def _save_debug_html(page: Page, url: str, progress: ProgressCB) -> None:
    """Save the page HTML for post-mortem debugging."""
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        page_html = await page.content()
        slug = re.sub(r"[^\w]+", "_", url.split("//")[-1])[:80]
        out = _DEBUG_DIR / f"{slug}.html"
        out.write_text(page_html, encoding="utf-8")
        if progress:
            await progress(f"  Debug HTML saved \u2192 {out}")
    except Exception as exc:
        if progress:
            await progress(f"  (debug HTML save failed: {exc})")


async def _extract_title_from_ref(el: Any, text: str) -> str:
    """Extract the paper title from a reference element."""
    # Prefer quoted text (IEEE format: Author(s), "Title," venue, year.)
    m = re.search(r'["\u201c](.+?)["\u201d]', text)
    if m:
        return m.group(1).strip().rstrip(",.")

    # Fall back to the first <a> that isn't a known button
    for a_el in await el.locator("a").all():
        try:
            a_text = (await a_el.inner_text()).strip()
        except Exception:
            continue
        if not a_text or a_text.lower() in _SKIP_LINK_TEXTS or len(a_text) < 5:
            continue
        return a_text.strip('"\u201c\u201d,.')

    return ""


def _extract_authors_text(text: str, title: str) -> str:
    """Extract authors text (everything before the title in the ref)."""
    if not title or title not in text:
        return ""
    before = text[: text.index(title)].strip().rstrip(",")
    return re.sub(r"^\[?\d+\]?\s*\.?\s*", "", before)


async def _classify_links(el: Any) -> dict[str, str]:
    """Classify outbound links in a reference element.

    Returns dict with keys: crossref_url, google_scholar_url, ieee_url, doi.
    """
    result: dict[str, str] = {
        "crossref_url": "", "google_scholar_url": "", "ieee_url": "", "doi": "",
    }
    for link in await el.locator("a").all():
        try:
            href = (await link.get_attribute("href")) or ""
            link_text = (await link.inner_text()).strip().lower()
        except Exception:
            continue
        _classify_single_link(link_text, href, result)
    return result


def _classify_single_link(
    link_text: str, href: str, out: dict[str, str],
) -> None:
    """Classify a single link and update *out* in-place."""
    if _LT_CROSSREF in link_text or "doi.org" in href:
        out["crossref_url"] = href
        doi = _extract_doi_from_text(href)
        if doi:
            out["doi"] = doi
    elif _LT_GOOGLE_SCHOLAR in link_text or "scholar.google" in href:
        out["google_scholar_url"] = href
    elif (
        _LT_VIEW_ARTICLE in link_text
        or _LT_SHOW_ARTICLE in link_text
        or "ieeexplore.ieee.org" in href
    ):
        out["ieee_url"] = href


async def _parse_single_ref(idx: int, el: Any) -> _RefSkeleton:
    """Parse one reference element into a skeleton."""
    try:
        text = await el.inner_text()
    except Exception:
        text = ""

    title = await _extract_title_from_ref(el, text)
    authors = _extract_authors_text(text, title)

    year_m = re.search(r"\b(19|20)\d{2}\b", text)
    links = await _classify_links(el)

    sk = _RefSkeleton(
        index=idx,
        title=title,
        authors_text=authors,
        year=int(year_m.group(0)) if year_m else None,
        crossref_url=links["crossref_url"],
        google_scholar_url=links["google_scholar_url"],
        ieee_url=links["ieee_url"],
        doi=links["doi"],
    )

    # Check the raw text for an inline DOI
    if not sk.doi:
        sk.doi = _extract_doi_from_text(text) or ""

    return sk


async def _collect_skeletons(
    page: Page,
    url: str,
    progress: ProgressCB = None,
) -> list[_RefSkeleton]:
    """Phase 1: navigate to the survey page and scrape every reference entry."""
    await page.goto(url, wait_until="networkidle", timeout=60_000)
    await _click_references_tab(page)
    await page.wait_for_timeout(3_000)

    ref_elements = await _find_ref_elements(page)
    if progress:
        await progress(f"Phase 1: found {len(ref_elements)} reference entries")

    await _save_debug_html(page, url, progress)

    skeletons: list[_RefSkeleton] = []
    for idx, el in enumerate(ref_elements, start=1):
        skeletons.append(await _parse_single_ref(idx, el))
        if progress and idx % 5 == 0:
            await progress(f"  Skeleton: {idx}/{len(ref_elements)}")

    if progress:
        already = sum(1 for s in skeletons if s.doi)
        await progress(
            f"Phase 1 done: {len(skeletons)} refs collected, "
            f"{already} DOIs found inline"
        )
    return skeletons


# ── Phase 2: resolve DOI for each skeleton ───────────────────


async def _resolve_doi_from_crossref_page(page: Page, url: str) -> str | None:
    """Extract the DOI from a Crossref / doi.org URL or its landing page."""
    doi = _extract_doi_from_text(url)
    if doi:
        return doi
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
        return (
            _extract_doi_from_text(page.url)
            or _extract_doi_from_text(await page.inner_text("body"))
        )
    except Exception:
        return None


async def _try_doi_from_meta_tags(page: Page) -> str | None:
    """Check common meta tags for a DOI."""
    for sel in ['meta[name="citation_doi"]', 'meta[name="dc.identifier"]']:
        try:
            val = await page.locator(sel).first.get_attribute("content")
            if val:
                doi = _extract_doi_from_text(val)
                if doi:
                    return doi
        except Exception:
            continue
    return None


async def _try_doi_from_doi_links(page: Page) -> str | None:
    """Check for doi.org anchor links on the page."""
    try:
        href = await page.locator('a[href*="doi.org"]').first.get_attribute("href")
        if href:
            return _extract_doi_from_text(href)
    except Exception:
        pass
    return None


async def _resolve_doi_from_ieee_page(page: Page, url: str) -> str | None:
    """Visit an IEEE 'View Article' page and extract the DOI."""
    try:
        full = url if url.startswith("http") else f"https://ieeexplore.ieee.org{url}"
        await page.goto(full, wait_until="networkidle", timeout=30_000)
        body = await page.inner_text("body")
        m = re.search(r"DOI:\s*(\S+)", body, re.IGNORECASE)
        if m:
            doi = _extract_doi_from_text(m.group(1))
            if doi:
                return doi
        return await _try_doi_from_meta_tags(page) or await _try_doi_from_doi_links(page)
    except Exception:
        return None


async def _resolve_doi_from_google_scholar(page: Page, url: str) -> str | None:
    """Visit a Google Scholar link and try to find a DOI on the page."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
        return _extract_doi_from_text(await page.inner_text("body"))
    except Exception:
        return None


async def _resolve_single_doi(page: Page, sk: _RefSkeleton) -> str | None:
    """Try all link sources for a single skeleton, return DOI or None."""
    if sk.crossref_url:
        doi = await _resolve_doi_from_crossref_page(page, sk.crossref_url)
        if doi:
            return doi
    if sk.ieee_url:
        doi = await _resolve_doi_from_ieee_page(page, sk.ieee_url)
        if doi:
            return doi
    if sk.google_scholar_url:
        return await _resolve_doi_from_google_scholar(page, sk.google_scholar_url)
    return None


async def _resolve_skeletons(
    ctx: BrowserContext,
    skeletons: list[_RefSkeleton],
    progress: ProgressCB = None,
) -> None:
    """Phase 2: resolve DOIs in-place for skeletons that lack one."""
    need_resolve = [s for s in skeletons if not s.doi]
    if not need_resolve:
        if progress:
            await progress("Phase 2: all DOIs already resolved - nothing to do")
        return

    if progress:
        await progress(
            f"Phase 2: resolving DOIs for {len(need_resolve)}/{len(skeletons)} refs..."
        )

    resolve_page = await ctx.new_page()
    resolved = 0

    for i, sk in enumerate(need_resolve, start=1):
        doi = await _resolve_single_doi(resolve_page, sk)
        if doi:
            sk.doi = doi
            resolved += 1

        if progress:
            status = f"\u2713 {doi}" if doi else "\u2717 no DOI"
            title_short = sk.title[:50] or "(untitled)"
            await progress(f"  [{i}/{len(need_resolve)}] {title_short} \u2192 {status}")

        await resolve_page.wait_for_timeout(int(_IEEE_CRAWL_DELAY * 1_000))

    await resolve_page.close()

    if progress:
        total_with_doi = sum(1 for s in skeletons if s.doi)
        await progress(
            f"Phase 2 done: resolved {resolved} new DOIs "
            f"({total_with_doi}/{len(skeletons)} total)"
        )


# ── Convert skeletons -> Articles ────────────────────────────


def _parse_authors(raw_text: str) -> list[str]:
    """Parse author names from rough reference text."""
    raw = re.sub(r"^\[?\d+\]?\s*\.?\s*", "", raw_text)
    parts = re.split(r",\s*|\s+and\s+", raw)
    authors = [p.strip() for p in parts if p.strip() and len(p.strip()) > 1]
    return [
        a for a in authors
        if not re.match(r"^(pp?\.|vol\.|no\.|in\s|proc|ieee|acm)", a, re.I)
        and not re.match(r"^\d+[\-\u2013]\d+$", a)
    ]


def _skeletons_to_articles(skeletons: list[_RefSkeleton]) -> list[Article]:
    """Convert resolved skeletons into Article models, preserving links."""
    articles: list[Article] = []
    seen: set[str] = set()

    for sk in skeletons:
        key = sk.doi.lower() if sk.doi else f"__no_doi_{sk.index}"
        if key in seen:
            continue
        seen.add(key)

        articles.append(Article(
            doi=sk.doi or f"UNRESOLVED-{sk.index}",
            title=sk.title,
            authors=_parse_authors(sk.authors_text) if sk.authors_text else [],
            year=sk.year,
            crossref_url=sk.crossref_url,
            google_scholar_url=sk.google_scholar_url,
            ieee_url=sk.ieee_url,
            accessed_date=date.today(),
            notes="" if sk.doi else "DOI could not be resolved automatically",
        ))

    return articles


# ── Phase 3: enrich metadata via Crossref API ───────────────


def _enrich_article_from_msg(art: Article, msg: dict[str, Any]) -> bool:
    """Apply Crossref API *msg* fields to *art*.  Returns True if changed."""
    changed = False
    is_junk = art.title.lower() in _JUNK_TITLES

    titles = msg.get("title", [])
    if titles and (not art.title or is_junk):
        art.title = titles[0]
        changed = True

    cr_authors = msg.get("author", [])
    if cr_authors and (not art.authors or is_junk):
        art.authors = [
            f"{a.get('given', '')} {a.get('family', '')}".strip()
            for a in cr_authors if a.get("family")
        ]
        changed = True

    changed = _apply_year(art, msg) or changed
    changed = _apply_venue(art, msg) or changed
    changed = _apply_abstract(art, msg) or changed
    changed = _apply_pdf_url(art, msg) or changed
    return changed


def _apply_year(art: Article, msg: dict[str, Any]) -> bool:
    if art.year:
        return False
    pub = msg.get("published-print") or msg.get("published-online") or msg.get("created")
    if not pub:
        return False
    parts = pub.get("date-parts", [[]])[0]
    if parts:
        art.year = parts[0]
        return True
    return False


def _apply_venue(art: Article, msg: dict[str, Any]) -> bool:
    containers = msg.get("container-title", [])
    if containers and not art.venue:
        art.venue = containers[0]
        return True
    return False


def _apply_abstract(art: Article, msg: dict[str, Any]) -> bool:
    raw = msg.get("abstract", "")
    if raw and not art.abstract:
        art.abstract = _strip_html_tags(raw)
        return True
    return False


def _apply_pdf_url(art: Article, msg: dict[str, Any]) -> bool:
    if art.pdf_url:
        return False
    for link in msg.get("link", []):
        ct = (link.get("content-type") or "").lower()
        url = link.get("URL", "")
        if "pdf" in ct and url:
            art.pdf_url = url
            return True
    return False


async def _enrich_one_article(client: httpx.AsyncClient, art: Article) -> int:
    """Query Crossref for one article.  Returns 1 if enriched, else 0."""
    try:
        resp = await client.get(f"https://api.crossref.org/works/{art.doi}")
        if resp.status_code != 200:
            return 0
        msg = resp.json().get("message", {})
        return 1 if _enrich_article_from_msg(art, msg) else 0
    except Exception:
        return 0


async def _enrich_from_crossref(
    articles: list[Article],
    progress: ProgressCB = None,
) -> int:
    """Enrich articles in-place from Crossref API.  Returns count enriched."""
    enrichable = [
        a for a in articles if a.doi and not a.doi.startswith("UNRESOLVED")
    ]
    if not enrichable:
        return 0

    if progress:
        await progress(
            f"Phase 3: enriching metadata from Crossref for {len(enrichable)} articles\u2026"
        )

    enriched = 0
    async with httpx.AsyncClient(
        timeout=20,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        for i, art in enumerate(enrichable, start=1):
            enriched += await _enrich_one_article(client, art)
            if progress and i % 5 == 0:
                await progress(f"  Crossref: {i}/{len(enrichable)} queried")
            await asyncio.sleep(0.3)

    if progress:
        await progress(
            f"Phase 3 done: enriched {enriched}/{len(enrichable)} articles from Crossref"
        )
    return enriched


# ── Phase 4: download PDFs ───────────────────────────────────


async def _download_single_pdf(
    client: httpx.AsyncClient, art: Article, pdf_dir: Path,
) -> bool:
    """Download one PDF.  Returns True on success."""
    try:
        resp = await client.get(art.pdf_url)
        if resp.status_code != 200:
            return False
        ct = (resp.headers.get("content-type") or "").lower()
        ext = ".pdf" if "pdf" in ct else ".bin"
        dest = pdf_dir / f"{_safe_filename(art.doi)}{ext}"
        dest.write_bytes(resp.content)
        art.local_path = str(dest)
        return True
    except Exception:
        return False


async def _download_pdfs(
    articles: list[Article],
    pdf_dir: Path,
    progress: ProgressCB = None,
) -> int:
    """Download PDFs for articles that have a pdf_url.  Returns count ok."""
    to_dl = [a for a in articles if a.pdf_url and not a.local_path]
    if not to_dl:
        return 0

    if progress:
        await progress(f"Phase 4: downloading {len(to_dl)} PDFs\u2026")

    pdf_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0

    async with httpx.AsyncClient(
        timeout=60,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        for i, art in enumerate(to_dl, start=1):
            if await _download_single_pdf(client, art, pdf_dir):
                downloaded += 1
            if progress and i % 3 == 0:
                await progress(f"  PDF: {i}/{len(to_dl)} attempted")
            await asyncio.sleep(0.2)

    if progress:
        await progress(f"Phase 4 done: downloaded {downloaded}/{len(to_dl)} PDFs")
    return downloaded


# ── Main IEEE flow ───────────────────────────────────────────


async def fetch_references_ieee(
    url: str,
    progress: ProgressCB = None,
    pdf_dir: Path | None = None,
) -> list[Article]:
    """Full pipeline: skeleton → DOI resolve → Crossref enrich → PDF download."""
    pw, browser, ctx, page = await _launch_browser()
    try:
        skeletons = await _collect_skeletons(page, url, progress)
        if not skeletons:
            if progress:
                await progress("No reference entries found on the page")
            return []
        await _resolve_skeletons(ctx, skeletons, progress)
        articles = _skeletons_to_articles(skeletons)
    finally:
        await browser.close()
        await pw.stop()  # prevent orphan Playwright process

    # Phase 3 + 4 — no browser needed
    await _enrich_from_crossref(articles, progress)
    await _download_pdfs(articles, pdf_dir or _DEFAULT_PDF_DIR, progress)
    return articles


# ── Public API ───────────────────────────────────────────────


def _extract_doi_from_source(source: str) -> str | None:
    """Pull the raw DOI string from a URL or plain DOI."""
    m = DOI_REGEX.search(source)
    return m.group(0).rstrip(".,;)") if m else None


async def _try_semantic_scholar(
    doi: str, progress: ProgressCB,
) -> list[Article] | None:
    """Try Semantic Scholar API.  Returns list on success, None to fall back."""
    if progress:
        await progress("Querying Semantic Scholar API...")
    try:
        refs = await fetch_references_semantic_scholar(doi, progress)
        if refs:
            if progress:
                await progress(f"\u2713 Semantic Scholar: {len(refs)} references with DOIs")
            return refs
    except Exception as exc:
        if progress:
            await progress(f"API failed ({exc}), falling back to scraper...")
    return None


async def fetch_ieee_title(url: str) -> str:
    """Fetch the title of an IEEE document page via lightweight HTTP GET.

    Returns the cleaned title string, or empty string on failure.
    """
    _validate_source_url(url)
    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                m = re.search(
                    r"<title>\s*(.*?)\s*</title>", resp.text,
                    re.IGNORECASE | re.DOTALL,
                )
                if m:
                    raw = html_mod.unescape(m.group(1))
                    # Strip " | IEEE Journals & Magazine | IEEE Xplore" suffix
                    raw = re.split(r"\s*\|\s*IEEE\b", raw, maxsplit=1)[0]
                    return raw.strip()
    except Exception:
        pass
    return ""


async def fetch_references(
    source: str,
    *,
    prefer_api: bool = True,
    progress_callback: ProgressCB = None,
    pdf_dir: Path | None = None,
) -> list[Article]:
    """Fetch references for a survey given its URL or DOI.

    Parameters
    ----------
    source:
        An IEEE Xplore URL or a raw DOI string.
    prefer_api:
        When True, try Semantic Scholar first (faster / more reliable).
    progress_callback:
        Optional async callable for progress updates.
    pdf_dir:
        Directory to save downloaded PDFs.
    """
    doi = _extract_doi_from_source(source)

    if prefer_api and doi:
        refs = await _try_semantic_scholar(doi, progress_callback)
        if refs is not None:
            return refs

    url = source if source.startswith("http") else f"https://doi.org/{source}"

    # Validate before handing to Playwright (SSRF prevention)
    _validate_source_url(url)
    if progress_callback:
        await progress_callback(f"Scraping {url}...")

    refs = await fetch_references_ieee(url, progress_callback, pdf_dir)
    if progress_callback:
        n = sum(1 for r in refs if not r.doi.startswith("UNRESOLVED"))
        await progress_callback(
            f"\u2713 Scraping done: {n} DOIs resolved out of {len(refs)} refs"
        )
    return refs
