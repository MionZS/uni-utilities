from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Browser, Page, async_playwright

DOI_REGEX = re.compile(r"10\.\d{4,9}/[^\s<>\"{}|\\^`]+", re.IGNORECASE)
DOI_LABEL_REGEX = re.compile(r'DOI:\s*([^\s<>"]+)', re.IGNORECASE)
_UNSAFE_PATH_CHARS = re.compile(r"[\\/:*?\"<>|\s]+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract DOIs from article URLs and optionally download open-access PDFs via Unpaywall."
    )
    parser.add_argument("--input", "-i", default="automation/playwright-doi-downloader/articles.sample.json")
    parser.add_argument("--out", "-o", default="automation/playwright-doi-downloader/datalake")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--no-chrome", action="store_true")
    parser.add_argument("--unpaywall-email", default="mahmionc@gmail.com")
    parser.add_argument("--timeout-ms", type=int, default=30_000)
    parser.add_argument("--target-config", default="automation/playwright-doi-downloader/target.sample.json")
    parser.add_argument("--no-save-json", action="store_true")
    return parser.parse_args()


# ── Helpers ──────────────────────────────────────────────────


def _first_str(d: dict[str, Any], key: str) -> str | None:
    value = d.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _safe_prefix(doi: str) -> str:
    return _UNSAFE_PATH_CHARS.sub("_", doi)


def normalize_doi(raw: str | None) -> str | None:
    if not raw:
        return None
    doi = str(raw).strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    doi = doi.strip()
    doi = re.sub(r"[\]\[)>(\"'.,;]+$", "", doi)
    return doi or None


def safe_file_base_name(doi: str | None, fallback_title: str | None) -> str:
    if doi:
        return _safe_prefix(doi)
    title = (fallback_title or "article")[:80]
    title = _UNSAFE_PATH_CHARS.sub("_", title)
    title = re.sub(r"_+", "_", title).strip("_")
    return title or "article"


# ── Selector waiter ──────────────────────────────────────────


async def _wait_for_any_selector(page: Page, selectors: list[str], timeout_ms: int) -> str | None:
    for selector in selectors:
        try:
            await page.locator(selector).first.wait_for(state="attached", timeout=timeout_ms)
            return selector
        except Exception:
            continue
    return None


# ── Target-site helpers ──────────────────────────────────────


def load_target_config(target_config_path: str) -> dict[str, Any]:
    """Load and validate the target config JSON (synchronous, no await needed)."""
    config_path = Path(target_config_path).expanduser().resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise RuntimeError("target-config must be a JSON object")
    if not _first_str(config, "targetUrl"):
        raise RuntimeError("target-config requires a non-empty 'targetUrl'")
    return config


async def ensure_target_page(context_page: Page, config: dict[str, Any]) -> Page:
    context = context_page.context
    target_page = await context.new_page()
    await target_page.goto(str(config["targetUrl"]), wait_until="domcontentloaded")
    return target_page


async def paste_doi_into_target(page: Page, config: dict[str, Any], doi: str) -> None:
    locator = _resolve_input_locator(page, config)
    await locator.first.click()
    await locator.first.fill(doi)
    await _submit_target(page, locator, config)


def _resolve_input_locator(page: Page, config: dict[str, Any]) -> Any:
    """Resolve the input locator from config."""
    input_cfg = config.get("input")
    if not isinstance(input_cfg, dict):
        raise RuntimeError("target-config 'input' must be an object")

    by_role = input_cfg.get("byRole")
    if isinstance(by_role, dict):
        role = _first_str(by_role, "role")
        name = _first_str(by_role, "name")
        if role and name:
            return page.get_by_role(role, name=name)

    selector = _first_str(input_cfg, "selector")
    if selector:
        return page.locator(selector)

    raise RuntimeError("target-config input requires input.byRole{role,name} or input.selector")


async def _submit_target(page: Page, locator: Any, config: dict[str, Any]) -> None:
    """Submit the input form."""
    submit_cfg = config.get("submit", {})
    if not isinstance(submit_cfg, dict):
        raise RuntimeError("target-config 'submit' must be an object if provided")

    press = _first_str(submit_cfg, "press")
    click_selector = _first_str(submit_cfg, "clickSelector")
    if press:
        await locator.first.press(press)
    elif click_selector:
        await page.locator(click_selector).first.click()
    else:
        await locator.first.press("Enter")


# ── Post-submit steps ───────────────────────────────────────


