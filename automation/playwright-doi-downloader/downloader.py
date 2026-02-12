from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Browser, Page, async_playwright

DOI_REGEX = re.compile(r"10\.\d{4,9}/[^\s<>\"{}|\\^`]+", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract DOIs from article URLs listed in a JSON file and optionally download open-access PDFs via Unpaywall."
        )
    )
    parser.add_argument("--input", "-i", default="automation/playwright-doi-downloader/articles.sample.json", help="Path to input JSON (array of objects)")
    parser.add_argument("--out", "-o", default="automation/playwright-doi-downloader/datalake", help="Output directory for PDFs")
    parser.add_argument("--headless", action="store_true", help="Run browser headless (default is headed)")
    parser.add_argument("--no-chrome", action="store_true", help="Use Playwright Chromium instead of installed Chrome")
    parser.add_argument(
        "--unpaywall-email",
        default="mahmionc@gmail.com",
        help="Email used for Unpaywall API (required to download OA PDFs)",
    )
    parser.add_argument("--timeout-ms", type=int, default=30_000, help="Navigation/selector timeout")
    parser.add_argument(
        "--target-config",
        default="automation/playwright-doi-downloader/target.sample.json",
        help=(
            "Optional JSON config describing a legal target site where the DOI should be pasted and (if available) a PDF downloaded."
        ),
    )
    parser.add_argument(
        "--no-save-json",
        action="store_true",
        help="Do not write articles.with_doi.json",
    )
    return parser.parse_args()


def _first_str(d: dict[str, Any], key: str) -> str | None:
    value = d.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return None


async def _wait_for_any_selector(page: Page, selectors: list[str], timeout_ms: int) -> str | None:
    for selector in selectors:
        try:
            await page.locator(selector).first.wait_for(state="attached", timeout=timeout_ms)
            return selector
        except Exception:
            continue
    return None


async def load_target_config(target_config_path: str) -> dict[str, Any]:
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
    input_cfg = config.get("input")
    if not isinstance(input_cfg, dict):
        raise RuntimeError("target-config 'input' must be an object")

    locator = None
    by_role = input_cfg.get("byRole")
    if isinstance(by_role, dict):
        role = _first_str(by_role, "role")
        name = _first_str(by_role, "name")
        if role and name:
            locator = page.get_by_role(role, name=name)

    selector = _first_str(input_cfg, "selector")
    if locator is None and selector:
        locator = page.locator(selector)

    if locator is None:
        raise RuntimeError("target-config input must provide either input.byRole{role,name} or input.selector")

    await locator.first.click()
    await locator.first.fill(doi)

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
        # Default fallback: Enter
        await locator.first.press("Enter")


async def run_post_submit_steps(page: Page, config: dict[str, Any]) -> None:
    """Execute optional postSubmitSteps defined in the target config."""
    steps = config.get("postSubmitSteps")
    if not isinstance(steps, list) or not steps:
        return

    for i, step in enumerate(steps, start=1):
        desc = step.get("description", f"step {i}")
        raw_selectors = step.get("selectors", "")
        timeout_ms = step.get("timeoutMs", 10_000)

        selectors = [s.strip() for s in raw_selectors.split(",") if s.strip()]
        if not selectors:
            print(f"  [postSubmit] {desc}: no selectors, skipping")
            continue

        print(f"  [postSubmit] {desc}")
        clicked = False
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="attached", timeout=timeout_ms)
                await loc.click()
                print(f"    matched: {sel}")
                clicked = True
                break
            except Exception:
                continue

        if not clicked:
            print(f"    (no selector matched for this step)")
            continue

        # Wait for navigation / network to settle after click
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass


