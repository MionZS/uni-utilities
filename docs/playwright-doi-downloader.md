# Python Playwright: DOI Extractor + OA PDF Downloader

## Purpose
A Python-only utility that:
1. Reads a JSON file containing article objects (with `originalUrl` / `url` fields).
2. Visits each page and extracts the DOI.
3. Optionally uses Unpaywall to find an open-access PDF and downloads it.

## Scope / legality
This is intentionally scoped to **legal workflows** (open-access PDFs, institutional access you’re authorized to use, or author-hosted PDFs). It does not bypass paywalls.

## Files
- automation/playwright-doi-downloader/downloader.py
- automation/playwright-doi-downloader/articles.sample.json

## Input JSON format
Input must be a JSON array of objects. Keep whatever extra fields you want; the tool preserves them.

Supported URL fields (first match wins):
- `originalUrl`
- `url`
- `ieeeUrl`

The tool adds/updates:
- `doi` (if found)
- `oaPdfUrl` (if Unpaywall has a PDF)
- `downloadedPath` (where the PDF was saved)

## Install
1. Install Python deps (this repo uses `pyproject.toml`):
- Ensure `playwright` is installed in your environment

2. Install the browser once:
`python -m playwright install chromium`

## Run
### Extract DOI only
`python automation/playwright-doi-downloader/downloader.py --input articles.json --no-save-json`

### Extract DOI + download open-access PDFs
Unpaywall requires an email parameter.

`python automation/playwright-doi-downloader/downloader.py --input articles.json --out downloads --unpaywall-email you@example.com`

### Use installed Chrome (optional)
`python automation/playwright-doi-downloader/downloader.py --input articles.json --unpaywall-email you@example.com --chrome --headful`

## Notes on DOI extraction
Order:
1. Common meta tags (e.g., `citation_doi`)
2. `doi.org` links
3. DOI regex fallback
