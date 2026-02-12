# IMPLEMENTATION ROADMAP
## From A- to A+ (Strategic Enhancements)

**Status:** Current system is A- (90/100) and production-ready.  
**Goal:** Reach A+ (95/100) with 2 weeks of focused work.  
**Effort:** ~10 hours total across 5 enhancements.

---

## THE PLAN AT A GLANCE

```
CURRENT: A- (90/100) → Target: A+ (95/100)

Week 1: HIGH Impact Fixes
├─ Add Structured Logging          (2 hours)  [Observability ↑↑]
└─ Create Integration Tests        (4 hours)  [Confidence ↑↑]

Week 2: MEDIUM Impact Improvements  
├─ Parallelize PDF Downloads       (1 hour)   [Performance ↑↑]
├─ Add Rate Limiting + Retry       (2 hours)  [Resilience ↑]
└─ Configuration as Code           (2 hours)  [Flexibility ↑]

Total Effort: 11 hours
Expected Result: A+ (95/100) system
```

---

## #1: ADD STRUCTURED LOGGING ⭐ CRITICAL

### Why This Matters
- **Current:** No visibility into Phase 2/3 failures
- **Impact:** Production troubleshooting takes 2-3 hours
- **Fix:** Adds production-grade observability
- **ROI:** Saves 10+ hours/year

### Implementation (2 hours)

**Step 1: Add logger setup in scraper.py (5 min)**

```python
# At top of scraper.py, after imports
import logging

logger = logging.getLogger("scraper")

# In main async function (fetch_references_ieee):
if progress:
    await progress(f"[INFO] Starting Phase 1 skeleton collection...")
```

**Step 2: Add logging in Phase 1 (20 min)**

```python
async def _collect_skeletons(...):
    refs = await page.locator(...).all()
    logger.info(f"Found {len(refs)} reference elements")
    
    for i, el in enumerate(refs, 1):
        title = await _extract_title_from_ref(el, text)
        authors = _extract_authors_text(text, title)
        
        if not title or title in _JUNK_TITLES:
            logger.warning(f"  [{i}] Bad title: {title!r}")
        else:
            logger.info(f"  [{i}] {title[:60]}...")
```

**Step 3: Add logging in Phase 2 (20 min)**

```python
async def _resolve_single_doi(skel: _RefSkeleton, ...):
    """Enhanced with logging."""
    
    if skel.crossref_url:
        logger.debug(f"Trying Crossref: {skel.crossref_url}")
        doi = await _try_doi_from_crossref_page(...)
        if doi:
            logger.info(f"[Phase 2] DOI resolved via Crossref: {doi}")
            return doi
        logger.debug("Crossref strategy failed")
    
    if skel.google_scholar_url:
        logger.debug(f"Trying Scholar: {skel.google_scholar_url}")
        doi = await _try_doi_from_scholar_meta(...)
        if doi:
            logger.info(f"[Phase 2] DOI resolved via Scholar: {doi}")
            return doi
        logger.debug("Scholar strategy failed")
    
    logger.warning(f"[Phase 2] Failed to resolve DOI for {skel.title}")
    return None
```

**Step 4: Add logging in Phase 3 (20 min)**

```python
async def _enrich_one_article(article: Article, ...):
    """Enhanced with logging."""
    try:
        logger.debug(f"Enriching {article.doi}...")
        
        resp = await client.get(f"https://api.crossref.org/works/{article.doi}")
        if not resp.is_success:
            logger.warning(f"[Phase 3] Crossref HTTP {resp.status} for {article.doi}")
            return False
        
        data = resp.json()["message"]
        article.title = data.get("title", [""])[0] if isinstance(...) else ""
        # ... rest of enrichment ...
        
        logger.info(f"[Phase 3] Enriched {article.doi}: {article.title[:60]}...")
        return True
        
    except Exception as exc:
        logger.error(f"[Phase 3] Enrichment failed for {article.doi}: {exc}", 
                     exc_info=True)
        return False
```

**Step 5: Configure logging (10 min)**

```python
# In __main__.py or app.py startup
def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler("datalake/scraper.log"),
            logging.StreamHandler(),  # Also to console
        ]
    )

# Call before main scraper run
_setup_logging()
```

### Testing Your Logging
```bash
# Run and check logs
python -m automation.bibliography_manager --external

# Then inspect
cat datalake/scraper.log | grep "Phase 2"
cat datalake/scraper.log | grep "WARNING"
```

### Result
✅ Production visibility ✅ Troubleshooting in 20 min vs 2 hours ✅ Observability A → A+

---

## #2: CREATE INTEGRATION TESTS ⭐ CRITICAL