async def run_post_submit_steps(page: Page, config: dict[str, Any]) -> None:
    """Execute optional postSubmitSteps defined in the target config."""
    steps = config.get("postSubmitSteps")
    if not isinstance(steps, list) or not steps:
        return

    for i, step in enumerate(steps, start=1):
        await _run_single_post_step(page, step, i)


async def _run_single_post_step(page: Page, step: dict[str, Any], index: int) -> None:
    """Run one post-submit step with retry support."""
    desc = step.get("description", f"step {index}")
    raw_selectors = step.get("selectors", "")
    timeout_ms = step.get("timeoutMs", 10_000)
    retries = step.get("retries", 1)
    retry_delay_ms = step.get("retryDelayMs", 2000)

    selectors = [s.strip() for s in raw_selectors.split(",") if s.strip()]
    if not selectors:
        print(f"  [postSubmit] {desc}: no selectors, skipping")
        return

    print(f"  [postSubmit] {desc}")
    clicked = await _try_click_selectors(page, selectors, timeout_ms, retries, retry_delay_ms)

    if not clicked:
        print(f"    (no selector matched after {retries} attempt(s))")
        return

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass


async def _try_click_selectors(
    page: Page, selectors: list[str], timeout_ms: int, retries: int, retry_delay_ms: int,
) -> bool:
    """Try clicking each selector with retries.  Returns True if any succeeded."""
    for attempt in range(1, retries + 1):
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="attached", timeout=timeout_ms)
                await loc.click()
                suffix = f" (attempt {attempt})" if attempt > 1 else ""
                print(f"    matched: {sel}{suffix}")
                return True
            except Exception:
                continue
        if attempt < retries:
            print(f"    retry {attempt}/{retries} \u2014 waiting {retry_delay_ms}ms...")
            await page.wait_for_timeout(retry_delay_ms)
    return False


# ── Download from target ─────────────────────────────────────


async def try_download_from_target(
    page: Page, config: dict[str, Any], doi: str, out_dir: Path,
) -> Path | None:
    result_cfg = config.get("result") or {}
    if not isinstance(result_cfg, dict):
        raise RuntimeError("target-config 'result' must be an object if provided")

    await _wait_for_result(page, result_cfg)

    download_cfg = config.get("download")
    if not isinstance(download_cfg, dict):
        return None

    return await _attempt_downloads(page, download_cfg, doi, out_dir)


async def _wait_for_result(page: Page, result_cfg: dict[str, Any]) -> None:
    """Wait for result selectors if configured."""
    wait_for = result_cfg.get("waitForAny")
    timeout_ms = result_cfg.get("timeoutMs", 15_000)
    if isinstance(timeout_ms, int) and timeout_ms > 0 and isinstance(wait_for, list):
        sels = [s for s in wait_for if isinstance(s, str) and s.strip()]
        if sels:
            await _wait_for_any_selector(page, sels, timeout_ms)


async def _attempt_downloads(
    page: Page, download_cfg: dict[str, Any], doi: str, out_dir: Path,
) -> Path | None:
    """Try multiple strategies to download a file."""
    click_candidates = download_cfg.get("clickFirstMatching")
    timeout_ms = download_cfg.get("timeoutMs", 60_000)
    if not isinstance(timeout_ms, int) or timeout_ms <= 0:
        timeout_ms = 60_000

    selectors: list[str] = []
    if isinstance(click_candidates, list):
        selectors = [s for s in click_candidates if isinstance(s, str) and s.strip()]

    for selector in selectors:
        result = await _try_download_selector(page, selector, doi, out_dir, timeout_ms)
        if result:
            return result
    return None


async def _try_download_selector(
    page: Page, selector: str, doi: str, out_dir: Path, timeout_ms: int,
) -> Path | None:
    """Try downloading via a single selector using multiple strategies."""
    loc = page.locator(selector).first
    try:
        await loc.wait_for(state="attached", timeout=1_500)
    except Exception:
        return None

    # Strategy 1: expect_download event
    result = await _try_download_event(page, loc, doi, out_dir, timeout_ms)
    if result:
        return result

    # Strategy 2: PDF response
    result = await _try_pdf_response(page, loc, doi, out_dir, timeout_ms)
    if result:
        return result

    # Strategy 3: new tab opens to PDF
    result = await _try_new_tab_pdf(page, loc, doi, out_dir)
    if result:
        return result

    # Strategy 4: same tab navigates to PDF
    return await _try_same_tab_pdf(page, loc, doi, out_dir)


