"""Bibliography Manager — Textual TUI application.

Launch with:  python -m automation.bibliography_manager
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Log,
    ProgressBar,
    Static,
)

from caseconverter import snakecase
from urllib.parse import urlparse, parse_qs

from .models import Article, Bibliography, Survey
from . import storage
from .scraper import fetch_ieee_title, fetch_references

# ── Constants ────────────────────────────────────────────────

_FRAC_RE = re.compile(r"(\d+)/(\d+)")
_MSG_SURVEY_NOT_FOUND = "Survey not found"
_IEEE_DELAY_RENAME: float = 10.0  # respect IEEE robots.txt crawl-delay

# ── Utility ──────────────────────────────────────────────────


def _open_in_editor(path: Path) -> None:
    """Open *path* in the OS default editor."""
    system = platform.system()
    if system == "Windows":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif system == "Darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ── Stats Card ───────────────────────────────────────────────


class StatsCard(Static):
    """A single stat box for the dashboard."""

    def __init__(self, title: str, value: str = "\u2014", **kw: Any) -> None:
        super().__init__(**kw)
        self._title = title
        self._value = value

    def compose(self) -> ComposeResult:
        yield Label(self._title, classes="stats-title")
        yield Label(self._value, id="stats-value", classes="stats-value")

    def update_value(self, value: str) -> None:
        self._value = value
        try:
            self.query_one("#stats-value", Label).update(value)
        except Exception:
            pass


# ── Add Survey Modal ─────────────────────────────────────────


class AddSurveyModal(ModalScreen[dict[str, str] | None]):
    """Prompt the user for a survey name + source (URL or DOI)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label("Add New Survey", id="modal-title")
            yield Label("Name / short description:")
            yield Input(placeholder="e.g. IoT Smart Grid Survey 2024", id="survey-name")
            yield Label("IEEE Xplore URL or DOI:")
            yield Input(
                placeholder="https://ieeexplore.ieee.org/document/\u2026 or 10.1109/\u2026",
                id="survey-source",
            )
            with Horizontal(id="modal-buttons"):
                yield Button("Add", variant="primary", id="btn-add")
                yield Button("Cancel", id="btn-cancel")

    @on(Button.Pressed, "#btn-add")
    def _on_add(self) -> None:
        name = self.query_one("#survey-name", Input).value.strip()
        source = self.query_one("#survey-source", Input).value.strip()
        if not source:
            self.notify("Source is required", severity="error")
            return
        self.dismiss({"name": name, "source": source})

    @on(Button.Pressed, "#btn-cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Progress-message parser ─────────────────────────────────


class _ProgressState:
    """Mutable counters used by the fetch progress modal."""

    __slots__ = ("total_refs", "dois_resolved", "dois_inline")

    def __init__(self) -> None:
        self.total_refs = 0
        self.dois_resolved = 0
        self.dois_inline = 0


def _parse_frac(text: str) -> tuple[int, int] | None:
    m = _FRAC_RE.search(text)
    return (int(m.group(1)), int(m.group(2))) if m else None


class _ProgressDispatcher:
    """Break up the giant _progress callback into focused handlers."""

    def __init__(
        self,
        state: _ProgressState,
        log: Log,
        bar: ProgressBar,
        set_phase: Any,
        set_counter: Any,
    ) -> None:
        self._s = state
        self._log = log
        self._bar = bar
        self._set_phase = set_phase
        self._set_counter = set_counter

    # The callback must be a coroutine for the scraper protocol.
    async def __call__(self, msg: str) -> None:
        self._log.write_line(msg)
        # Yield to the event loop so the TUI repaints
        await asyncio.sleep(0)

        if msg.startswith("Phase 1:"):
            self._handle_phase1(msg)
        elif msg.strip().startswith("Skeleton:"):
            self._handle_skeleton(msg)
        elif msg.startswith("Phase 1 done"):
            self._handle_phase1_done(msg)
        elif msg.startswith("Phase 2:"):
            self._set_phase("Phase 2: resolving DOIs (visiting each ref)\u2026")
        elif "\u2713" in msg and "[" in msg:
            self._handle_doi_resolved(msg)
        elif "\u2717" in msg and "[" in msg:
            self._handle_doi_failed(msg)
        elif msg.startswith("Phase 2 done"):
            self._handle_phase2_done()
        elif msg.startswith("Phase 3:"):
            self._set_phase("Phase 3: enriching metadata from Crossref\u2026")
        elif msg.strip().startswith("Crossref:"):
            self._handle_crossref(msg)
        elif msg.startswith("Phase 3 done"):
            self._set_phase("Phase 3 complete \u2014 metadata enriched")
        elif msg.startswith("Phase 4:"):
            self._set_phase("Phase 4: downloading PDFs\u2026")
        elif msg.strip().startswith("PDF:"):
            self._handle_pdf_progress(msg)
        elif msg.startswith("Phase 4 done"):
            self._set_phase("Phase 4 complete \u2014 PDFs downloaded")
        elif "Semantic Scholar" in msg or "API:" in msg:
            self._handle_api(msg)

    # ── individual handlers ──────────────────────────────────

    def _handle_phase1(self, msg: str) -> None:
        self._set_phase("Phase 1: collecting reference skeletons\u2026")
        m = re.search(r"found (\d+)", msg)
        if m:
            self._s.total_refs = int(m.group(1))
            self._bar.update(total=self._s.total_refs * 2, progress=0)
            self._set_counter(f"0/{self._s.total_refs} refs collected")

    def _handle_skeleton(self, msg: str) -> None:
        frac = _parse_frac(msg)
        if frac:
            self._bar.update(progress=frac[0])
            self._set_counter(f"{frac[0]}/{self._s.total_refs} refs collected")

    def _handle_phase1_done(self, msg: str) -> None:
        m = re.search(r"(\d+) DOIs found inline", msg)
        if m:
            self._s.dois_inline = int(m.group(1))
            self._s.dois_resolved = self._s.dois_inline
        self._bar.update(progress=self._s.total_refs)
        self._set_phase("Phase 1 complete \u2014 skeletons collected")
        self._set_counter(
            f"{self._s.total_refs} refs | {self._s.dois_inline} DOIs from links"
        )

    def _handle_doi_resolved(self, msg: str) -> None:
        self._s.dois_resolved += 1
        frac = _parse_frac(msg)
        if frac:
            self._bar.update(progress=self._s.total_refs + frac[0])
        self._set_counter(f"{self._s.dois_resolved}/{self._s.total_refs} DOIs resolved")

    def _handle_doi_failed(self, msg: str) -> None:
        frac = _parse_frac(msg)
        if frac:
            self._bar.update(progress=self._s.total_refs + frac[0])
        self._set_counter(f"{self._s.dois_resolved}/{self._s.total_refs} DOIs resolved")

    def _handle_phase2_done(self) -> None:
        self._bar.update(progress=self._s.total_refs * 2)
        self._set_phase("Phase 2 complete")

    def _handle_crossref(self, msg: str) -> None:
        frac = _parse_frac(msg)
        if frac:
            bar_total = self._s.total_refs * 2 + frac[1]
            self._bar.update(total=bar_total, progress=self._s.total_refs * 2 + frac[0])
            self._set_counter(f"Crossref: {frac[0]}/{frac[1]} enriched")

    def _handle_pdf_progress(self, msg: str) -> None:
        frac = _parse_frac(msg)
        if frac:
            self._set_counter(f"PDF: {frac[0]}/{frac[1]} attempted")

    def _handle_api(self, msg: str) -> None:
        self._set_phase("Querying Semantic Scholar API\u2026")
        frac = _parse_frac(msg)
        if frac:
            self._bar.update(total=frac[1], progress=frac[0])
            self._set_counter(f"{frac[0]}/{frac[1]} entries processed")
        m2 = re.search(r"(\d+) references", msg)
        if m2:
            n = int(m2.group(1))
            self._bar.update(total=n, progress=n)
            self._set_counter(f"\u2713 {n} references with DOIs")


# ── Fetch Progress Modal ────────────────────────────────────


class FetchProgressModal(ModalScreen[None]):
    """Shows a log + progress bar while fetching references."""

    BINDINGS = [Binding("escape", "close_modal", "Close")]

    def __init__(self, survey: Survey, bib_path: Path, **kw: Any) -> None:
        super().__init__(**kw)
        self._survey = survey
        self._bib_path = bib_path

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label(
                f"Fetching references for: {self._survey.name or self._survey.source}",
                id="modal-title",
            )
            yield Label("Phase: starting\u2026", id="phase-label")
            yield ProgressBar(total=100, id="fetch-progress")
            yield Label("", id="counter-label")
            yield Log(id="fetch-log", max_lines=500)
            yield Button("Close", id="btn-close", disabled=True)

    def on_mount(self) -> None:
        self._run_fetch()

    def _set_phase(self, text: str) -> None:
        try:
            self.query_one("#phase-label", Label).update(text)
        except Exception:
            pass

    def _set_counter(self, text: str) -> None:
        try:
            self.query_one("#counter-label", Label).update(text)
        except Exception:
            pass

    @work(exclusive=True)
    async def _run_fetch(self) -> None:
        log = self.query_one("#fetch-log", Log)
        bar = self.query_one("#fetch-progress", ProgressBar)
        btn = self.query_one("#btn-close", Button)

        state = _ProgressState()
        dispatcher = _ProgressDispatcher(state, log, bar, self._set_phase, self._set_counter)

        try:
            refs = await fetch_references(
                self._survey.source,
                prefer_api=True,
                progress_callback=dispatcher,
            )
            self._merge_results(refs, log)
            # Rename survey to snake_case of its IEEE title
            await self._update_survey_name(log)
        except Exception as exc:
            log.write_line(f"\n\u2717 Error: {exc}")
            self._set_phase("\u2717 Failed")

        btn.disabled = False

    async def _update_survey_name(self, log: Log) -> None:
        """Fetch the survey's own title from IEEE and rename to snake_case."""
        source = self._survey.source
        if not source.startswith("http"):
            return
        try:
            title = await fetch_ieee_title(source)
            if title:
                new_name = _to_snake_name(title)
                if new_name:
                    self._survey.name = new_name
                    bib = storage.load(self._bib_path)
                    existing = bib.find_survey(self._survey.id)
                    if existing:
                        existing.name = new_name
                    storage.save(bib, self._bib_path)
                    log.write_line(f"  Survey renamed \u2192 {new_name}")
        except Exception as exc:
            log.write_line(f"  Could not fetch survey title: {exc}")

    def _merge_results(self, refs: list[Article], log: Log) -> None:
        """Merge fetched articles into the survey and save.

        Unresolved articles (no DOI) are kept with whatever metadata
        the scraper could extract so the user can search manually.
        """
        added, skipped_dup = 0, 0
        for art in refs:
            # De-dup key: real DOI or title-based fallback for unresolved
            key = art.doi.lower()
            already = any(
                a.doi.lower() == key for a in self._survey.articles
            )
            if already:
                skipped_dup += 1
            else:
                self._survey.articles.append(art)
                added += 1

        self._survey.total_references_expected = max(
            self._survey.total_references_expected, len(refs),
        )

        bib = storage.load(self._bib_path)
        existing = bib.find_survey(self._survey.id)
        if existing:
            existing.articles = self._survey.articles
            existing.total_references_expected = self._survey.total_references_expected
        storage.save(bib, self._bib_path)

        with_doi = sum(1 for r in refs if not r.doi.startswith("UNRESOLVED"))
        unresolved = len(refs) - with_doi
        self._set_phase("\u2713 Complete")
        self._set_counter(
            f"{added} new | {with_doi} DOIs | {unresolved} unresolved (kept) | "
            f"{skipped_dup} duplicates"
        )
        log.write_line(
            f"\n\u2713 Done \u2014 {added} new, {with_doi}/{len(refs)} with DOI, "
            f"{unresolved} unresolved (saved with title), {skipped_dup} duplicates"
        )

    @on(Button.Pressed, "#btn-close")
    def _on_close(self) -> None:
        self.dismiss(None)

    def action_close_modal(self) -> None:
        self.dismiss(None)


# ── Completeness Report Modal ───────────────────────────────


class CompletenessModal(ModalScreen[None]):
    """Shows per-survey completeness report."""

    BINDINGS = [Binding("escape", "close_modal", "Close")]

    def __init__(self, bib: Bibliography, **kw: Any) -> None:
        super().__init__(**kw)
        self._bib = bib

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label("Completeness Report", id="modal-title")
            yield VerticalScroll(id="report-scroll")
            yield Button("Close", id="btn-close")

    def on_mount(self) -> None:
        container = self.query_one("#report-scroll", VerticalScroll)
        if not self._bib.surveys:
            container.mount(Label("No surveys added yet."))
            return
        for survey in self._bib.surveys:
            container.mount(Static(self._format_survey(survey), markup=True))

    @staticmethod
    def _format_survey(s: Survey) -> str:
        pct = s.completeness * 100
        missing_pdf = sum(1 for a in s.articles if not a.local_path)
        incomplete = sum(1 for a in s.articles if not a.title or not a.authors)
        return (
            f"[bold]{s.name or s.id}[/bold]\n"
            f"  Source: {s.source}\n"
            f"  References: {s.fetched_count}/{s.total_references_expected} ({pct:.0f}%)\n"
            f"  Missing PDF path: {missing_pdf}\n"
            f"  Incomplete metadata: {incomplete}\n"
        )

    @on(Button.Pressed, "#btn-close")
    def _on_close(self) -> None:
        self.dismiss(None)

    def action_close_modal(self) -> None:
        self.dismiss(None)


# ── TXT Import Modal ────────────────────────────────────────


_INPUT_DIR = Path("input")


class ImportTxtModal(ModalScreen[list[dict[str, str]] | None]):
    """Let the user pick a .txt file from the input/ folder and import surveys."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label("Import Surveys from TXT", id="modal-title")
            yield Label("Select a file from the input/ folder:")
            yield VerticalScroll(id="report-scroll")
            yield Button("Cancel", id="btn-cancel")

    def on_mount(self) -> None:
        container = self.query_one("#report-scroll", VerticalScroll)
        _INPUT_DIR.mkdir(parents=True, exist_ok=True)
        txt_files = sorted(_INPUT_DIR.glob("*.txt"))
        if not txt_files:
            container.mount(
                Label("No .txt files found in input/ folder.\n"
                      "Create a text file with one URL or DOI per line.")
            )
            return
        for f in txt_files:
            btn_id = f"pick-file-{f.stem}"
            line_count = sum(
                1 for line in f.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.strip() and not line.strip().startswith("#")
            )
            container.mount(
                Button(
                    f"{f.name} ({line_count} entries)",
                    id=btn_id,
                    classes="survey-pick-btn",
                )
            )

    @on(Button.Pressed, ".survey-pick-btn")
    def _on_pick(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        stem = btn_id.removeprefix("pick-file-")
        path = _INPUT_DIR / f"{stem}.txt"
        if not path.exists():
            self.dismiss(None)
            return
        entries = _parse_txt_file(path)
        self.dismiss(entries)

    @on(Button.Pressed, "#btn-cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


def _parse_txt_file(path: Path) -> list[dict[str, str]]:
    """Parse a TXT file, classifying URLs and deduplicating by document ID.

    Each entry dict has keys: source, name, pdf_url, doc_id, type.
    """
    seen: dict[str, dict[str, str]] = {}  # doc_id → entry
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        info = _classify_ieee_url(stripped)
        doc_id = info["doc_id"]
        if doc_id in seen:
            # Merge: prefer a pdf_url if this duplicate provides one
            if info["pdf_url"] and not seen[doc_id].get("pdf_url"):
                seen[doc_id]["pdf_url"] = info["pdf_url"]
            continue
        seen[doc_id] = {
            "source": info["canonical_url"],
            "name": f"ieee_{doc_id}",
            "pdf_url": info["pdf_url"],
            "doc_id": doc_id,
            "type": info["type"],
        }
    return list(seen.values())


def _classify_ieee_url(url: str) -> dict[str, str]:
    """Classify an IEEE URL into document / stamp / direct-PDF."""
    parsed = urlparse(url)
    path = parsed.path

    # Direct PDF: /ielx7/.../XXXXXXXX.pdf?arnumber=...
    if path.endswith(".pdf"):
        qs = parse_qs(parsed.query)
        arnumber = qs.get("arnumber", [""])[0]
        if arnumber:
            return {
                "type": "pdf",
                "doc_id": arnumber,
                "canonical_url": f"https://ieeexplore.ieee.org/document/{arnumber}",
                "pdf_url": url,
            }

    # Stamp viewer: /stamp/stamp.jsp?...arnumber=...
    if "/stamp/" in path:
        qs = parse_qs(parsed.query)
        arnumber = qs.get("arnumber", [""])[0]
        if arnumber:
            return {
                "type": "pdf",
                "doc_id": arnumber,
                "canonical_url": f"https://ieeexplore.ieee.org/document/{arnumber}",
                "pdf_url": url,
            }

    # Standard document URL: /document/XXXXX
    if "/document/" in path:
        doc_id = path.rstrip("/").rsplit("/document/", 1)[-1]
        return {
            "type": "survey",
            "doc_id": doc_id,
            "canonical_url": url,
            "pdf_url": "",
        }

    # DOI string
    if url.startswith("10."):
        return {
            "type": "survey",
            "doc_id": url[:30],
            "canonical_url": url,
            "pdf_url": "",
        }

    # Unknown format — treat as survey
    return {
        "type": "survey",
        "doc_id": url[:40],
        "canonical_url": url,
        "pdf_url": "",
    }


def _to_snake_name(title: str) -> str:
    """Convert a paper title to a snake_case filename-safe string."""
    if not title:
        return ""
    cleaned = re.sub(r"\s+", " ", title).strip()
    cleaned = re.sub(r"[^\w\s-]", "", cleaned)
    return snakecase(cleaned)


# ── PDF Download Progress Modal ─────────────────────────────


class PDFDownloadModal(ModalScreen[None]):
    """Downloads PDFs for articles that have a pdf_url but no local_path."""

    BINDINGS = [Binding("escape", "close_modal", "Close")]

    def __init__(self, survey: Survey, bib_path: Path, **kw: Any) -> None:
        super().__init__(**kw)
        self._survey = survey
        self._bib_path = bib_path

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label(
                f"Downloading PDFs — {self._survey.name or self._survey.source}",
                id="modal-title",
            )
            yield Label("Starting…", id="phase-label")
            yield ProgressBar(total=100, id="fetch-progress")
            yield Label("", id="counter-label")
            yield Log(id="fetch-log", max_lines=300)
            yield Button("Close", id="btn-close", disabled=True)

    def on_mount(self) -> None:
        self._run_download()

    def _set_phase(self, text: str) -> None:
        try:
            self.query_one("#phase-label", Label).update(text)
        except Exception:
            pass

    def _set_counter(self, text: str) -> None:
        try:
            self.query_one("#counter-label", Label).update(text)
        except Exception:
            pass

    @work(exclusive=True)
    async def _run_download(self) -> None:
        log = self.query_one("#fetch-log", Log)
        bar = self.query_one("#fetch-progress", ProgressBar)
        btn = self.query_one("#btn-close", Button)

        to_dl = [
            a for a in self._survey.articles
            if a.pdf_url and not a.local_path
        ]

        if not to_dl:
            log.write_line("No articles with downloadable PDF URLs (without local copy).")
            self._set_phase("Nothing to download")
            btn.disabled = False
            return

        bar.update(total=len(to_dl), progress=0)
        self._set_phase(f"Downloading {len(to_dl)} PDFs…")

        pdf_dir = Path("bibliography/pdfs")
        pdf_dir.mkdir(parents=True, exist_ok=True)

        downloaded = 0
        async with httpx.AsyncClient(
            timeout=60,
            follow_redirects=True,
            headers={"User-Agent": "BibManager/1.0 (mailto:student@example.com)"},
        ) as client:
            for i, art in enumerate(to_dl, start=1):
                title_short = (art.title or art.doi)[:50]
                try:
                    resp = await client.get(art.pdf_url)
                    if resp.status_code == 200:
                        ct = (resp.headers.get("content-type") or "").lower()
                        ext = ".pdf" if "pdf" in ct else ".bin"
                        from .scraper import _safe_filename
                        dest = pdf_dir / f"{_safe_filename(art.doi)}{ext}"
                        dest.write_bytes(resp.content)
                        art.local_path = str(dest)
                        downloaded += 1
                        log.write_line(f"  ✓ [{i}/{len(to_dl)}] {title_short}")
                    else:
                        log.write_line(
                            f"  ✗ [{i}/{len(to_dl)}] {title_short} "
                            f"— HTTP {resp.status_code}"
                        )
                except Exception as exc:
                    log.write_line(f"  ✗ [{i}/{len(to_dl)}] {title_short} — {exc}")

                bar.update(progress=i)
                self._set_counter(f"{downloaded}/{i} downloaded")
                await asyncio.sleep(0.1)

        # Save updated local_path values
        bib = storage.load(self._bib_path)
        existing = bib.find_survey(self._survey.id)
        if existing:
            existing.articles = self._survey.articles
        storage.save(bib, self._bib_path)

        self._set_phase(f"✓ Downloaded {downloaded}/{len(to_dl)} PDFs")
        log.write_line(f"\n✓ Done — {downloaded}/{len(to_dl)} PDFs saved to {pdf_dir}")
        btn.disabled = False

    @on(Button.Pressed, "#btn-close")
    def _on_close(self) -> None:
        self.dismiss(None)

    def action_close_modal(self) -> None:
        self.dismiss(None)


# ── Survey Picker Modal (for fetch / view) ───────────────────


class SurveyPickerModal(ModalScreen[str | None]):
    """Let the user pick which survey to operate on."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, surveys: list[Survey], **kw: Any) -> None:
        super().__init__(**kw)
        self._surveys = surveys

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label("Select a Survey", id="modal-title")
            for s in self._surveys:
                yield Button(s.name or s.source, id=f"pick-{s.id}", classes="survey-pick-btn")
            yield Button("Cancel", id="btn-cancel")

    @on(Button.Pressed, ".survey-pick-btn")
    def _on_pick(self, event: Button.Pressed) -> None:
        sid = event.button.id
        if sid and sid.startswith("pick-"):
            self.dismiss(sid.removeprefix("pick-"))

    @on(Button.Pressed, "#btn-cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Article List Screen (drill-down) ─────────────────────────


class ArticleListScreen(ModalScreen[None]):
    """Shows all articles for a given survey in a DataTable."""

    BINDINGS = [Binding("escape", "close_screen", "Back")]

    def __init__(self, survey: Survey, **kw: Any) -> None:
        super().__init__(**kw)
        self._survey = survey

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label(
                f"Articles \u2014 {self._survey.name or self._survey.source}",
                id="modal-title",
            )
            yield Label(f"{len(self._survey.articles)} articles", id="article-count-label")
            yield DataTable(id="article-table")
            yield Button("Back [Esc]", id="btn-close")

    def on_mount(self) -> None:
        table = self.query_one("#article-table", DataTable)
        table.add_columns("#", "DOI", "Title", "Authors", "Year", "PDF")
        for i, art in enumerate(self._survey.articles, start=1):
            doi_display = (
                "⚠ no DOI" if art.doi.startswith("UNRESOLVED") else
                _truncate(art.doi, 30)
            )
            pdf_status = _pdf_status_icon(art)
            table.add_row(
                str(i),
                doi_display,
                _truncate(art.title, 55),
                _format_authors(art.authors),
                str(art.year or "—"),
                pdf_status,
            )

    @on(Button.Pressed, "#btn-close")
    def _on_close(self) -> None:
        self.dismiss(None)

    def action_close_screen(self) -> None:
        self.dismiss(None)


def _truncate(text: str, limit: int) -> str:
    return text[:limit] + ("\u2026" if len(text) > limit else "")


def _format_authors(authors: list[str]) -> str:
    result = ", ".join(authors[:3])
    if len(authors) > 3:
        result += " et al."
    return result


def _pdf_status_icon(art: Article) -> str:
    """Return a single-char icon for the article's PDF state."""
    if art.local_path:
        return "✓"
    if art.pdf_url:
        return "⬇"
    return "—"


# ── Main App ─────────────────────────────────────────────────


class BibliographyApp(App[None]):
    """TUI bibliography manager for TCC research."""

    TITLE = "Bibliography Manager"
    SUB_TITLE = "TCC Research Assistant"

    CSS = """
    Screen {
        overflow-y: auto;
    }

    #dashboard {
        height: 1fr;
        min-height: 12;
        padding: 1 2;
    }

    #stats-bar {
        height: 5;
        width: 100%;
        padding: 0 1;
    }

    StatsCard {
        width: 1fr;
        height: 5;
        border: solid $primary;
        padding: 0 1;
        content-align: center middle;
    }

    .stats-title {
        text-style: bold;
        color: $text-muted;
    }
    .stats-value {
        text-style: bold;
        color: $text;
        text-align: center;
    }

    #survey-table {
        height: 1fr;
        min-height: 6;
        margin-top: 1;
    }

    #button-bar {
        dock: bottom;
        height: auto;
        max-height: 10;
        margin-bottom: 1;
        padding: 0 1;
        layout: grid;
        grid-size: 5 3;
        grid-gutter: 0 1;
    }

    #button-bar Button {
        width: 100%;
        height: 3;
        min-width: 14;
    }

    /* ── modals ──────────────────────────────── */
    #modal-dialog {
        width: 80%;
        max-width: 100;
        max-height: 90%;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }

    #modal-title {
        text-style: bold;
        margin-bottom: 1;
    }

    #modal-buttons {
        margin-top: 1;
        height: 3;
    }

    .survey-pick-btn {
        width: 100%;
        margin-bottom: 1;
    }

    #fetch-log {
        height: 15;
        border: solid $primary;
        margin: 1 0;
    }

    #report-scroll {
        height: 20;
        border: solid $primary;
        margin: 1 0;
    }

    #article-table {
        height: 1fr;
        margin: 1 0;
    }

    #article-count-label {
        color: $text-muted;
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("f", "fetch", "Fetch Meta"),
        Binding("d", "download_pdfs", "Download PDFs"),
        Binding("a", "add_survey", "Add Survey"),
        Binding("i", "import_txt", "Import TXT"),
        Binding("v", "view_articles", "View Articles"),
        Binding("c", "check", "Completeness"),
        Binding("e", "edit_json", "Edit JSON"),
        Binding("n", "rename_surveys", "Rename Snake"),
        Binding("x", "clear_surveys", "Clear All"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, bib_path: str | Path | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.bib_path = storage.resolve_path(bib_path)
        self.bib = storage.load(self.bib_path)
        self._bib_mtime: float = (
            self.bib_path.stat().st_mtime if self.bib_path.exists() else 0.0
        )

    # ── compose ──────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="dashboard"):
            with Horizontal(id="stats-bar"):
                yield StatsCard("Surveys", id="stat-surveys")
                yield StatsCard("Unique Articles", id="stat-articles")
                yield StatsCard("Completeness", id="stat-completeness")
                yield StatsCard("With PDF", id="stat-pdfs")
            yield DataTable(id="survey-table")
        with Horizontal(id="button-bar"):
            yield Button("Fetch Meta", id="btn-fetch", variant="primary")
            yield Button("Download PDFs", id="btn-download")
            yield Button("Add Survey", id="btn-add-survey")
            yield Button("Import TXT", id="btn-import-txt")
            yield Button("View Articles", id="btn-view-articles")
            yield Button("Completeness", id="btn-check")
            yield Button("Edit JSON", id="btn-edit")
            yield Button("Rename Snake", id="btn-rename")
            yield Button("Refresh", id="btn-refresh")
            yield Button("Quit", id="btn-quit", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#survey-table", DataTable)
        table.add_columns("Survey", "Source", "Refs", "Expected", "%", "Added")
        self._refresh_dashboard()

    # ── dashboard refresh ────────────────────────────────────

    def _refresh_dashboard(self, *, force: bool = False) -> None:
        # Only re-parse the JSON when the file has actually changed on disk.
        current_mtime = (
            self.bib_path.stat().st_mtime if self.bib_path.exists() else 0.0
        )
        if force or current_mtime != self._bib_mtime:
            self.bib = storage.load(self.bib_path)
            self._bib_mtime = current_mtime

        bib = self.bib

        total_surveys = len(bib.surveys)
        total_articles = bib.total_unique_articles
        completeness = 0.0
        if bib.surveys:
            completeness = sum(s.completeness for s in bib.surveys) / len(bib.surveys)
        pdfs = sum(1 for a in bib.unique_articles.values() if a.local_path)

        self.query_one("#stat-surveys", StatsCard).update_value(str(total_surveys))
        self.query_one("#stat-articles", StatsCard).update_value(str(total_articles))
        self.query_one("#stat-completeness", StatsCard).update_value(f"{completeness * 100:.0f}%")
        self.query_one("#stat-pdfs", StatsCard).update_value(str(pdfs))

        table = self.query_one("#survey-table", DataTable)
        table.clear()
        for s in bib.surveys:
            table.add_row(
                s.name or s.id,
                _truncate(s.source, 50),
                str(s.fetched_count),
                str(s.total_references_expected),
                f"{s.completeness * 100:.0f}%",
                str(s.date_added),
            )

    # ── actions ──────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._refresh_dashboard(force=True)
        self.notify("Dashboard refreshed")

    def action_quit(self) -> None:
        self.exit()

    def action_edit_json(self) -> None:
        if not self.bib_path.exists():
            storage.save(self.bib, self.bib_path)
        _open_in_editor(self.bib_path)
        self.notify(f"Opened {self.bib_path.name} in editor")

    def action_add_survey(self) -> None:
        self.push_screen(AddSurveyModal(), callback=self._on_survey_added)

    def _on_survey_added(self, result: dict[str, str] | None) -> None:
        if result is None:
            return
        source = result["source"]
        name = result.get("name", "")
        survey_id = source
        if self.bib.find_survey(survey_id):
            self.notify("Survey already exists", severity="warning")
            return
        survey = Survey(id=survey_id, name=name, source=source, date_added=date.today())
        self.bib.surveys.append(survey)
        storage.save(self.bib, self.bib_path)
        self._refresh_dashboard()
        self.notify(f"Survey added: {name or source}")

    def action_fetch(self) -> None:
        self._pick_survey_then(self._start_fetch)

    def action_view_articles(self) -> None:
        self._pick_survey_then(self._show_articles)

    def action_check(self) -> None:
        self.push_screen(CompletenessModal(self.bib))

    def action_download_pdfs(self) -> None:
        self._pick_survey_then(self._start_download)

    def action_import_txt(self) -> None:
        self.push_screen(ImportTxtModal(), callback=self._on_txt_imported)

    def _on_txt_imported(self, entries: list[dict[str, str]] | None) -> None:
        """Add surveys from the parsed TXT file with deduplication."""
        if not entries:
            return
        added, skipped = 0, 0
        pdf_queue: list[dict[str, str]] = []
        for entry in entries:
            source = entry["source"]
            name = entry.get("name", "")
            if self.bib.find_survey(source):
                skipped += 1
                continue
            survey = Survey(
                id=source, name=name, source=source, date_added=date.today(),
            )
            self.bib.surveys.append(survey)
            added += 1
            if entry.get("pdf_url"):
                pdf_queue.append({
                    "survey_id": source,
                    "pdf_url": entry["pdf_url"],
                    "doc_id": entry.get("doc_id", ""),
                })
        if added:
            storage.save(self.bib, self.bib_path)
            self._refresh_dashboard()
        msg = f"Imported {added} surveys ({skipped} duplicates skipped)"
        if pdf_queue:
            msg += f", downloading {len(pdf_queue)} PDFs\u2026"
        self.notify(msg, severity="information" if added else "warning")
        if pdf_queue:
            self._download_import_pdfs(pdf_queue)

    @work(exclusive=True)
    async def _download_import_pdfs(self, entries: list[dict[str, str]]) -> None:
        """Download PDFs from direct/stamp URLs identified during TXT import."""
        from .scraper import _safe_filename

        pdf_dir = Path("bibliography/pdfs")
        pdf_dir.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        async with httpx.AsyncClient(
            timeout=60,
            follow_redirects=True,
            headers={"User-Agent": "BibManager/1.0 (mailto:student@example.com)"},
        ) as client:
            for entry in entries:
                try:
                    resp = await client.get(entry["pdf_url"])
                    if resp.status_code == 200:
                        ct = (resp.headers.get("content-type") or "").lower()
                        ext = ".pdf" if "pdf" in ct else ".bin"
                        dest = pdf_dir / f"survey_{_safe_filename(entry['doc_id'])}{ext}"
                        dest.write_bytes(resp.content)
                        survey = self.bib.find_survey(entry["survey_id"])
                        if survey:
                            survey.local_path = str(dest)
                        downloaded += 1
                except Exception:
                    pass
                await asyncio.sleep(0.5)
        if downloaded:
            storage.save(self.bib, self.bib_path)
            self._refresh_dashboard()
        self.notify(
            f"Downloaded {downloaded}/{len(entries)} survey PDFs",
            severity="information",
        )

    def action_rename_surveys(self) -> None:
        """Rename all surveys to snake_case based on their IEEE page titles."""
        self._do_rename_surveys()

    def action_clear_surveys(self) -> None:
        """Delete all surveys from the bibliography for fresh reimport."""
        if not self.bib.surveys:
            self.notify("No surveys to delete", severity="warning")
            return
        count = len(self.bib.surveys)
        self.bib.surveys.clear()
        storage.save(self.bib, self.bib_path)
        self._refresh_dashboard()
        self.notify(f"Deleted {count} surveys — ready for reimport", severity="information")

    @work(exclusive=True)
    async def _do_rename_surveys(self) -> None:
        renamed = 0
        for survey in self.bib.surveys:
            if not survey.source.startswith("http"):
                continue
            try:
                title = await fetch_ieee_title(survey.source)
                if title:
                    new_name = _to_snake_name(title)
                    if new_name and new_name != survey.name:
                        survey.name = new_name
                        renamed += 1
                await asyncio.sleep(_IEEE_DELAY_RENAME)
            except Exception:
                pass
        if renamed:
            storage.save(self.bib, self.bib_path)
            self._refresh_dashboard()
        self.notify(f"Renamed {renamed} surveys to snake_case", severity="information")

    # ── survey picking helpers ───────────────────────────────

    def _pick_survey_then(self, callback: Any) -> None:
        """Pick a survey (auto-selects if only one) then call *callback(survey_id)*."""
        if not self.bib.surveys:
            self.notify("No surveys \u2014 add one first", severity="warning")
            return
        if len(self.bib.surveys) == 1:
            callback(self.bib.surveys[0].id)
        else:
            self.push_screen(
                SurveyPickerModal(self.bib.surveys),
                callback=lambda sid: sid and callback(sid),
            )

    def _start_fetch(self, survey_id: str) -> None:
        survey = self.bib.find_survey(survey_id)
        if not survey:
            self.notify(_MSG_SURVEY_NOT_FOUND, severity="error")
            return
        self.push_screen(
            FetchProgressModal(survey, self.bib_path),
            callback=lambda _: self._refresh_dashboard(),
        )

    def _start_download(self, survey_id: str) -> None:
        survey = self.bib.find_survey(survey_id)
        if not survey:
            self.notify(_MSG_SURVEY_NOT_FOUND, severity="error")
            return
        downloadable = [a for a in survey.articles if a.pdf_url and not a.local_path]
        if not downloadable:
            self.notify("No downloadable PDFs for this survey", severity="warning")
            return
        self.push_screen(
            PDFDownloadModal(survey, self.bib_path),
            callback=lambda _: self._refresh_dashboard(),
        )

    def _show_articles(self, survey_id: str) -> None:
        survey = self.bib.find_survey(survey_id)
        if not survey:
            self.notify(_MSG_SURVEY_NOT_FOUND, severity="error")
            return
        if not survey.articles:
            self.notify("No articles fetched yet", severity="warning")
            return
        self.push_screen(ArticleListScreen(survey))

    # ── button handlers ──────────────────────────────────────

    @on(Button.Pressed, "#btn-fetch")
    def _btn_fetch(self) -> None:
        self.action_fetch()

    @on(Button.Pressed, "#btn-download")
    def _btn_download(self) -> None:
        self.action_download_pdfs()

    @on(Button.Pressed, "#btn-add-survey")
    def _btn_add(self) -> None:
        self.action_add_survey()

    @on(Button.Pressed, "#btn-import-txt")
    def _btn_import_txt(self) -> None:
        self.action_import_txt()

    @on(Button.Pressed, "#btn-view-articles")
    def _btn_view_articles(self) -> None:
        self.action_view_articles()

    @on(Button.Pressed, "#btn-check")
    def _btn_check(self) -> None:
        self.action_check()

    @on(Button.Pressed, "#btn-edit")
    def _btn_edit(self) -> None:
        self.action_edit_json()

    @on(Button.Pressed, "#btn-refresh")
    def _btn_refresh(self) -> None:
        self.action_refresh()

    @on(Button.Pressed, "#btn-rename")
    def _btn_rename(self) -> None:
        self.action_rename_surveys()

    @on(Button.Pressed, "#btn-quit")
    def _btn_quit(self) -> None:
        self.action_quit()