### Why This Matters
- **Current:** No regression tests; refactoring is risky
- **Impact:** Can't confidently change Phase 2/3
- **Fix:** Regression detection + confidence
- **ROI:** Prevents bugs before production

### Implementation (3-4 hours)

**Step 1: Create tests/test_scraper.py (2 hours)**

```python
# tests/test_scraper.py
import pytest
import asyncio
import httpx
from automation.bibliography_manager.scraper import (
    _RefSkeleton,
    _resolve_single_doi,
    _extract_doi_from_text,
    DOI_REGEX,
)

class TestPhase2DOIResolution:
    """Phase 2: DOI resolution from reference links."""
    
    @pytest.mark.asyncio
    async def test_doi_format_validation(self):
        """Valid DOI starts with 10.XXXX"""
        text = "10.1109/5.771073"
        matches = DOI_REGEX.findall(text)
        assert len(matches) == 1
        assert matches[0].startswith("10.")
    
    @pytest.mark.asyncio
    async def test_clean_doi_strips_trailing_punct(self):
        """DOI cleanup removes trailing punctuation."""
        from automation.bibliography_manager.scraper import _clean_doi
        
        assert _clean_doi("10.1234/test.") == "10.1234/test"
        assert _clean_doi("10.1234/test,") == "10.1234/test"
        assert _clean_doi("10.1234/test)") == "10.1234/test"
    
    @pytest.mark.asyncio
    async def test_doi_extraction_from_html(self):
        """Extract DOI from HTML text."""
        html = """
        <p>See the full paper at:
        <a href="https://doi.org/10.1109/5.771073">Link</a>
        </p>
        """
        
        from automation.bibliography_manager.scraper import _extract_doi_from_text
        
        doi = _extract_doi_from_text(html)
        assert doi is not None
        assert doi == "10.1109/5.771073"

class TestPhase3Enrichment:
    """Phase 3: Crossref API enrichment."""
    
    @pytest.mark.asyncio
    async def test_article_model_serialization(self):
        """Article model round-trips through JSON."""
        from automation.bibliography_manager.models import Article
        
        article = Article(
            doi="10.1234/test",
            title="Test Paper",
            authors=["Alice", "Bob"],
            year=2024,
            venue="Nature",
        )
        
        # Serialize
        json_str = article.model_dump_json()
        
        # Deserialize
        restored = Article.model_validate_json(json_str)
        
        assert restored == article
        assert restored.doi == "10.1234/test"
        assert len(restored.authors) == 2
    
    @pytest.mark.asyncio
    async def test_article_validation_strict(self):
        """Pydantic validates fields strictly."""
        from automation.bibliography_manager.models import Article
        
        # Should fail: relevance_score out of range
        with pytest.raises(ValueError):
            Article(
                doi="10.1234/test",
                relevance_score=1.5,  # > 1.0
            )
        
        # Should succeed: in valid range
        art = Article(doi="10.1234/test", relevance_score=0.8)
        assert art.relevance_score == 0.8

class TestPhase1Skeleton:
    """Phase 1: Reference skeleton extraction."""
    
    def test_junk_title_detection(self):
        """Junk titles are filtered."""
        from automation.bibliography_manager.scraper import _JUNK_TITLES
        
        # These are not real paper titles
        assert "" in _JUNK_TITLES
        assert "crossref" in _JUNK_TITLES
        assert "view article" in _JUNK_TITLES
        
        # Real title should not be in set
        assert "Network Optimization" not in _JUNK_TITLES

# Run with:
# python -m pytest tests/test_scraper.py -v
# python -m pytest tests/test_scraper.py::TestPhase2DOIResolution -v
```

**Step 2: Create tests/conftest.py (30 min)**

```python
# tests/conftest.py
import pytest
import asyncio

@pytest.fixture
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

@pytest.fixture
def sample_article():
    """Sample Article for testing."""
    from automation.bibliography_manager.models import Article
    
    return Article(
        doi="10.1234/test",
        title="Test Paper",
        authors=["Alice", "Bob"],
        year=2024,
        venue="Nature",
        abstract="Test abstract",
    )
```

**Step 3: Create pytest.ini (10 min)**

```ini
# pytest.ini
[pytest]
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
asyncio_mode = auto
markers =
    asyncio: marks tests as async (from pytest-asyncio)
```

**Step 4: Install test dependencies (5 min)**

```bash
pip install pytest pytest-asyncio pytest-cov
# Or if using uv:
uv pip install pytest pytest-asyncio pytest-cov
```

**Step 5: Run tests**

