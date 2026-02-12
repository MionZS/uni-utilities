# DEBUG GUIDE: Bibliography Manager & Scraper System
**For:** Future Developers & Debugging Sessions  
**Updated:** February 12, 2026  
**Cost:** 📊 Proper debugging saves ~5-10 hours per incident

---

## QUICK START: How to Debug Scraping Failures

### The "Save HTML & Inspect" Technique

This is your **most powerful debugging tool** for Phase 1 failures.

#### Why This Matters

When the scraper collects titles incorrectly (e.g., returning "CrossRef" instead of "Network Optimization"), the root cause is always **HTML structure changed**. Inspecting the raw HTML reveals this instantly.

#### How It Works (3 Steps)

**Step 1: Trigger HTML Export**

```python
# In scraper.py, _collect_skeletons() 
await _save_debug_html(page, url, progress)
```

This creates: `datalake/debug/{DOI}_{timestamp}.html`

**Step 2: Open & Inspect**

```bash
# On Windows
start datalake/debug/10_1234_sample_2024_02_12_143522.html

# On Mac
open datalake/debug/...html

# On Linux
xdg-open datalake/debug/...html
```

**Step 3: Understand the Reality**

Now you see what the browser actually saw. Open DevTools in your browser:

```
Right-click → Inspect Element → Look for <li> tags with references
```

You'll see the actual HTML structure, not what you assumed it was.

#### Real-World Example: The "CrossRef" Bug

**Problem:** All titles came back as "CrossRef"

**Old Code (Incorrect):**
```python
def _extract_title_from_ref(el, text):
    # Looking for a <span class="ref-title"> 
    title_el = el.select_one("span.ref-title")
    return title_el.get_text() if title_el else ""
```

**What happened:**
- Developer assumed IEEE structure was `<span class="ref-title">`
- Reality: IEEE changed to `<div data-test="ref-content"><p>title</p>`
- Code returned empty string → fallback used first link text → "CrossRef"

**Debug Solution:**
1. Save HTML: `await _save_debug_html(page, url, progress)`
2. Open HTML in browser
3. Right-click on "CrossRef" text → Inspect Element
4. See actual tag: `<a href="..." class="ref-link"> CrossRef </a>`
5. Find actual title: `<h3 class="ref-title">Network Optimization</h3>`
6. Update selector: `title_el = el.select_one("h3.ref-title")`
7. Test & deploy

**Time cost:** 15 minutes (vs. 2 hours of guessing)

---

## Debugging Workflow: Three-Layer Approach

### Layer 1: Phase 1 (Browser) - "skeleton" Formation

**Symptoms:**
- Empty titles
- Wrong authors
- Missing links

**Diagnosis Tools:**
1. HTML dump + browser inspection (see above)
2. Screenshot before/after tab click
3. Element locator timing

**Debug Commands:**

```python
# Add to scraper.py temporarily
async def debug_phase1(page: Page, url: str):
    """Dump the References tab state."""
    await _click_references_tab(page)
    
    # Screenshot
    page.screenshot(path="debug_screenshot_after_click.png")
    
    # HTML dump
    html = await page.content()
    Path("debug_raw_html.html").write_text(html)
    
    # Element count
    refs = await page.locator("[class*='reference'] li").all()
    print(f"Found {len(refs)} reference elements")
    
    # Inspect first ref
    if refs:
        first = refs[0]
        text = await first.inner_text()
        print(f"First ref text:\n{text[:500]}")
        
        links = await first.locator("a").all()
        print(f"First ref has {len(links)} links:")
        for link in links:
            href = await link.get_attribute("href")
            label = await link.inner_text()
            print(f"  {label} → {href}")
```

**Then:**
1. Run `python -c "asyncio.run(debug_phase1(...))"` against a test paper
2. Inspect `debug_raw_html.html` in browser
3. Update selectors based on reality

---

### Layer 2: Phase 2 (DOI Resolution) - "muscle" Addition

**Symptoms:**
- 50% of references missing DOI
- Wrong DOI assigned
- API timeouts