async def try_download_from_target(page: Page, config: dict[str, Any], doi: str, out_dir: Path) -> Path | None:
    result_cfg = config.get("result", {})
    if result_cfg is None:
        result_cfg = {}
    if not isinstance(result_cfg, dict):
        raise RuntimeError("target-config 'result' must be an object if provided")

    wait_for_any = result_cfg.get("waitForAny")
    timeout_ms = result_cfg.get("timeoutMs", 15_000)
    if isinstance(timeout_ms, int) and timeout_ms > 0 and isinstance(wait_for_any, list) and wait_for_any:
        selectors = [s for s in wait_for_any if isinstance(s, str) and s.strip()]
        if selectors:
            await _wait_for_any_selector(page, selectors, timeout_ms)

    download_cfg = config.get("download")
    if not isinstance(download_cfg, dict):
        return None

    click_candidates = download_cfg.get("clickFirstMatching")
    download_timeout_ms = download_cfg.get("timeoutMs", 60_000)
    if not isinstance(download_timeout_ms, int) or download_timeout_ms <= 0:
        download_timeout_ms = 60_000

    selectors: list[str] = []
    if isinstance(click_candidates, list):
        selectors = [s for s in click_candidates if isinstance(s, str) and s.strip()]

    for selector in selectors:
        loc = page.locator(selector).first
        try:
            await loc.wait_for(state="attached", timeout=1_500)
        except Exception:
            continue

        try:
            async with page.expect_download(timeout=download_timeout_ms) as dl_info:
                await loc.click()
            download = await dl_info.value
            out_dir.mkdir(parents=True, exist_ok=True)
            suggested = download.suggested_filename
            safe_prefix = re.sub(r"[\\/:*?\"<>|\s]+", "_", doi)
            dest = out_dir / f"{safe_prefix}__{suggested}"
            await download.save_as(dest)
            return dest
        except Exception:
            # If no download event fires, some sites still serve a PDF response to display in-browser.
            # Try capturing the first application/pdf response triggered by the click.
            try:
                async with page.expect_response(
                    lambda r: "pdf" in (r.headers.get("content-type") or "").lower(),
                    timeout=download_timeout_ms,
                ) as resp_info:
                    await loc.click()
                resp = await resp_info.value
                if resp.ok:
                    out_dir.mkdir(parents=True, exist_ok=True)
                    safe_prefix = re.sub(r"[\\/:*?\"<>|\s]+", "_", doi)
                    dest = out_dir / f"{safe_prefix}.pdf"
                    dest.write_bytes(await resp.body())
                    return dest
            except Exception:
                pass

            # Not a download event. Some sites open a PDF in a tab instead of triggering a download.
            # Try: (1) new tab opens to a PDF; (2) current tab navigates to a PDF.

            # (1) new tab
            try:
                async with page.context.expect_page(timeout=2_500) as new_page_info:
                    await loc.click()
                new_page = await new_page_info.value
                await new_page.wait_for_load_state("domcontentloaded", timeout=10_000)
                saved = await try_save_pdf_from_current_page(new_page, doi, out_dir)
                if saved:
                    return saved
            except Exception:
                pass

            # (2) same tab
            try:
                await loc.click()
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                saved = await try_save_pdf_from_current_page(page, doi, out_dir)
                if saved:
                    return saved
            except Exception:
                pass

            continue

    return None


def normalize_doi(raw: str | None) -> str | None:
    if not raw:
        return None

    doi = str(raw).strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    doi = doi.strip()
    doi = re.sub(r"[\]\[)>(\"'.,;]+$", "", doi)
    return doi or None


DOI_LABEL_REGEX = re.compile(r'DOI:\s*([^\s<>"]+)', re.IGNORECASE)


async def extract_doi_from_page(page: Page) -> str | None:
    print("  [debug] Page title:", await page.title())
    print("  [debug] Page URL:", page.url)

    # 1) Look for visible "DOI: ..." text on the page
    try:
        body_text = await page.inner_text("body")
        match = DOI_LABEL_REGEX.search(body_text)
        if match:
            print(f"  [debug] Found DOI label in body text: {match.group(0)}")
            return normalize_doi(match.group(1))
    except Exception as exc:
        print(f"  [debug] body text search failed: {exc}")

    # 2) Meta tags
    meta_candidates = [
        'meta[name="citation_doi"]',
        'meta[name="dc.Identifier"]',
        'meta[name="dc.identifier"]',
        'meta[name="doi"]',
        'meta[name="Doi"]',
    ]

    for selector in meta_candidates:
        try:
            value = await page.locator(selector).first.get_attribute("content")
        except Exception:
            value = None
        normalized = normalize_doi(value)
        if normalized:
            print(f"  [debug] Found DOI in meta tag {selector}: {normalized}")
            return normalized

    # 3) doi.org links
    try:
        doi_href = await page.locator('a[href*="doi.org/"]').first.get_attribute("href")
    except Exception:
        doi_href = None
    normalized_href = normalize_doi(doi_href)
    if normalized_href:
        print(f"  [debug] Found DOI in doi.org link: {normalized_href}")
        return normalized_href

    # 4) Full HTML source with DOI: label
    try:
        html = await page.content()
        match = DOI_LABEL_REGEX.search(html)
        if match:
            print(f"  [debug] Found DOI label in HTML: {match.group(0)}")
            return normalize_doi(match.group(1))
    except Exception:
        pass

    # 5) Full HTML with generic DOI regex
    try:
        html = await page.content()
        match = DOI_REGEX.search(html)
        if match:
            print(f"  [debug] Found DOI via regex in HTML: {match.group(0)}")
            return normalize_doi(match.group(0))
    except Exception:
        pass

    print("  [debug] No DOI found by any method")
    return None


