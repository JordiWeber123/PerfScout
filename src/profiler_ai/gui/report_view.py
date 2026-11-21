"""Report and experiment readers for the desktop investigation workspace."""

import json
from collections import Counter
from functools import partial
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from urllib.parse import unquote

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

_STYLE = """
QWidget#readingPage, QWidget#readingSheet, QScrollArea#readingScroll {
    background: #1a1a16;
    border: 0;
    color: #e4e3df;
}
QLabel { color: #e4e3df; background: transparent; }
QLabel[role="title"] { font-size: 24px; font-weight: 650; color: #f4f3ef; }
QLabel[role="section"] { font-size: 17px; font-weight: 650; color: #f4f3ef; }
QLabel[role="heading"] { font-size: 13px; font-weight: 650; color: #efeeea; }
QLabel[role="body"] { font-size: 15px; }
QLabel[role="muted"] { font-size: 12px; color: #b1b0ac; }
QLabel[role="accent"] { font-size: 12px; color: #ff921f; font-weight: 600; }
QLabel[role="warning"] { font-size: 12px; color: #e5b991; font-weight: 600; }
QFrame#readingCard { background: transparent; border: 0; border-top: 1px solid #3c3932; }
QFrame#readingRule { background: #393834; border: 0; }
QPushButton[readingAction="true"] {
    background: #302f2b;
    border: 1px solid #44433f;
    border-radius: 5px;
    color: #58d5ed;
    padding: 4px 8px;
    font-size: 12px;
    text-align: left;
}
QPushButton[readingAction="true"]:hover { background: #3f3e3a; }
QPushButton[readingAction="true"]:focus { border: 1px solid #ff921f; }
QPushButton[readingAction="true"]:pressed { background: #504f4b; }
QPushButton[readingAction="true"][compact="true"] { color: #58d5ed; background: transparent; border: 0; padding: 2px 4px; font-size: 12px; }
QPushButton[compact="true"]:focus, QPushButton[compact="true"]:hover { background: #263a3c; }
QLabel[role="metric"] { color: #f5e5cc; font-family: Consolas, monospace; font-size: 18px; }
QLabel[role="delta"] { color: #ffad53; background: #332619; border: 1px solid #96602e; border-radius: 4px; font-size: 18px; padding: 1px 6px; }
QPushButton[tone="regression"] { color: #f0a98f; }
QPushButton[tone="weak"] { color: #e5b991; }
QPlainTextEdit#experimentCommand {
    background: #1c1b17;
    color: #e5e4e0;
    border: 1px solid #3b3a36;
    border-radius: 5px;
    padding: 4px;
    font-family: Consolas, monospace;
    font-size: 12px;
}
"""

_STATUSES = {
    "agent_failed": "Analysis failed",
    "measurements_only": "Measurements available",
    "supported": "Supported conclusion",
    "inconclusive": "Inconclusive",
}

_EXPERIMENT_FIELDS = (
    ("workload", "Workload"),
    ("collection_settings", "Collection settings"),
    ("controls", "Controls"),
    ("budget", "Budget"),
    ("permission", "Permission"),
    ("interpretation", "Interpretation"),
)


def _label(text: str, role: str = "body", *, selectable: bool = False) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setProperty("role", role)
    label.setWordWrap(True)
    if selectable:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _action(button: QPushButton) -> QPushButton:
    button.setProperty("readingAction", True)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def _panel() -> tuple[QFrame, QVBoxLayout]:
    panel = QFrame()
    panel.setObjectName("readingCard")
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(0, 8, 0, 6)
    layout.setSpacing(7)
    return panel, layout


def _basename(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).name


def _portable_path(path: str) -> PurePath:
    windows = PureWindowsPath(path)
    return windows if windows.drive else PurePosixPath(path)


def _run_roots(data: dict) -> dict[str, PurePath]:
    roots = {
        side: _portable_path(path)
        for side, path in ((data.get("provenance") or {}).get("runs") or {}).items()
        if isinstance(path, str) and path
    }
    legacy_manifests = [
        _portable_path(item["path"]).parent
        for item in data.get("citations", {}).values()
        if item.get("locator") == "manifest" and item.get("path")
    ]
    if len(legacy_manifests) == 2:
        for side, path in zip(("baseline", "candidate"), legacy_manifests, strict=True):
            roots.setdefault(side, path)
    return roots