**Root Causes:**
1. Reference link died/changed
2. Strategy order matters (Crossref works, Scholar doesn't)
3. Network/timeout issue

**Diagnosis Tools:**
1. Trace logging per strategy
2. Save intermediate results
3. Isolate single reference

**Debug Commands:**

```python
# Minimal reproducible example
import asyncio
import httpx
from pathlib import Path

async def debug_phase2_single_ref():
    """Test DOI resolution for one reference."""
    
    skel = _RefSkeleton(
        index=1,
        title="Network Optimization",
        crossref_url="https://crossref.org/10.1109/...",
        google_scholar_url="https://scholar.google.com/...",
    )
    
    async with httpx.AsyncClient(timeout=10) as client:
        print(f"[Ref {skel.index}] Resolving: {skel.title}")
        
        # Try Crossref
        print(f"  → Trying Crossref: {skel.crossref_url}")
        try:
            resp = await client.get(skel.crossref_url)
            print(f"    HTTP {resp.status}")
            html = resp.text
            Path("debug_crossref_page.html").write_text(html)
            doi = _extract_doi_from_text(html)
            print(f"    Found DOI: {doi}")
            if doi:
                return doi
        except Exception as e:
            print(f"    ERROR: {e}")
        
        # Try Scholar
        print(f"  → Trying Scholar: {skel.google_scholar_url}")
        try:
            resp = await client.get(skel.google_scholar_url)
            print(f"    HTTP {resp.status}")
            html = resp.text
            Path("debug_scholar_page.html").write_text(html)
            # Extract from meta tags
            import re
            m = re.search(r'citation_doi["\']?\s*content=["\']([^"\']+)', html)
            doi = m.group(1) if m else None
            print(f"    Found DOI: {doi}")
            if doi:
                return doi
        except Exception as e:
            print(f"    ERROR: {e}")
        
        print(f"  ✗ All strategies failed")
        return None

# Run it:
# asyncio.run(debug_phase2_single_ref())
```

**Key Insight:** Save intermediate HTTP responses (`debug_crossref_page.html`) to inspect what the API actually returns.

---

### Layer 3: Phase 3 (Enrichment) - API Response Validation

**Symptoms:**
- Wrong metadata (title, authors, venue)
- Missing abstract
- PDF URL doesn't work

**Root Causes:**
1. Field name mismatch in response parsing
2. Array vs. string type mismatch
3. Null/empty fields in API response

**Diagnosis Tool:**
Dump and inspect raw Crossref API response

**Debug Commands:**

```python
# Add to scraper.py
async def debug_phase3_enrichment(doi: str):
    """Inspect Crossref response for a single DOI."""
    import json
    
    url = f"https://api.crossref.org/works/{doi}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        
        if not resp.is_success:
            print(f"ERROR: HTTP {resp.status}")
            return
        
        data = resp.json()
        msg = data.get("message", {})
        
        # Pretty-print the whole thing
        print(json.dumps(msg, indent=2, default=str))
        
        # Also save to file for reference
        Path("debug_crossref_response.json").write_text(
            json.dumps(msg, indent=2, default=str)
        )
        
        # Check specific fields
        print("\n=== PARSED FIELDS ===")
        print(f"Title: {msg.get('title')} (type: {type(msg.get('title')).__name__})")
        print(f"Authors: {len(msg.get('author', []))} found")
        print(f"Year: {msg.get('published', {}).get('date-parts')}")
        print(f"Venue: {msg.get('container-title', 'N/A')} (type: {type(msg.get('container-title')).__name__})")
        print(f"PDF URLs: {len(msg.get('link', []))} links")
        for link in msg.get("link", []):
            ct = link.get("content-type", "unknown")
            url = link.get("URL", "N/A")
            print(f"  {ct}: {url[:60]}...")

# Run:
# asyncio.run(debug_phase3_enrichment("10.1234/test"))
```

**Then inspect:** `debug_crossref_response.json` with `jq` or JSON viewer.

**Example Finding:**
```json
{
  "title": ["Network Optimization"],  // ← It's an ARRAY!
  "author": [
    {"family": "Smith", "given": "John"},
    {"family": "Jones", "given": "Alice"}
  ],
  "container-title": ["Nature"],  // ← Also an array!
  "link": [
    {"URL": "https://...pdf", "content-type": "application/pdf"}
  ]
}
```

**Fix:**
```python
# Your code might expect string; API returns array
title = msg.get("title")
if isinstance(title, list) and title:
    article.title = title[0]
elif isinstance(title, str):
    article.title = title
```

---

## TUI Debugging Reference

### Layout Issues (Bottom Bar Off-Screen)

**Symptom:** Button bar doesn't appear or gets cut off

**Root Cause:** Terminal too short, TUI renders components beyond visible area

**Solution: Use CSS Height Constraints**

```css
Screen {
  overflow-y: auto;      /* Allow scrolling if needed */
}

#button-bar {
  height: auto;          /* Don't expand */
  max-height: 3;         /* Limit to 3 lines */
}

#survey-table {
  min-height: 6;         /* Minimum height */
  overflow: auto;        /* Scroll if needed */
}
```

**Debug Steps:**
1. Launch with `--external` flag (gets you 130x40 terminal)
2. Or manually: `mode con: cols=120 lines=40` (Windows)
3. Check CSS in `app_styles.tcss`
4. Use Textual DevTools: `python -c "from textual.devtools import cli; cli()"` (in dev environment)

### Progress Events Not Updating

**Symptom:** Progress bar frozen, "Fetching..." stuck

**Root Cause:** Callback not awaited, async deadlock, or event loop blocked

**Debug:**

```python
# In _ProgressDispatcher.__call__
async def __call__(self, msg: str) -> None:
    print(f"[PROGRESS] {msg}")  # ← Add debug output
    
    # YOUR CODE HERE
    
    await asyncio.sleep(0)  # ← Must yield event loop
```

The `await asyncio.sleep(0)` is **critical**. Without it, the TUI event loop never gets CPU time.

---

## Common Failure Patterns & Solutions

### Pattern 1: "All References Got DOI: None"

**Probable Cause:** Link URLs stored incorrectly or not visited

**Check:**
```python
# After Phase 1, before Phase 2
for skel in skeletons:
    print(f"{skel.index}. {skel.title}")
    print(f"   Crossref: {skel.crossref_url}")
    print(f"   Scholar: {skel.google_scholar_url}")
    # If all empty, Phase 1 failed to classify links
```

**Fix:** Debug Phase 1 link classification (see Layer 1 debugging above)

### Pattern 2: "Crossref Returns 404 for All DOIs"

**Probable Cause:** DOI format is wrong

**Check:**
```python
# Validate DOI format
import re
for skel in skeletons:
    if skel.doi:
        if not re.match(r"10\.\d{4,9}", skel.doi):
            print(f"⚠️  Invalid DOI format: {skel.doi}")
```

**Valid DOI examples:**
- `10.1109/5.771073` ✓
- `10.1038/nature12373` ✓
- `DOI: 10.1234/test` ✗ (includes prefix)
- `http://doi.org/10.1234` ✗ (URL encoded)

Use `_clean_doi()` to normalize.

### Pattern 3: "PDF Download Fails silently"

**Probable Cause:** PDF URL invalid or network timeout

**Check:**
```python
# Test PDF download
import httpx

async def test_pdf_download(url: str):
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(url, follow_redirects=True)
            print(f"HTTP {resp.status}")
            print(f"Content-Type: {resp.headers.get('content-type')}")
            print(f"Content-Length: {len(resp.content)} bytes")
            
            if resp.status_code == 200 and "pdf" in resp.headers.get("content-type", ""):
                Path("debug_downloaded.pdf").write_bytes(resp.content)
                print("✓ Valid PDF")
            else:
                print("✗ Not a PDF or bad status")
        except Exception as e:
            print(f"✗ Download failed: {e}")

# asyncio.run(test_pdf_download("https://example.com/paper.pdf"))
```

---

## Instrumentation Checklist

Before you declare "it works," add these traces:

### Scraper Instrumentation
- [ ] Count references found in Phase 1
- [ ] Count DOIs resolved in Phase 2 (per strategy)
- [ ] Count articles enriched in Phase 3
- [ ] Count PDFs downloaded in Phase 4
- [ ] Log any article with missing critical field (doi, title, authors)

### TUI Instrumentation
- [ ] Log fetch start/end time
- [ ] Log progress callback invocations
- [ ] Log button clicks with timestamp
- [ ] Log survey selection + article count

### Example:
```python
@dataclass
class _Stats:
    refs_found: int = 0
    dois_resolved_via_crossref: int = 0
    dois_resolved_via_scholar: int = 0
    dois_failed: int = 0
    articles_enriched: int = 0
    pdfs_downloaded: int = 0

_stats = _Stats()

# In Phase 1:
refs = await page.locator("[class*='reference'] li").all()
_stats.refs_found = len(refs)

# In Phase 2, per strategy:
if doi via crossref:
    _stats.dois_resolved_via_crossref += 1

# At end:
print(f"""
STATS:
  Refs found: {_stats.refs_found}
  DOIs (Crossref): {_stats.dois_resolved_via_crossref}
  DOIs (Scholar): {_stats.dois_resolved_via_scholar}
  DOIs (Failed): {_stats.dois_failed}
  Enriched: {_stats.articles_enriched}
  PDFs: {_stats.pdfs_downloaded}
  Success rate: {(1 - _stats.dois_failed/_stats.refs_found)*100:.1f}%
""")
```

---

## Tools & Commands Reference

### Inspect HTML/JSON Outputs

```bash
# Windows - open in browser
explorer datalake/debug/10_1234*.html

# Linux/Mac - open in browser
open datalake/debug/10_1234*.html

# Inspect JSON with jq (install: choco install jq)
jq .message.title debug_crossref_response.json
jq '.message.author | length' debug_crossref_response.json
jq '.message.link[] | {url: .URL, type: ."content-type"}' debug_crossref_response.json
```

### Run Single Test Manually

```bash
cd d:\Uni\utilities

# Test Phase 1 + 2 on one IEEE paper
python -c "
import asyncio
from automation.bibliography_manager.scraper import fetch_references_ieee

# Replace XXXX with real IEEE ID
asyncio.run(fetch_references_ieee(
    url='https://ieeexplore.ieee.org/document/XXXX',
))
"

# Test Phase 3 enrichment on one DOI
python -c "
import asyncio
from automation.bibliography_manager.scraper import fetch_references_semantic_scholar

asyncio.run(fetch_references_semantic_scholar(doi='10.1109/78.298298'))
"
```

### Command-Line Help

```bash
# See all available flags
python -m automation.bibliography_manager --help

python -m automation.playwright-doi-downloader --help
```

---

## Quick Reference: Where to Add Debug Code

| Component | File | Function | What to Debug |
|---|---|---|---|
| HTML parsing | scraper.py | `_extract_title_from_ref` | Check if selector matches |
| Link extraction | scraper.py | `_classify_links` | Log URL + detected type |
| DOI resolution | scraper.py | `_resolve_single_doi` | Add trace per strategy |
| Crossref API | scraper.py | `_enrich_one_article` | Dump response JSON |
| TUI updates | app.py | `_ProgressDispatcher.__call__` | Print message type |
| PDF download | downloader.py | `_download_single_pdf` | Log URL + HTTP status |

---

## Before Declaring Success

✅ **Checklist:**

- [ ] Ran with sample data (5-10 papers) and inspected output
- [ ] Verified titles are meaningful (not "CrossRef", "View Article", etc.)
- [ ] Verified DOI count > 80% (some refs might not have DOI)
- [ ] Verified metadata fields populated (title, authors, year, abstract)
- [ ] Downloaded 1-2 PDFs manually to verify URLs work
- [ ] TUI displays results without crashing
- [ ] No exceptions in terminal output
- [ ] Bibliography JSON is valid (can be loaded back)

**If any check fails:** Start with Layer 1 debugging (HTML dump) and work down.

---

## Key Takeaway

**The HTML dump technique (Layer 1) solves ~80% of scraping problems in <20 minutes.**

When something breaks:
1. Save HTML: `await _save_debug_html(...)`
2. Open in browser
3. Inspect elements with DevTools
4. Update selectors
5. Test

This is **orders of magnitude faster** than printf debugging or guessing.

---

**Need help?** Check the TECHNICAL_REVIEW_SENIOR_CONSULTANT.md for deeper architectural guidance.