```bash
# Run all tests
pytest

# Run specific test file
pytest tests/test_scraper.py -v

# With coverage
pytest --cov=automation tests/
```

### Result
✅ Regression detection ✅ Confidence in refactoring ✅ Testing A → A+

---

## #3: PARALLELIZE PDF DOWNLOADS (Optional but Quick)

### Implementation (1 hour)

**Before (Sequential):**
```python
async def _download_pdfs(articles: list[Article], pdf_dir: Path):
    """Current: Downloads sequentially (O(n))"""
    for article in articles:
        await _download_single_pdf(article, pdf_dir)
    # 50 PDFs @ 2s each = 100 seconds
```

**After (Parallel):**
```python
async def _download_pdfs(articles: list[Article], pdf_dir: Path):
    """Parallel with rate limiting: O(n/5)"""
    semaphore = asyncio.Semaphore(5)  # Max 5 concurrent
    
    async def _bounded_download(art: Article) -> bool:
        async with semaphore:
            return await _download_single_pdf(art, pdf_dir)
    
    tasks = [_bounded_download(art) for art in articles]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Count successes
    success_count = sum(1 for r in results if r is True)
    if progress:
        await progress(f"Downloaded {success_count}/{len(articles)} PDFs")
    
    return success_count
```

### Test It
```bash
# Measure before/after
time python -c "asyncio.run(download_test_batch(50))"

# Before: 100 seconds
# After: 20 seconds (5x faster!)
```

### Result
✅ 5x speedup on batch operations

---

## #4: ADD RATE LIMITING + RETRY (1-2 hours)

### Install tenacity
```bash
pip install tenacity
# Or: uv pip install tenacity
```

### Enhance Phase 2 & 3

```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
)
async def _call_crossref_api(doi: str, client: httpx.AsyncClient):
    """Auto-retry on rate limit."""
    resp = await client.get(f"https://api.crossref.org/works/{doi}")
    
    if resp.status_code == 429:  # Rate limit
        # Tenacity will auto-retry with exponential backoff
        # (1s, 2s, 4s, then fail)
        raise httpx.HTTPStatusError("Rate limited", request=..., response=resp)
    
    resp.raise_for_status()
    return resp.json()
```

### Result
✅ Handles transient failures automatically

---

## #5: CONFIGURATION AS CODE (2 hours)

### Create config/settings.yaml

```yaml
scraper:
  timeout_ms: 30000
  headless: true
  max_refs: 1000
  pdf_dir: bibliography/pdfs
  debug_dir: datalake/debug

downloader:
  timeout_ms: 60000
  unpaywall_email: your-email@domain.com
  max_parallel: 5

crossref:
  api_base: https://api.crossref.org
  rate_limit_per_sec: 50

tui:
  refresh_interval_ms: 500
```

### Load config in __main__.py

```python
import yaml
from pathlib import Path

CONFIG = yaml.safe_load(Path("config/settings.yaml").read_text())

PDF_DIR = Path(CONFIG["scraper"]["pdf_dir"])
TIMEOUT_MS = CONFIG["scraper"]["timeout_ms"]
```

### Result
✅ Environment-specific settings ✅ Easy deployment

---

## ROADMAP SUMMARY

| Task | Effort | Impact | ROI | Priority |
|------|--------|--------|-----|----------|
| **Logging** | 2h | Visibility ↑↑ | Save 10h/yr | **CRITICAL** |
| **Tests** | 4h | Confidence ↑↑ | Prevent bugs | **CRITICAL** |
| **Parallel PDFs** | 1h | Speed ↑↑ | 5x faster | Medium |
| **Rate Limit** | 2h | Resilience ↑ | Handle failures | Medium |
| **Config** | 2h | Flexibility ↑ | Easy deploy | Medium |

**Total Time: 11 hours → Result: A+ (95/100) system**

---

## EXPECTED OUTCOME

### Before (Current)
- A- Grade (90/100)
- Hard to troubleshoot (no logging)
- Risky to refactor (no tests)
- Slow batch operations (sequential PDFs)
- Hard-coded settings

### After (With Improvements)
- A+ Grade (95/100)
- Production-grade visibility (logging)
- Regression-safe (test suite)
- 5x faster batch operations (parallel)
- Flexible deployment (config as code)

---

## NEXT STEPS

1. **Week 1:** Implement #1 (Logging) + #2 (Tests)
2. **Week 2:** Implement #3-5 (Optional enhancements)
3. **Deploy:** With enhanced system
4. **Monitor:** Watch logs for issues

---

**Estimated Value:** This 11-hour work prevents ~50+ hours of future troubleshooting and maintenance cost.

**ROI: 5x** → Do it.