def _artifact_side(item: dict, roots: dict[str, PurePath]) -> str:
    if item.get("side") in {"baseline", "candidate"}:
        return item["side"]
    paths = [_portable_path(path) for path in item.get("paths") or [item.get("path")] if path]
    sides = [
        side for side, root in roots.items() if any(path.is_relative_to(root) for path in paths)
    ]
    return sides[0] if len(sides) == 1 else ""


def _citation_label(item: dict, revisions: dict) -> tuple[str, str, str]:
    """Return a grouping key, source label and location label for a citation."""
    locator = item.get("locator", "")
    if item.get("kind") == "git":
        try:
            git = json.loads(locator)
        except ValueError:
            git = {}
        if not isinstance(git, dict):
            git = {}
        revision = item.get("revision", "")
        side = next((side.title() for side, sha in revisions.items() if sha == revision), "Git")
        if "diff_from" in git:
            paths = git.get("paths", [])
            names = ", ".join(_basename(path) for path in paths)
            return f"diff/{paths}", names or "Git changes", "Committed diff"
        if path := git.get("path"):
            start, end = git.get("start"), git.get("end")
            location = f"Lines {start}–{end}" if end != start else f"Line {start}"
            return f"git/{revision}/{path}", f"{side} · {_basename(path)}", location
        return f"git/{revision}", side, "Committed source"

    path = item.get("path", "")
    title = _basename(path) or "Artifact"
    if locator.startswith("line:"):
        location = f"Line {locator.removeprefix('line:')}"
    elif locator.startswith(("pprof:sample/", "pprof:location/")):
        kind, _, number = locator.removeprefix("pprof:").partition("/")
        location = f"{kind.title()} {number}"
    elif locator in {"", "pprof:profile", "papi:#"}:
        location = "Profile" if locator else "Open"
    elif locator.startswith("papi:#/"):
        parts = [part.replace("~1", "/").replace("~0", "~") for part in unquote(locator).split("/")]
        location = next(
            (part for part in reversed(parts) if part.startswith(("PAPI_", "perf::"))),
            parts[-1] if not parts[-1].isdigit() else "Reading",
        )
    else:
        location = locator.replace("_", " ").capitalize()
    return f"artifact/{path}", title, location


def _line_number(location: str) -> str:
    number = location.removeprefix("Line ")
    return number if number != location and number.isdecimal() else ""


def _chip_text(location: str) -> str:
    if number := _line_number(location):
        return number
    return location if len(location) <= 22 else location[:21] + "…"


