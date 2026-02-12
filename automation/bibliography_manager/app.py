"""Bibliography Manager — Textual TUI application.

Launch with:  python -m automation.bibliography-manager
"""

from __future__ import annotations

import asyncio
import os
import platform
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
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

from .models import Article, Bibliography, Survey
from . import storage
from .scraper import fetch_references

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

    def __init__(self, title: str, value: str = "—", **kw: Any) -> None:
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
            yield Input(placeholder="https://ieeexplore.ieee.org/document/… or 10.1109/…", id="survey-source")
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
            yield Label(f"Fetching references for: {self._survey.name or self._survey.source}", id="modal-title")
            yield ProgressBar(total=100, id="fetch-progress")
            yield Log(id="fetch-log", max_lines=200)
            yield Button("Close", id="btn-close", disabled=True)

    def on_mount(self) -> None:
        self._run_fetch()

    @work(exclusive=True)
    async def _run_fetch(self) -> None:
        log = self.query_one("#fetch-log", Log)
        progress = self.query_one("#fetch-progress", ProgressBar)
        btn = self.query_one("#btn-close", Button)

        async def _progress(msg: str) -> None:
            log.write_line(msg)

        try:
            progress.update(progress=30)
            refs = await fetch_references(
                self._survey.source,
                prefer_api=True,
                progress_callback=_progress,
            )
            progress.update(progress=80)

            # Merge into survey
            added = 0
            for art in refs:
                if not self._survey.has_doi(art.doi):
                    self._survey.articles.append(art)
                    added += 1

            self._survey.total_references_expected = max(
                self._survey.total_references_expected,
                len(refs),
            )

            # Save
            bib = storage.load(self._bib_path)
            existing = bib.find_survey(self._survey.id)
            if existing:
                existing.articles = self._survey.articles
                existing.total_references_expected = self._survey.total_references_expected
            storage.save(bib, self._bib_path)

            progress.update(progress=100)
            log.write_line(f"\n✓ Done — {added} new articles added ({len(refs)} total references found)")

        except Exception as exc:
            log.write_line(f"\n✗ Error: {exc}")

        btn.disabled = False

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
        bib = self._bib

        if not bib.surveys:
            container.mount(Label("No surveys added yet."))
            return

        for survey in bib.surveys:
            pct = survey.completeness * 100
            fetched = survey.fetched_count
            expected = survey.total_references_expected
            missing_pdf = sum(1 for a in survey.articles if not a.local_path)
            incomplete_meta = sum(
                1 for a in survey.articles if not a.title or not a.authors
            )

            block = (
                f"[bold]{survey.name or survey.id}[/bold]\n"
                f"  Source: {survey.source}\n"
                f"  References: {fetched}/{expected} ({pct:.0f}%)\n"
                f"  Missing PDF path: {missing_pdf}\n"
                f"  Incomplete metadata: {incomplete_meta}\n"
            )
            container.mount(Static(block, markup=True))

    @on(Button.Pressed, "#btn-close")
    def _on_close(self) -> None:
        self.dismiss(None)

    def action_close_modal(self) -> None:
        self.dismiss(None)


# ── Survey Picker Modal (for fetch) ─────────────────────────