async def _try_download_event(
    page: Page, loc: Any, doi: str, out_dir: Path, timeout_ms: int,
) -> Path | None:
    try:
        async with page.expect_download(timeout=timeout_ms) as dl_info:
            await loc.click()
        download = await dl_info.value
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"{_safe_prefix(doi)}__{download.suggested_filename}"
        await download.save_as(dest)
        return dest
    except Exception:
        return None


async def _try_pdf_response(
    page: Page, loc: Any, doi: str, out_dir: Path, timeout_ms: int,
) -> Path | None:
    try:
        async with page.expect_response(
            lambda r: "pdf" in (r.headers.get("content-type") or "").lower(),
            timeout=timeout_ms,
        ) as resp_info:
            await loc.click()
        resp = await resp_info.value
        if resp.ok:
            out_dir.mkdir(parents=True, exist_ok=True)
            dest = out_dir / f"{_safe_prefix(doi)}.pdf"
            dest.write_bytes(await resp.body())
            return dest
    except Exception:
        pass
    return None


async def _try_new_tab_pdf(page: Page, loc: Any, doi: str, out_dir: Path) -> Path | None:
    try:
        async with page.context.expect_page(timeout=2_500) as new_page_info:
            await loc.click()
        new_page = await new_page_info.value
        await new_page.wait_for_load_state("domcontentloaded", timeout=10_000)
        return await try_save_pdf_from_current_page(new_page, doi, out_dir)
    except Exception:
        return None


async def _try_same_tab_pdf(page: Page, loc: Any, doi: str, out_dir: Path) -> Path | None:
    try:
        await loc.click()
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        return await try_save_pdf_from_current_page(page, doi, out_dir)
    except Exception:
        return None


# ── DOI extraction ───────────────────────────────────────────


async def extract_doi_from_page(page: Page) -> str | None:
    """Extract a DOI from the current page using multiple strategies."""
    print("  [debug] Page title:", await page.title())
    print("  [debug] Page URL:", page.url)

    doi = await _doi_from_body_text(page)
    if doi:
        return doi

    doi = await _doi_from_meta_tags(page)
    if doi:
        return doi

    doi = await _doi_from_doi_links(page)
    if doi:
        return doi

    doi = await _doi_from_html_source(page)
    if doi:
        return doi

    print("  [debug] No DOI found by any method")
    return None


async def _doi_from_body_text(page: Page) -> str | None:
    try:
        body_text = await page.inner_text("body")
        match = DOI_LABEL_REGEX.search(body_text)
        if match:
            print(f"  [debug] Found DOI label in body text: {match.group(0)}")
            return normalize_doi(match.group(1))
    except Exception as exc:
        print(f"  [debug] body text search failed: {exc}")
    return None


async def _doi_from_meta_tags(page: Page) -> str | None:
    candidates = [
        'meta[name="citation_doi"]',
        'meta[name="dc.Identifier"]',
        'meta[name="dc.identifier"]',
        'meta[name="doi"]',
        'meta[name="Doi"]',
    ]
    for selector in candidates:
        try:
            value = await page.locator(selector).first.get_attribute("content")
        except Exception:
            value = None
        normalized = normalize_doi(value)
        if normalized:
            print(f"  [debug] Found DOI in meta tag {selector}: {normalized}")
            return normalized
    return None


async def _doi_from_doi_links(page: Page) -> str | None:
    try:
        href = await page.locator('a[href*="doi.org/"]').first.get_attribute("href")
    except Exception:
        href = None
    normalized = normalize_doi(href)
    if normalized:
        print(f"  [debug] Found DOI in doi.org link: {normalized}")
        return normalized
    return None


async def _doi_from_html_source(page: Page) -> str | None:
    try:
        html = await page.content()
        match = DOI_LABEL_REGEX.search(html)
        if match:
            print(f"  [debug] Found DOI label in HTML: {match.group(0)}")
            return normalize_doi(match.group(1))
        match = DOI_REGEX.search(html)
        if match:
            print(f"  [debug] Found DOI via regex in HTML: {match.group(0)}")
            return normalize_doi(match.group(0))
    except Exception:
        pass
    return None


# ── Crossref fallback ────────────────────────────────────────