class _PreviewButton(QPushButton):
    """A link-like button with pointer and keyboard previews."""

    previewRequested = Signal(QPoint)
    previewDismissed = Signal()

    def __init__(self, title: str, accessible_name: str):
        super().__init__(title)
        _action(self)
        self.setAccessibleName(accessible_name)
        self._previewing = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._preview)
        self.clicked.connect(self._dismiss)

    def _preview(self):
        self._previewing = True
        self.previewRequested.emit(self.mapToGlobal(QPoint(self.width() // 2, self.height())))

    def _dismiss(self):
        self._timer.stop()
        if self._previewing:
            self._previewing = False
            self.previewDismissed.emit()

    def enterEvent(self, event):
        self._timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._dismiss()
        super().leaveEvent(event)

    def focusInEvent(self, event):
        if event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason):
            self._timer.start()
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self._dismiss()
        super().focusOutEvent(event)

    def hideEvent(self, event):
        self._dismiss()
        super().hideEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            self.click()
            event.accept()
        else:
            super().keyPressEvent(event)


class _ReadingView(QWidget):
    previewRequested = Signal(str, QPoint)
    previewDismissed = Signal()
    evidenceRequested = Signal(str)
    measurementRequested = Signal(int)
    measurementPreviewRequested = Signal(int, QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("readingPage")
        self.setStyleSheet(_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea()
        self._scroll.setObjectName("readingScroll")
        self._scroll.setWidgetResizable(True)
        layout.addWidget(self._scroll)
        self.setFocusProxy(self._scroll)
        self._report = None
        self._sections: dict[str, QWidget] = {}
        self._deferred_scroll = QTimer(self)
        self._deferred_scroll.setSingleShot(True)
        self._deferred_scroll.timeout.connect(self._restore_scroll)
        self._scroll_target: int | str | None = None
        self.set_report({})

    def set_report(self, data: dict):
        if data == self._report:
            return
        position = self._scroll.verticalScrollBar().value() if self._same_revisions(data) else 0
        self._report = data
        self._sections.clear()
        sheet = QWidget()
        sheet.setObjectName("readingSheet")
        sheet_layout = QHBoxLayout(sheet)
        sheet_layout.setContentsMargins(0, 0, 0, 0)
        content = QWidget()
        content.setMaximumWidth(980)
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        sheet_layout.addStretch()
        sheet_layout.addWidget(content, 12)
        sheet_layout.addStretch()
        body = QVBoxLayout(content)
        body.setContentsMargins(14, 20, 14, 24)
        body.setSpacing(10)
        self._render(body, data)
        body.addStretch(1)
        self._scroll.setWidget(sheet)
        self._scroll_target = position
        self._deferred_scroll.start(0)

    def _same_revisions(self, data: dict) -> bool:
        return bool(
            self._report and data and self._report.get("revisions") == data.get("revisions")
        )

    def _restore_scroll(self):
        if not self.isVisible():
            return
        target, self._scroll_target = self._scroll_target, None
        if isinstance(target, str):
            widget = self._sections.get(target)
            if widget is None:
                return
            target = widget.mapTo(self._scroll.widget(), QPoint()).y() - 12
        if target is not None:
            self._scroll.verticalScrollBar().setValue(target)

    def showEvent(self, event):
        super().showEvent(event)
        if self._scroll_target is not None:
            self._deferred_scroll.start(0)

    def _render(self, body: QVBoxLayout, data: dict):
        raise NotImplementedError

    def _section(self, body: QVBoxLayout, key: str, title: str):
        body.addSpacing(8)
        heading = _label(title, "section")
        self._sections[key] = heading
        body.addWidget(heading)

    def _wire_evidence(self, location: str, context: str, reference: str) -> _PreviewButton:
        link = _PreviewButton(_chip_text(location), f"{context} · {location}")
        link.setProperty("compact", True)
        link.previewRequested.connect(partial(self.previewRequested.emit, reference))
        link.previewDismissed.connect(self.previewDismissed)
        link.clicked.connect(partial(self.evidenceRequested.emit, reference))
        return link


class ReportView(_ReadingView):
    experimentsRequested = Signal()

    def jump(self, section: str):
        if section == "report" or section in self._sections:
            self._scroll_target = 0 if section == "report" else section
            self._deferred_scroll.start(0)

    def _render(self, body: QVBoxLayout, data: dict):
        self._artifact_roots = _run_roots(data)
        comparisons = data.get("benchmarks", {}).get("comparisons", [])
        timing = next(
            (
                row
                for row in comparisons
                if row["status"] == "comparable"
                and row["unit"] == "ns/op"
                and row.get("relative_change_percent") is not None
            ),
            None,
        )
        if timing:
            name = timing["benchmark"].removeprefix("Benchmark")
            change = timing["relative_change_percent"]
            direction = "fell" if change < 0 else "rose"
            headline = (
                f"{name} time {direction} {abs(change):.3g}%"
                if change
                else f"{name} timing held steady"
            )
            body.addWidget(_label(headline, "title"))
            strip = QHBoxLayout()
            for heading, value in (
                ("Baseline", f"{timing['baseline']['median']:,} ns/op"),
                ("Candidate", f"{timing['candidate']['median']:,} ns/op"),
                ("Change", f"{timing['relative_change_percent']:+.3g}%"),
            ):
                column = QVBoxLayout()
                column.addWidget(_label(heading.upper(), "muted"))
                column.addWidget(
                    _label(value, "delta" if heading == "Change" else "metric"),
                    alignment=Qt.AlignmentFlag.AlignLeft,
                )
                strip.addLayout(column)
            body.addLayout(strip)
        else:
            body.addWidget(_label("Performance investigation", "title"))
        if not data:
            body.addWidget(_label("Open an investigation to read its report.", "muted"))
            return
        findings = data.get("findings") or {}
        status = findings.get("conclusion") or data.get("status", "")
        status_role = "warning" if status in {"agent_failed", "inconclusive"} else "accent"
        body.addWidget(_label(_STATUSES.get(status, "Investigation complete"), status_role))
        revisions = data.get("revisions", {})
        if revisions:
            revision_line = _label(
                "   →   ".join(f"{side.title()} {sha[:8]}" for side, sha in revisions.items()),
                "muted",
            )
            revision_line.setToolTip(
                "\n".join(f"{side.title()} {sha}" for side, sha in revisions.items())
            )
            body.addWidget(revision_line)

        if findings:
            self._section(body, "findings", "Findings")
            self._findings(body, findings, data)
        self._section(body, "measurements", "Measurements")
        benchmarks = data.get("benchmarks", {})
        self._measurements(body, benchmarks.get("comparisons", []))
        if not findings:
            self._section(body, "findings", "Findings")
            body.addWidget(_label(data.get("error") or "Analysis has no findings.", "muted"))
        self._section(body, "gaps", "Measurement gaps")
        self._gaps(body, data)

    def _findings(self, body: QVBoxLayout, findings: dict, data: dict):
        for key, title in (("observations", "Observations"), ("hypotheses", "Hypotheses")):
            if claims := findings.get(key):
                body.addWidget(_label(title, "heading"))
                for claim in claims:
                    panel, content = _panel()
                    content.addWidget(_label(claim["text"], selectable=True))
                    self._citations(content, claim.get("evidence", []), data)
                    body.addWidget(panel)

        for key, title in (
            ("alternatives", "Alternative explanations"),
            ("missing_evidence", "Missing evidence"),
            ("confidence_limits", "Confidence limits"),
        ):
            if items := findings.get(key):
                body.addWidget(_label(title, "heading"))
                for item in [items] if isinstance(items, str) else items:
                    body.addWidget(_label(item, selectable=True))
        if findings.get("experiments"):
            experiments = _action(QPushButton("View proposed experiments  →"))
            experiments.clicked.connect(self.experimentsRequested)
            body.addWidget(experiments, alignment=Qt.AlignmentFlag.AlignLeft)

    def _citations(self, layout: QVBoxLayout, references: list[str], data: dict):
        groups = {}
        citations = data.get("citations", {})
        for index, reference in enumerate(dict.fromkeys(references), 1):
            if item := citations.get(reference):
                key, group, location = _citation_label(item, data.get("revisions", {}))
                if key.startswith("artifact/") and (
                    side := _artifact_side(item, self._artifact_roots)
                ):
                    group = f"{side.title()} · {group}"
            else:
                key, group, location = "unlisted", "Evidence", f"Reference {index}"
            groups.setdefault(key, (group, []))[1].append((location, reference))
        names = Counter(group for group, _ in groups.values())
        citations_layout = QVBoxLayout()
        citations_layout.setSpacing(4)
        for key, (group, links) in groups.items():
            if names[group] > 1 and key.startswith("artifact/"):
                path = key.removeprefix("artifact/").replace("\\", "/")
                parent, _, _ = path.rpartition("/")
                group = f"{group} · {PurePosixPath(parent).name}"
            self._citation_group(citations_layout, group, links)
        if groups:
            layout.addLayout(citations_layout)

    def _citation_group(self, layout: QVBoxLayout, group: str, links: list[tuple[str, str]]):
        row = QHBoxLayout()
        row.setSpacing(5)
        title = _label(group, "muted")
        title.setWordWrap(False)
        title.setAccessibleName(group)
        title.setMaximumWidth(158)
        title.setText(title.fontMetrics().elidedText(group, Qt.TextElideMode.ElideMiddle, 158))
        row.addWidget(title)
        primary = 4 if all(_line_number(location) for location, _ in links) else 2
        if sum(len(_chip_text(location)) for location, _ in links[:primary]) > 25:
            primary = 1
        for location, reference in links[:primary]:
            row.addWidget(self._wire_evidence(location, group, reference))
        layout.addLayout(row)
        if rest := links[primary:]:
            count = len(rest)
            toggle = _action(QPushButton(f"+{count}"))
            toggle.setProperty("compact", True)
            toggle.setCheckable(True)
            toggle.setAccessibleName(f"Show {count} more citations for {group}")
            row.addWidget(toggle)
            details = QWidget()
            extra = QVBoxLayout(details)
            extra.setContentsMargins(0, 0, 0, 0)
            extra.setSpacing(4)
            details.hide()
            layout.addWidget(details)
            toggle.toggled.connect(
                partial(self._toggle_citations, group, rest, toggle, details, extra)
            )
        row.addStretch(1)

    def _toggle_citations(
        self,
        group: str,
        links: list[tuple[str, str]],
        toggle: QPushButton,
        details: QWidget,
        extra: QVBoxLayout,
        expanded: bool,
    ):
        if expanded and not extra.count():
            row = QHBoxLayout()
            row.setSpacing(5)
            extra.addLayout(row)
            length = 0
            for location, reference in links:
                width = len(_chip_text(location)) + 4
                if length + width > 38:
                    row.addStretch(1)
                    row = QHBoxLayout()
                    row.setSpacing(5)
                    extra.addLayout(row)
                    length = 0
                row.addWidget(self._wire_evidence(location, group, reference))
                length += width
            row.addStretch(1)
        details.setVisible(expanded)
        toggle.setText("Less" if expanded else f"+{len(links)}")
        action = "Hide extra citations" if expanded else f"Show {len(links)} more citations"
        toggle.setAccessibleName(f"{action} for {group}")

    def _measurements(self, body: QVBoxLayout, rows: list[dict]):
        if not rows:
            body.addWidget(_label("No benchmark comparisons available.", "muted"))
            return
        panel, layout = _panel()
        table = QGridLayout()
        table.setHorizontalSpacing(10)
        table.setVerticalSpacing(6)
        table.setColumnStretch(0, 4)
        for column, title in enumerate(("Benchmark", "Baseline", "Candidate", "Change")):
            table.addWidget(_label(title, "muted"), 0, column)
            if column:
                table.setColumnStretch(column, 2)
        for index, metric in enumerate(rows):
            row = 1 + index * 2
            rule = QFrame()
            rule.setObjectName("readingRule")
            rule.setFixedHeight(1)
            table.addWidget(rule, row, 0, 1, 4)
            name = metric["benchmark"]
            unit = metric["unit"]
            table.addLayout(self._metric_cell(name, unit), row + 1, 0)
            for column, side in enumerate(("baseline", "candidate"), 1):
                summary = metric[side]
                median = summary["median"]
                count = summary["sample_count"]
                count_label = "sample" if count == 1 else "samples"
                table.addLayout(
                    self._metric_cell(
                        "—" if median is None else f"{median:,}", f"{count} {count_label}"
                    ),
                    row + 1,
                    column,
                )
            delta = metric.get("relative_change_percent")
            change = "—" if delta is None else (f"{delta:+.3g}%" if delta else "0%")
            status = metric["status"]
            spoken_change = change if delta is not None else "Change unavailable"
            link = _PreviewButton(
                f"{change}  ›",
                f"{name}, {unit}, {spoken_change}, {status}. View measurement details.",
            )
            direction, better = metric.get("direction"), metric.get("better")
            if status != "comparable":
                link.setProperty("tone", "weak")
            elif (better, direction) in {("lower", "increased"), ("higher", "decreased")}:
                link.setProperty("tone", "regression")
            link.previewRequested.connect(partial(self.measurementPreviewRequested.emit, index))
            link.previewDismissed.connect(self.previewDismissed)
            link.clicked.connect(partial(self.measurementRequested.emit, index))
            cell = QVBoxLayout()
            cell.setSpacing(2)
            cell.addWidget(link, alignment=Qt.AlignmentFlag.AlignLeft)
            cell.addWidget(_label(status.capitalize(), "muted"))
            table.addLayout(cell, row + 1, 3)
        layout.addLayout(table)
        body.addWidget(panel)

    @staticmethod
    def _metric_cell(primary: str, secondary: str) -> QVBoxLayout:
        cell = QVBoxLayout()
        cell.setSpacing(2)
        cell.setAlignment(Qt.AlignmentFlag.AlignTop)
        cell.addWidget(_label(primary, selectable=True))
        cell.addWidget(_label(secondary, "muted"))
        return cell

    def _gaps(self, body: QVBoxLayout, data: dict):
        gaps = {message: ([], []) for message in data.get("gaps", [])}
        for gap in data.get("benchmarks", {}).get("gaps", []):
            references, contexts = gaps.setdefault(gap["message"], ([], []))
            references.extend(gap.get("evidence", []))
            if benchmark := gap.get("benchmark"):
                contexts.append(f"{benchmark} · {gap['unit']}" if gap.get("unit") else benchmark)
            elif side := gap.get("side"):
                contexts.append(side.title())
        if not gaps:
            body.addWidget(_label("No measurement gaps recorded.", "muted"))
            return
        for message, (references, contexts) in gaps.items():
            panel, layout = _panel()
            layout.addWidget(_label(message, selectable=True))
            if contexts:
                layout.addWidget(_label("\n".join(dict.fromkeys(contexts)), "muted"))
            self._citations(layout, references, data)
            body.addWidget(panel)


class ExperimentsView(_ReadingView):
    def __init__(self, parent=None):
        self._expanded: set[int] = set()
        super().__init__(parent)

    def set_report(self, data: dict):
        if not self._same_revisions(data):
            self._expanded.clear()
        super().set_report(data)

    def _render(self, body: QVBoxLayout, data: dict):
        experiments = (data.get("findings") or {}).get("experiments", [])
        if not experiments:
            body.addWidget(_label("No experiments proposed for this investigation.", "muted"))
        for index, experiment in enumerate(experiments):
            panel, layout = _panel()
            intro = QHBoxLayout()
            intro.addWidget(_label(experiment["kind"].capitalize(), "accent"))
            intro.addStretch()
            toggle = _action(QPushButton("Show details"))
            toggle.setAccessibleName(f"Experiment details · {experiment['hypothesis']}")
            toggle.setCheckable(True)
            intro.addWidget(toggle)
            layout.addLayout(intro)
            layout.addWidget(_label(experiment["hypothesis"], "heading", selectable=True))
            layout.addWidget(_label(experiment["rationale"], selectable=True))
            details = self._details(experiment, data.get("revisions", {}))
            layout.addWidget(details)
            toggle.toggled.connect(partial(self._toggle, index, details, toggle))
            expanded = index in self._expanded
            if expanded:
                toggle.setChecked(True)
            details.setVisible(expanded)
            body.addWidget(panel)

    def _toggle(self, index: int, details: QWidget, button: QPushButton, expanded: bool):
        details.setVisible(expanded)
        button.setText("Hide details" if expanded else "Show details")
        if expanded:
            self._expanded.add(index)
        else:
            self._expanded.discard(index)

    def _details(self, experiment: dict, revisions: dict) -> QWidget:
        details = QWidget()
        layout = QVBoxLayout(details)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)
        revision = experiment["revision"]
        side = next((side.title() for side, sha in revisions.items() if sha == revision), "")
        revision_text = _label(f"{side} {revision[:8]}".strip(), selectable=True)
        revision_text.setToolTip(revision)
        layout.addWidget(_label("Revision", "muted"))
        layout.addWidget(revision_text)
        for key, title in _EXPERIMENT_FIELDS:
            layout.addWidget(_label(title, "muted"))
            layout.addWidget(_label(experiment[key], selectable=True))
        command = experiment["command_or_patch"]
        toolbar = QHBoxLayout()
        toolbar.addWidget(_label("Command or patch", "heading"))
        toolbar.addStretch()
        copy = _action(QPushButton("Copy"))
        copy.setAccessibleName("Copy command or patch")
        copy.clicked.connect(partial(self._copy, command, copy))
        toolbar.addWidget(copy)
        layout.addLayout(toolbar)
        command_view = QPlainTextEdit(command)
        command_view.setObjectName("experimentCommand")
        command_view.setAccessibleName("Command or patch")
        command_view.setReadOnly(True)
        command_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        command_view.setFixedHeight(min(126, max(48, 22 + command_view.blockCount() * 18)))
        layout.addWidget(command_view)
        return details

    @staticmethod
    def _copy(command: str, button: QPushButton):
        QApplication.clipboard().setText(command)
        button.setText("Copied")