async def extract_doi_from_crossref(page: Page, url: str) -> str | None:
    """Fallback: extract the IEEE arnumber from the URL, query Crossref for the DOI."""
    import re as _re
    m = _re.search(r"document/(\d+)", url)
    if not m:
        return None
    arnumber = m.group(1)
    # Crossref doesn't index by arnumber directly, but we can search by URL
    query_url = f"https://api.crossref.org/works?query.bibliographic={arnumber}&filter=member:263&rows=1"
    res = await page.request.get(
        query_url,
        headers={
            "user-agent": "utilities-playwright-doi-downloader/0.1.0",
            "accept": "application/json",
        },
    )
    if not res.ok:
        return None
    data = await res.json()
    items = data.get("message", {}).get("items", [])
    if not items:
        return None
    # Verify the DOI links back to this arnumber
    for item in items:
        doi_candidate = item.get("DOI")
        links = item.get("link", [])
        resource_url = item.get("resource", {}).get("primary", {}).get("URL", "")
        if arnumber in resource_url:
            return normalize_doi(doi_candidate)
        for link in links:
            if isinstance(link, dict) and arnumber in link.get("URL", ""):
                return normalize_doi(doi_candidate)
    # If only one result, trust it
    if len(items) == 1:
        return normalize_doi(items[0].get("DOI"))
    return None


async def unpaywall_lookup(page: Page, doi: str, email: str) -> dict[str, Any]:
    url = f"https://api.unpaywall.org/v2/{doi}?email={email}"
    res = await page.request.get(
        url,
        headers={
            "user-agent": "utilities-playwright-doi-downloader/0.1.0",
            "accept": "application/json",
        },
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


def safe_file_base_name(doi: str | None, fallback_title: str | None) -> str:
    if doi:
        return re.sub(r"[\\/:*?\"<>|\s]+", "_", doi)

    title = (fallback_title or "article")[:80]
    title = re.sub(r"[\\/:*?\"<>|\s]+", "_", title)
    title = re.sub(r"_+", "_", title).strip("_")
    return title or "article"


async def download_to_file(page: Page, url: str, dest_path: Path) -> None:
    res = await page.request.get(url, timeout=60_000, max_redirects=10)
    if not res.ok:
        raise RuntimeError(f"Download HTTP {res.status} from {url}")

    body = await res.body()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(body)


def _looks_like_pdf_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False

    # Some sites serve PDFs behind viewer URLs that don't end with .pdf.
    # We'll verify via Content-Type when fetching.
    return True


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
    safe_prefix = re.sub(r"[\\/:*?\"<>|\s]+", "_", doi)
    dest = out_dir / f"{safe_prefix}.pdf"
    dest.write_bytes(await res.body())
    return dest


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
        target_config = await load_target_config(args.target_config)

    doi_found = 0
    pdf_downloaded = 0

    try:
        for idx, article in enumerate(articles, start=1):
            if not isinstance(article, dict):
                print(f"[{idx}/{len(articles)}] Skipping: item is not an object")
                continue

            url = article.get("originalUrl") or article.get("url") or article.get("ieeeUrl")
            if not url:
                print(f"[{idx}/{len(articles)}] Skipping: no URL fields found")
                continue

            print(f"[{idx}/{len(articles)}] Visiting: {url}")

            doi = normalize_doi(article.get("doi"))
            if not doi:
                try:
                    await page.goto(str(url), wait_until="networkidle")
                    doi = await extract_doi_from_page(page)
                except Exception as exc:
                    print(f"  DOI extraction from page failed: {exc}")
                    doi = None

            # Fallback: try Crossref API for IEEE URLs
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
                    doi = None

            if doi:
                doi_found += 1
                article["doi"] = doi
                print(f"  DOI: {doi}")
            else:
                print("  DOI: (not found)")
                continue

            if target_config:
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

            if not args.unpaywall_email:
                continue

            try:
                unpaywall_json = await unpaywall_lookup(page, doi, args.unpaywall_email)
                pdf_url = pick_best_pdf_url(unpaywall_json)
                if not pdf_url:
                    print("  OA PDF: (not available)")
                    continue

                base_name = safe_file_base_name(doi, article.get("title"))
                pdf_path = out_dir / f"{base_name}.pdf"

                print(f"  OA PDF: {pdf_url}")
                await download_to_file(page, pdf_url, pdf_path)

                pdf_downloaded += 1
                article["oaPdfUrl"] = pdf_url
                article["downloadedPath"] = str(pdf_path)
                print(f"  Saved: {pdf_path}")
            except Exception as exc:
                print(f"  OA download failed: {exc}")

    finally:
        await browser.close()

    if not args.no_save_json:
        out_json = input_path.with_name("articles.with_doi.json")
        out_json.write_text(json.dumps(articles, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nWrote: {out_json}")

    print(f"\nDone. DOIs found: {doi_found}/{len(articles)}. OA PDFs downloaded: {pdf_downloaded}.")
    return 0


def main() -> None:
    import asyncio

    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