async def extract_doi_from_crossref(page: Page, url: str) -> str | None:
    """Fallback: query Crossref by IEEE arnumber."""
    m = re.search(r"document/(\d+)", url)
    if not m:
        return None
    arnumber = m.group(1)
    query_url = (
        f"https://api.crossref.org/works?query.bibliographic={arnumber}"
        f"&filter=member:263&rows=1"
    )
    res = await page.request.get(
        query_url,
        headers={"user-agent": "utilities-playwright-doi-downloader/0.1.0", "accept": "application/json"},
    )
    if not res.ok:
        return None
    data = await res.json()
    items = data.get("message", {}).get("items", [])
    return _match_crossref_items(items, arnumber)


def _match_crossref_items(items: list[dict[str, Any]], arnumber: str) -> str | None:
    """Match Crossref results to the IEEE arnumber."""
    for item in items:
        doi_candidate = item.get("DOI")
        resource_url = item.get("resource", {}).get("primary", {}).get("URL", "")
        if arnumber in resource_url:
            return normalize_doi(doi_candidate)
        for link in item.get("link", []):
            if isinstance(link, dict) and arnumber in link.get("URL", ""):
                return normalize_doi(doi_candidate)
    if len(items) == 1:
        return normalize_doi(items[0].get("DOI"))
    return None


# ── Unpaywall ────────────────────────────────────────────────


async def unpaywall_lookup(page: Page, doi: str, email: str) -> dict[str, Any]:
    url = f"https://api.unpaywall.org/v2/{doi}?email={email}"
    res = await page.request.get(
        url,
        headers={"user-agent": "utilities-playwright-doi-downloader/0.1.0", "accept": "application/json"},
    )
    if not res.ok:
        raise RuntimeError(f"Unpaywall HTTP {res.status} for DOI {doi}")
    return await res.json()


def pick_best_pdf_url(unpaywall_json: dict[str, Any]) -> str | None:
    best = unpaywall_json.get("best_oa_location")
    if isinstance(best, dict) and best.get("url_for_pdf"):
        return str(best["url_for_pdf"])
    locations = unpaywall_json.get("oa_locations")
    if isinstance(locations, list):
        for loc in locations:
            if isinstance(loc, dict) and loc.get("url_for_pdf"):
                return str(loc["url_for_pdf"])
    return None


# ── PDF helpers ──────────────────────────────────────────────


def _looks_like_pdf_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in {"http", "https"}


async def try_save_pdf_from_current_page(page: Page, doi: str, out_dir: Path) -> Path | None:
    url = page.url
    if not url or not _looks_like_pdf_url(url):
        return None
    res = await page.request.get(url, timeout=60_000, max_redirects=10)
    if not res.ok:
        return None
    content_type = (res.headers.get("content-type") or "").lower()
    if "pdf" not in content_type:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{_safe_prefix(doi)}.pdf"
    dest.write_bytes(await res.body())
    return dest


async def download_to_file(page: Page, url: str, dest_path: Path) -> None:
    res = await page.request.get(url, timeout=60_000, max_redirects=10)
    if not res.ok:
        raise RuntimeError(f"Download HTTP {res.status} from {url}")
    body = await res.body()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(body)


# ── Browser launch ───────────────────────────────────────────


async def launch_browser(headless: bool, no_chrome: bool) -> tuple[Browser, Page]:
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        headless=headless,
        channel=None if no_chrome else "chrome",
    )
    context = await browser.new_context(
        accept_downloads=True,
        viewport={"width": 1920, "height": 1080},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="America/New_York",
    )
    page = await context.new_page()
    return browser, page


# ── Main run ─────────────────────────────────────────────────


