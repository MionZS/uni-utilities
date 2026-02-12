# DOI “Target Site” Scaffold (Python Playwright)

## Goal
Let you pick a **legal** website where you can paste a DOI into a search box and (when the site offers it) download a PDF.

This is meant for:
- Open-access aggregators (e.g., Semantic Scholar) that expose legitimate PDF links
- Your university library resolver / discovery UI (if you’re authorized)
- Publisher sites you have access to

Not meant for:
- Paywall bypass
- Piracy mirrors

## How it works
The downloader already:
1) visits your `originalUrl` to extract the DOI
2) optionally downloads OA PDFs via Unpaywall API

This scaffold adds an **optional** third step:
3) open a target site tab once, and for each DOI:
   - fill the site’s search input
   - submit
   - if a “PDF”/download link exists, click it and save the file

## Config
See: automation/playwright-doi-downloader/target.sample.json

Fields:
- `targetUrl`: page to open for DOI search
- `input`: how to locate the input
  - `byRole`: `{ role, name }` (Playwright accessible selector)
  - OR `selector`: a CSS selector
- `submit`:
  - `press`: e.g. `Enter`
  - OR `clickSelector`: CSS selector to click
- `result`:
  - `waitForAny`: array of selectors; first one found means “results loaded”
  - `timeoutMs`
- `download`:
  - `clickFirstMatching`: array of selectors to try clicking
  - `timeoutMs`

## Recommended target
Semantic Scholar is a decent default because it often exposes OA PDFs when they exist.

## Usage
Once the code is wired up (next step), you’ll run:
`python automation/playwright-doi-downloader/downloader.py --input articles.json --target-config automation/playwright-doi-downloader/target.sample.json`