class SurveyPickerModal(ModalScreen[str | None]):
    """Let the user pick which survey to fetch references for."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, surveys: list[Survey], **kw: Any) -> None:
        super().__init__(**kw)
        self._surveys = surveys

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label("Select a Survey", id="modal-title")
            for s in self._surveys:
                yield Button(
                    s.name or s.source,
                    id=f"pick-{s.id}",
                    classes="survey-pick-btn",
                )
            yield Button("Cancel", id="btn-cancel")

    @on(Button.Pressed, ".survey-pick-btn")
    def _on_pick(self, event: Button.Pressed) -> None:
        survey_id = event.button.id
        if survey_id and survey_id.startswith("pick-"):
            self.dismiss(survey_id.removeprefix("pick-"))

    @on(Button.Pressed, "#btn-cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Main App ─────────────────────────────────────────────────


class BibliographyApp(App[None]):
    """TUI bibliography manager for TCC research."""

    TITLE = "Bibliography Manager"
    SUB_TITLE = "TCC Research Assistant"

    CSS = """
    /* ── layout ─────────────────────────────── */
    #dashboard {
        height: 1fr;
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
        margin-top: 1;
    }

    #button-bar {
        dock: bottom;
        height: 3;
        padding: 0 1;
    }

    #button-bar Button {
        margin: 0 1;
    }

    /* ── modals ──────────────────────────────── */
    #modal-dialog {
        width: 70;
        max-height: 80%;
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
    """

    BINDINGS = [
        Binding("f", "fetch", "Fetch Refs"),
        Binding("a", "add_survey", "Add Survey"),
        Binding("c", "check", "Completeness"),
        Binding("e", "edit_json", "Edit JSON"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, bib_path: str | Path | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.bib_path = storage.resolve_path(bib_path)
        self.bib = storage.load(self.bib_path)

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
            yield Button("Fetch Refs [F]", id="btn-fetch", variant="primary")
            yield Button("Add Survey [A]", id="btn-add-survey")
            yield Button("Completeness [C]", id="btn-check")
            yield Button("Edit JSON [E]", id="btn-edit")
            yield Button("Refresh [R]", id="btn-refresh")
            yield Button("Quit [Q]", id="btn-quit", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#survey-table", DataTable)
        table.add_columns("Survey", "Source", "Refs", "Expected", "Completeness", "Added")
        self._refresh_dashboard()

    # ── dashboard refresh ────────────────────────────────────

    def _refresh_dashboard(self) -> None:
        self.bib = storage.load(self.bib_path)
        bib = self.bib

        # Stats
        total_surveys = len(bib.surveys)
        total_articles = bib.total_unique_articles
        overall_completeness = 0.0
        if bib.surveys:
            overall_completeness = (
                sum(s.completeness for s in bib.surveys) / len(bib.surveys)
            )
        pdfs = sum(1 for a in bib.unique_articles.values() if a.local_path)

        self.query_one("#stat-surveys", StatsCard).update_value(str(total_surveys))
        self.query_one("#stat-articles", StatsCard).update_value(str(total_articles))
        self.query_one("#stat-completeness", StatsCard).update_value(f"{overall_completeness * 100:.0f}%")
        self.query_one("#stat-pdfs", StatsCard).update_value(str(pdfs))

        # Table
        table = self.query_one("#survey-table", DataTable)
        table.clear()
        for s in bib.surveys:
            pct = f"{s.completeness * 100:.0f}%"
            table.add_row(
                s.name or s.id,
                s.source[:50] + ("…" if len(s.source) > 50 else ""),
                str(s.fetched_count),
                str(s.total_references_expected),
                pct,
                str(s.date_added),
            )

    # ── actions ──────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._refresh_dashboard()
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
        survey_id = source  # use DOI/URL as id
        if self.bib.find_survey(survey_id):
            self.notify("Survey already exists", severity="warning")
            return
        survey = Survey(id=survey_id, name=name, source=source, date_added=date.today())
        self.bib.surveys.append(survey)
        storage.save(self.bib, self.bib_path)
        self._refresh_dashboard()
        self.notify(f"Survey added: {name or source}")

    def action_fetch(self) -> None:
        if not self.bib.surveys:
            self.notify("No surveys — add one first", severity="warning")
            return
        if len(self.bib.surveys) == 1:
            self._start_fetch(self.bib.surveys[0].id)
        else:
            self.push_screen(
                SurveyPickerModal(self.bib.surveys),
                callback=self._on_survey_picked,
            )

    def _on_survey_picked(self, survey_id: str | None) -> None:
        if survey_id:
            self._start_fetch(survey_id)

    def _start_fetch(self, survey_id: str) -> None:
        survey = self.bib.find_survey(survey_id)
        if not survey:
            self.notify("Survey not found", severity="error")
            return
        self.push_screen(
            FetchProgressModal(survey, self.bib_path),
            callback=lambda _: self._refresh_dashboard(),
        )

    def action_check(self) -> None:
        self.push_screen(CompletenessModal(self.bib))

    # ── button handlers ──────────────────────────────────────

    @on(Button.Pressed, "#btn-fetch")
    def _btn_fetch(self) -> None:
        self.action_fetch()

    @on(Button.Pressed, "#btn-add-survey")
    def _btn_add(self) -> None:
        self.action_add_survey()

    @on(Button.Pressed, "#btn-check")
    def _btn_check(self) -> None:
        self.action_check()

    @on(Button.Pressed, "#btn-edit")
    def _btn_edit(self) -> None:
        self.action_edit_json()

    @on(Button.Pressed, "#btn-refresh")
    def _btn_refresh(self) -> None:
        self.action_refresh()

    @on(Button.Pressed, "#btn-quit")
    def _btn_quit(self) -> None:
        self.action_quit()