async def run() -> int:
    args = parse_args()
    input_path = Path(args.input).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()

    articles: list[dict[str, Any]] = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(articles, list):
        raise RuntimeError("Input JSON must be an array of objects")

    browser, page = await launch_browser(args.headless, args.no_chrome)
    page.set_default_timeout(args.timeout_ms)

    target_config: dict[str, Any] | None = None
    target_page: Page | None = None
    if args.target_config:
        target_config = load_target_config(args.target_config)

    doi_found = 0
    pdf_downloaded = 0

    try:
        for idx, article in enumerate(articles, start=1):
            result = await _process_article(
                idx, len(articles), article, page, args, target_config, target_page, out_dir,
            )
            if result:
                target_page = result.get("target_page", target_page)
                doi_found += result.get("doi_found", 0)
                pdf_downloaded += result.get("pdf_downloaded", 0)
    finally:
        await browser.close()

    if not args.no_save_json:
        out_json = input_path.with_name("articles.with_doi.json")
        out_json.write_text(json.dumps(articles, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nWrote: {out_json}")

    print(f"\nDone. DOIs found: {doi_found}/{len(articles)}. OA PDFs downloaded: {pdf_downloaded}.")
    return 0


async def _process_article(
    idx: int,
    total: int,
    article: dict[str, Any],
    page: Page,
    args: argparse.Namespace,
    target_config: dict[str, Any] | None,
    target_page: Page | None,
    out_dir: Path,
) -> dict[str, Any] | None:
    """Process a single article.  Returns counters dict or None."""
    if not isinstance(article, dict):
        print(f"[{idx}/{total}] Skipping: item is not an object")
        return None

    url = article.get("originalUrl") or article.get("url") or article.get("ieeeUrl")
    if not url:
        print(f"[{idx}/{total}] Skipping: no URL fields found")
        return None

    print(f"[{idx}/{total}] Visiting: {url}")

    doi = await _resolve_article_doi(page, article, url)
    result: dict[str, Any] = {"target_page": target_page, "doi_found": 0, "pdf_downloaded": 0}

    if not doi:
        print("  DOI: (not found)")
        return result

    result["doi_found"] = 1
    article["doi"] = doi
    print(f"  DOI: {doi}")

    if target_config:
        target_page = await _handle_target(page, target_config, target_page, doi, args, article, out_dir)
        result["target_page"] = target_page

    if args.unpaywall_email:
        result["pdf_downloaded"] = await _handle_unpaywall(page, doi, article, args.unpaywall_email, out_dir)

    return result


async def _resolve_article_doi(
    page: Page, article: dict[str, Any], url: str,
) -> str | None:
    """Try to get a DOI for an article, including Crossref fallback."""
    doi = normalize_doi(article.get("doi"))
    if not doi:
        try:
            await page.goto(str(url), wait_until="networkidle")
            doi = await extract_doi_from_page(page)
        except Exception as exc:
            print(f"  DOI extraction from page failed: {exc}")

    if not doi and url and "ieee.org" in str(url):
        print("  [debug] Trying Crossref API fallback...")
        try:
            doi = await extract_doi_from_crossref(page, str(url))
            if doi:
                print(f"  [debug] Crossref found DOI: {doi}")
            else:
                print("  [debug] Crossref returned no DOI")
        except Exception as exc:
            print(f"  Crossref fallback failed: {exc}")
    return doi


async def _handle_target(
    page: Page,
    target_config: dict[str, Any],
    target_page: Page | None,
    doi: str,
    args: argparse.Namespace,
    article: dict[str, Any],
    out_dir: Path,
) -> Page | None:
    """Paste DOI into target and attempt download.  Returns updated target_page."""
    try:
        if target_page is None:
            target_page = await ensure_target_page(page, target_config)
            target_page.set_default_timeout(args.timeout_ms)
        else:
            await target_page.goto(str(target_config["targetUrl"]), wait_until="domcontentloaded")
        await paste_doi_into_target(target_page, target_config, doi)
        await run_post_submit_steps(target_page, target_config)
        downloaded = await try_download_from_target(target_page, target_config, doi, out_dir)
        if downloaded:
            article["targetDownloadedPath"] = str(downloaded)
            print(f"  Target download: {downloaded}")
        else:
            print("  Target download: (not available)")
    except Exception as exc:
        print(f"  Target automation failed: {exc}")
    return target_page


async def _handle_unpaywall(
    page: Page, doi: str, article: dict[str, Any], email: str, out_dir: Path,
) -> int:
    """Try Unpaywall OA download.  Returns 1 if success, 0 otherwise."""
    try:
        unpaywall_json = await unpaywall_lookup(page, doi, email)
        pdf_url = pick_best_pdf_url(unpaywall_json)
        if not pdf_url:
            print("  OA PDF: (not available)")
            return 0

        base_name = safe_file_base_name(doi, article.get("title"))
        pdf_path = out_dir / f"{base_name}.pdf"

        print(f"  OA PDF: {pdf_url}")
        await download_to_file(page, pdf_url, pdf_path)

        article["oaPdfUrl"] = pdf_url
        article["downloadedPath"] = str(pdf_path)
        print(f"  Saved: {pdf_path}")
        return 1
    except Exception as exc:
        print(f"  OA download failed: {exc}")
        return 0


def main() -> None:
    import asyncio
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
