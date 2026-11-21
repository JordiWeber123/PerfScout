"""Pinned evidence, committed source and Git patch views for the desktop app."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from typing import ClassVar

from PySide6.QtCore import QObject, QPoint, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFontDatabase,
    QSyntaxHighlighter,
    QTextCursor,
    QTextFormat,
)
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .reader import Reader

_SIDES = ("baseline", "candidate")
_HUNK = re.compile(r"^@@ -([0-9]+)(?:,[0-9]+)? \+([0-9]+)(?:,[0-9]+)? @@")
_MODES = ("Side by side", "Unified diff", "Baseline source", "Candidate source")
_SOURCE_LINES = 4000
_SOURCE_WIDTH = 4000
_COLORS = {
    "add": ("#283e22", "#b9e0ce"),
    "remove": ("#502722", "#efbac3"),
    "hunk": ("#323027", "#c8c7c3"),
    "cite": ("#43423e", "#e8e7e3"),
}


def _label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def _html(value) -> str:
    return escape("—" if value is None else str(value))


def _link(reference: str, label: str) -> str:
    return f'<a href="evidence:{_html(reference)}">{_html(label)}</a>'


def _pre(text: str) -> str:
    return f"<pre>{_html(text)}</pre>" if text else ""


def _measurement_html(result: dict) -> str:
    html = [f"<p>{_html(result['text']).replace(chr(10), '<br>')}</p>"]
    for side in _SIDES:
        samples = result["comparison"][side].get("samples", [])
        if not samples:
            continue
        html.append(f"<h3>{side.title()} repetitions</h3>")
        html.append(
            "<table cellpadding='4'><tr><th>#</th><th>Recorded value</th><th>Iterations</th></tr>"
        )
        for index, sample in enumerate(samples, 1):
            value = f"{sample['original_value']} {sample['original_unit']}"
            html.append(
                f"<tr><td>{index}</td><td>{_link(sample['evidence'], value)}</td>"
                f"<td>{_html(sample['iterations'])}</td></tr>"
            )
        html.append("</table>")
    return "".join(html)


def _profile_html(result: dict) -> str:
    summary = result["summary"]
    query = result.get("query")
    html = [f"<p>{_html(result['text']).replace(chr(10), '<br>')}</p>"] if result["text"] else []
    summary_refs = [
        _link(ref, "Raw profile" if index == 0 else "Matching executable")
        for index, ref in enumerate(summary.get("evidence", []))
    ]
    if summary_refs:
        html.append("<p>" + " · ".join(summary_refs) + "</p>")
    if query is None:
        if summary["format"] == "pprof":
            html.append(f"<p>{_html(summary.get('sample_records'))} sample records</p>")
            items = [
                f"{_html(item['metric'])} · {_html(item['total_weight'])} {_html(item['unit'])}"
                for item in summary.get("metrics", [])
            ]
        else:
            html.append(
                f"<p>{_html(summary.get('reading_count'))} readings · "
                f"{_html(summary.get('thread_count'))} threads · "
                f"{_html(summary.get('region_count'))} regions</p>"
            )
            items = [
                f"{_html(item['event'])} · {_html(item.get('unit'))}"
                for item in summary.get("events", [])
            ]
        html.append("<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>")
        html.extend(f"<p>{_html(gap)}</p>" for gap in summary.get("gaps", []))
        return "".join(html)

    rows = query.get("rows", [])
    offset, total = query.get("offset", 0), query["total_rows"]
    if rows:
        html.append(f"<p>Showing {offset + 1}–{offset + len(rows)} of {total}</p>")
    if query["operation"] == "counters":
        pages = []
        if offset:
            pages.append(f'<a href="page:{max(0, offset - query["limit"])}">Previous</a>')
        if offset + len(rows) < total:
            pages.append(f'<a href="page:{offset + len(rows)}">Next</a>')
        if pages:
            html.append("<p>" + " · ".join(pages) + "</p>")
    html.extend(f"<p>{_html(gap)}</p>" for gap in query.get("gaps", []))
    if "metric" in query:
        html.append(
            f"<p>{_html(query['metric'])} · "
            f"{_html(query['total_weight'])} {_html(query.get('unit'))} total weight</p>"
        )
    for row in rows:
        if query["operation"] == "hotspots":
            title = row["function"]
            detail = f"{row['flat']} flat · {row['cumulative']} cumulative · {row['sample_records']} records"
            labels = ["Location", *(f"Sample {i}" for i in range(1, len(row["evidence"])))]
        else:
            title = row["event"]
            identity = f"Thread {row['thread']} · {row['region']} · {row['reading']}"
            if row["rank"] is not None:
                identity = f"Rank {row['rank']} · " + identity
            amount = f"{row['exact_value']} {row['unit'] or ''}".rstrip()
            detail = f"{amount} · {identity}"
            labels = ["Counter reading"]
        refs = [_link(ref, label) for ref, label in zip(row["evidence"], labels, strict=True)]
        html.append(f"<p><b>{_html(title)}</b><br>{_html(detail)}<br>{' · '.join(refs)}</p>")
    return "".join(html)


class EvidenceView(QWidget):
    """A pinned, selectable evidence preview with recovery and navigation actions."""

    locateRepository = Signal()
    locateRun = Signal(str)
    openCode = Signal(dict)
    openEvidence = Signal(str)
    queryProfile = Signal(str, str, dict)
    previewRequested = Signal(str, QPoint)
    previewDismissed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._result: dict = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        self.title = _label()
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.metadata = _label()
        self.metadata.setWordWrap(True)
        self.metadata.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.metadata.setStyleSheet("color: #b0afab; font-size: 11px;")
        actions = QHBoxLayout()
        self.locate_repository = QPushButton("Locate repository")
        self.locate_repository.clicked.connect(self.locateRepository)
        self.open_code = QPushButton("Open in Code")
        self.open_code.clicked.connect(lambda: self.openCode.emit(self._result))
        self._run_buttons = {}
        for side in _SIDES:
            button = QPushButton(f"Locate {side} run")
            button.clicked.connect(lambda _checked=False, side=side: self.locateRun.emit(side))
            self._run_buttons[side] = button
            actions.addWidget(button)
        for button in (self.locate_repository, self.open_code):
            actions.addWidget(button)
        actions.addStretch(1)
        self._profile_options = QComboBox()
        self._aggregation = QComboBox()
        self._aggregation.setAccessibleName("Hotspot aggregation")
        self._aggregation.addItem("Flat", "flat")
        self._aggregation.addItem("Cumulative", "cumulative")
        self._profile_button = QPushButton()
        self._profile_button.clicked.connect(self._run_profile_query)
        profile_controls = QHBoxLayout()
        profile_controls.addWidget(self._profile_options, 1)
        profile_controls.addWidget(self._aggregation)
        profile_controls.addWidget(self._profile_button)
        self.details = QTextBrowser()
        self.details.setOpenLinks(False)
        self.details.setOpenExternalLinks(False)
        self.details.setStyleSheet(
            "QTextBrowser {background: #252420; color: #e9e8e4; border: 0; padding: 5px;}"
        )
        self.details.document().setDefaultStyleSheet("a { color: #ff921f; }")
        self._hover_url = QUrl()
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(300)
        self._hover_timer.timeout.connect(self._preview)
        self.details.highlighted.connect(self._highlighted)
        self.details.anchorClicked.connect(self._follow_evidence)
        layout.addWidget(self.title)
        layout.addWidget(self.metadata)
        layout.addLayout(actions)
        layout.addLayout(profile_controls)
        layout.addWidget(self.details, 1)
        self.show_result(
            {"title": "Evidence", "text": "Select a citation or measurement to inspect it."}
        )

    def show_result(self, result: dict) -> None:
        self._highlighted(QUrl())
        self._result = result
        kind = result.get("kind")
        self.title.setText(result.get("title", "Evidence"))
        locator = result.get("locator")
        location = result.get("artifact_path") or result.get("path") or result.get("repository")
        fields = [location, result.get("revision"), result.get("digest"), locator]
        self.metadata.setText(
            "\n".join(field for field in fields if isinstance(field, str) and field)
        )
        self.metadata.setVisible(any(fields))
        action = result.get("action")
        self.locate_repository.setVisible(action == "repository")
        self.open_code.setVisible(kind in {"source", "diff"})
        for side, button in self._run_buttons.items():
            button.setVisible(action == "run" and result.get("recovery_side") in (side, None))
        self._profile_options.setVisible(kind == "profile")
        self._profile_button.setVisible(kind == "profile")
        self._aggregation.setVisible(kind == "profile" and result["summary"]["format"] == "pprof")
        if kind == "measurement":
            html = _measurement_html(result)
        elif kind == "profile":
            self._setup_profile_controls(result)
            html = _profile_html(result)
        elif result.get("lines"):
            number = result["line"]
            html = (
                "<pre>"
                + "\n".join(
                    f"<b style='background-color:#44433f;color:#e8e7e3'>{index:>5}  {_html(line)}</b>"
                    if index == number
                    else f"{index:>5}  {_html(line)}"
                    for index, line in result["lines"]
                )
                + "</pre>"
            )
        else:
            html = _pre(result.get("text", ""))
        self.details.setHtml(html)

    @Slot(QUrl)
    def _follow_evidence(self, url: QUrl) -> None:
        if url.scheme() == "evidence":
            self._highlighted(QUrl())
            self.openEvidence.emit(url.toString().removeprefix("evidence:"))
        elif url.scheme() == "page" and self._result.get("operation") == "counters":
            offset = url.toString().removeprefix("page:")
            if offset.isdecimal():
                params = {**self._result["params"], "offset": int(offset)}
                self.queryProfile.emit(self._result["profile_id"], "counters", params)

    @Slot(QUrl)
    def _highlighted(self, url: QUrl) -> None:
        if url == self._hover_url:
            return
        self._hover_timer.stop()
        if not self._hover_url.isEmpty():
            self.previewDismissed.emit()
        self._hover_url = url if url.scheme() == "evidence" else QUrl()
        if not self._hover_url.isEmpty():
            self._hover_timer.start()

    @Slot()
    def _preview(self) -> None:
        viewport = self.details.viewport()
        url = self._hover_url.toString()
        position = QCursor.pos()
        local = viewport.mapFromGlobal(position)
        if self.details.anchorAt(local) != url:
            position = viewport.mapToGlobal(self.details.cursorRect().bottomLeft())
        self.previewRequested.emit(url.removeprefix("evidence:"), position)

    def hideEvent(self, event) -> None:
        self._highlighted(QUrl())
        super().hideEvent(event)

    def _setup_profile_controls(self, result: dict) -> None:
        summary = result["summary"]
        self._profile_options.clear()
        if summary["format"] == "pprof":
            self._profile_button.setText("Show hotspots")
            aggregation = result["params"].get("aggregation", "flat")
            self._aggregation.setCurrentIndex(self._aggregation.findData(aggregation))
            metric = result["params"].get("metric", summary.get("default_metric_index"))
            for item in summary.get("metrics", []):
                name = item["metric"]
                if item.get("unit"):
                    name += f" · {item['unit']}"
                self._profile_options.addItem(name, item["metric_index"])
            selected = self._profile_options.findData(metric)
        else:
            self._profile_button.setText("Show counters")
            self._profile_options.addItem("All events", None)
            for item in summary.get("events", []):
                self._profile_options.addItem(item["event"], item["event"])
            selected = self._profile_options.findData(result["params"].get("event"))
        if selected >= 0:
            self._profile_options.setCurrentIndex(selected)

    @Slot()
    def _run_profile_query(self) -> None:
        metric = self._profile_options.currentData()
        if self._result["summary"]["format"] == "pprof":
            operation, params = "hotspots", {"metric": metric} if metric is not None else {}
            params["aggregation"] = self._aggregation.currentData() or "flat"
        else:
            operation, params = "counters", {"event": metric} if metric is not None else {}
        self.queryProfile.emit(self._result["profile_id"], operation, params)


@dataclass(frozen=True)
class _Line:
    text: str = ""
    number: int | None = None
    kind: str = ""

    def display(self) -> str:
        return f"{self.number:>6}  {self.text}" if self.number is not None else self.text


def _diff_columns(patch: str) -> tuple[list[_Line], list[_Line]]:
    """Pair adjacent removed and added lines within each Git hunk."""
    before, after = [], []
    removed, added = [], []
    left = right = None

    def flush() -> None:
        size = max(len(removed), len(added))
        before.extend([*removed, *([_Line()] * (size - len(removed)))])
        after.extend([*added, *([_Line()] * (size - len(added)))])
        removed.clear()
        added.clear()

    for line in patch.split("\n"):
        if hunk := _HUNK.match(line):
            flush()
            left, right = int(hunk[1]), int(hunk[2])
            before.append(_Line(line, kind="hunk"))
            after.append(_Line(line, kind="hunk"))
        elif left is not None and right is not None:
            prefix, text = line[:1], line[1:].rstrip("\r")
            if prefix == "-":
                removed.append(_Line(text, left, "remove"))
                left += 1
            elif prefix == "+":
                added.append(_Line(text, right, "add"))
                right += 1
            elif prefix == " ":
                flush()
                before.append(_Line(text, left))
                after.append(_Line(text, right))
                left += 1
                right += 1
            elif line.startswith("diff --git "):
                flush()
                left = right = None
    flush()
    return before, after


def _unified_lines(patch: str) -> list[_Line]:
    lines = []
    inside_hunk = False
    for line in patch.rstrip("\n").split("\n"):
        kind = ""
        if line.startswith("diff --git "):
            inside_hunk = False
        elif _HUNK.match(line):
            inside_hunk = True
            kind = "hunk"
        elif inside_hunk:
            kind = {"+": "add", "-": "remove"}.get(line[:1], "")
        lines.append(_Line(line.rstrip("\r"), kind=kind))
    return lines


def _source_lines(text: str, focus: tuple[int, int] | None) -> list[_Line]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    start = 0 if focus is None else max(0, focus[0] - (_SOURCE_LINES // 2))
    end = start + _SOURCE_LINES
    result = [
        _Line(
            line[:_SOURCE_WIDTH].rstrip("\r") + ("…" if len(line) > _SOURCE_WIDTH else ""),
            number,
            "cite" if focus and focus[0] <= number <= focus[1] else "",
        )
        for number, line in enumerate(lines[start:end], start + 1)
    ]
    if start:
        result.insert(0, _Line(f"… {start} earlier lines"))
    if end < len(lines):
        result.append(_Line(f"… {len(lines) - end} more lines"))
    return result


class _JobSignals(QObject):
    done = Signal(int, object, str)


class _Job(QRunnable):
    def __init__(self, token: int, work: Callable, signals: _JobSignals):
        super().__init__()
        self.token = token
        self.work = work
        self.signals = signals

    def run(self) -> None:
        try:
            value = self.work()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.signals.done.emit(self.token, None, str(exc))
        else:
            self.signals.done.emit(self.token, value, "")


class _CodeHighlight(QSyntaxHighlighter):
    tokens = re.compile(
        r'(?P<string>"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27|`[^`]*`)'
        r"|(?P<comment>//.*)"
        r"|\b(?P<keyword>func|def|class|struct|return|if|else|for|range|while|import|package|type|const|var|switch|case|break|continue)\b"
        r"|\b(?P<function>[A-Za-z_]\w*)(?=\()"
    )
    colors: ClassVar[dict[str, str]] = {
        "string": "#d8bc80",
        "comment": "#9c9b8d",
        "keyword": "#84d9e6",
        "function": "#e3b981",
    }

    def highlightBlock(self, text):
        for token in self.tokens.finditer(text):
            # Qt counts UTF-16 positions, including both halves of an astral character.
            start = len(text[: token.start()].encode("utf-16-le")) // 2
            size = len(token.group().encode("utf-16-le")) // 2
            self.setFormat(start, size, QColor(self.colors[token.lastgroup]))


def _code_editor() -> QPlainTextEdit:
    editor = QPlainTextEdit()
    editor.setReadOnly(True)
    editor.setUndoRedoEnabled(False)
    editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setPointSizeF(11)
    editor.setFont(font)
    editor.setStyleSheet(
        "QPlainTextEdit {background: #252420; color: #e9e8e4; border: 0; selection-background-color: #4d6867;}"
    )
    return editor


def _paint(editor: QPlainTextEdit, lines: list[_Line]) -> None:
    editor.setPlainText("\n".join(line.display() for line in lines))
    selections = []
    focus = None
    block = editor.document().firstBlock()
    for line in lines:
        if line.kind in _COLORS:
            selection = QTextEdit.ExtraSelection()
            selection.cursor = QTextCursor(block)
            selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            background, foreground = _COLORS[line.kind]
            selection.format.setBackground(QColor(background))
            if line.kind in {"hunk", "cite"}:
                selection.format.setForeground(QColor(foreground))
            selections.append(selection)
            if line.kind == "cite" and focus is None:
                focus = QTextCursor(block)
        block = block.next()
    editor.setExtraSelections(selections)
    if focus is not None:
        editor.setTextCursor(focus)
        editor.centerCursor()


class CodeView(QWidget):
    """Browse changed files, aligned patches and individual committed revisions."""

    locateRepository = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._reader: Reader | None = None
        self._target: dict = {}
        self._hunk_focus: tuple[str, int, int] | None = None
        self._file_token = 0
        self._code_token = 0
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._file_events = _JobSignals(self)
        self._file_events.done.connect(self._files_loaded)
        self._code_events = _JobSignals(self)
        self._code_events.done.connect(self._code_loaded)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        controls = QHBoxLayout()
        self.files = QComboBox()
        self.files.setAccessibleName("Changed file")
        self.files.setMinimumContentsLength(20)
        self.files.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.mode = QComboBox()
        self.mode.setAccessibleName("Code display mode")
        self.mode.addItems(_MODES)
        self.mode.setCurrentIndex(1)
        self.hunks = QComboBox()
        self.hunks.setAccessibleName("Jump to diff hunk")
        self.hunks.setPlaceholderText("Jump to hunk")
        self.hunks.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.hunks.setMinimumContentsLength(11)
        self.hunks.hide()
        self.hunks.activated.connect(self._jump_hunk)
        self.locate_repository = QPushButton("Locate repository")
        self.locate_repository.clicked.connect(self.locateRepository)
        self.locate_repository.hide()
        controls.addWidget(self.files, 1)
        controls.addWidget(self.mode)
        self.message = _label("Open an investigation to browse committed code.")
        self.message.setWordWrap(True)
        self.message.setStyleSheet("color: #b0afab;")
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        code_actions = QHBoxLayout()
        code_actions.addWidget(self.message, 1)
        code_actions.addWidget(self.hunks)
        code_actions.addWidget(self.locate_repository)
        self._split = QSplitter()
        self._split.setChildrenCollapsible(False)
        self._left, self._right = _code_editor(), _code_editor()
        self._highlighters = [
            _CodeHighlight(editor.document()) for editor in (self._left, self._right)
        ]
        self._left.setAccessibleName("Baseline or unified code")
        self._right.setAccessibleName("Candidate code")
        self._left_title, self._right_title = _label(), _label()
        for title, editor in ((self._left_title, self._left), (self._right_title, self._right)):
            pane = QWidget()
            pane_layout = QVBoxLayout(pane)
            pane_layout.setContentsMargins(0, 0, 0, 0)
            pane_layout.addWidget(title)
            pane_layout.addWidget(editor, 1)
            self._split.addWidget(pane)
        self._split.setStretchFactor(0, 1)
        self._split.setStretchFactor(1, 1)
        self._left.verticalScrollBar().valueChanged.connect(
            self._right.verticalScrollBar().setValue
        )
        self._right.verticalScrollBar().valueChanged.connect(
            self._left.verticalScrollBar().setValue
        )
        layout.addLayout(controls)
        layout.addLayout(code_actions)
        layout.addWidget(self._split, 1)
        self.files.currentIndexChanged.connect(self._load_code)
        self.mode.currentIndexChanged.connect(self._load_code)

    def set_reader(self, reader: Reader | None) -> None:
        self._reader = reader
        self._target = {}
        self._hunk_focus = None
        self.refresh()

    def open_result(self, result: dict) -> None:
        if result.get("kind") not in {"diff", "source"}:
            return
        self._target = result
        self._hunk_focus = None
        index = 0 if result["kind"] == "diff" else 2 + int(result.get("side") == "candidate")
        self.mode.blockSignals(True)
        self.mode.setCurrentIndex(index)
        self.mode.blockSignals(False)
        self.refresh()

    def _commits(self) -> tuple[str | None, str | None]:
        if self._target.get("kind") == "diff":
            return self._target.get("baseline"), self._target.get("revision")
        return None, None

    def refresh(self) -> None:
        preferred = self._target.get("path") or self.files.currentData()
        self._file_token += 1
        self._code_token += 1
        self._clear_code()
        self.files.blockSignals(True)
        self.files.clear()
        self.files.blockSignals(False)
        self.locate_repository.hide()
        reader = self._reader
        if reader is None:
            self.message.setText("Open an investigation to browse committed code.")
            return
        self.message.setText("Loading committed file list…")
        base, candidate = self._commits()
        job = _Job(
            self._file_token,
            lambda: (reader.changed_files(base, candidate), preferred),
            self._file_events,
        )
        self._pool.start(job)

    @Slot(int, object, str)
    def _files_loaded(self, token: int, value, error: str) -> None:
        if token != self._file_token:
            return
        if error:
            self.message.setText(error)
            self.locate_repository.show()
            return
        files, preferred = value
        scope = self._target.get("paths") or []
        status = {path: state for state, path in files}
        shown = scope or list(status)
        cited = self._target.get("path")
        if cited and cited not in shown:
            shown.insert(0, cited)
        self.files.blockSignals(True)
        for path in shown:
            label = f"{status[path]}  {path}" if path in status else path
            self.files.addItem(label, path)
            self.files.setItemData(
                self.files.count() - 1, status.get(path), Qt.ItemDataRole.UserRole + 1
            )
        selection = self.files.findData(preferred)
        self.files.setCurrentIndex(max(0, selection))
        self.files.blockSignals(False)
        if shown:
            self._load_code()
        else:
            self.message.setText("No changed files between the measured revisions.")

    def _clear_code(self) -> None:
        self._left.clear()
        self._right.clear()
        self._left_title.clear()
        self._right_title.clear()
        self.hunks.clear()
        self.hunks.hide()

    @Slot()
    def _load_code(self) -> None:
        reader, path = self._reader, self.files.currentData()
        if reader is None or path is None:
            return
        self._code_token += 1
        self._clear_code()
        self.message.setText("Loading committed code…")
        self.locate_repository.hide()
        mode = self.mode.currentIndex()
        base, candidate = self._commits()
        target = self._target
        hunk = self._hunk_focus
        status = self.files.currentData(Qt.ItemDataRole.UserRole + 1)
        self._split.widget(1).setVisible(mode == 0)

        def work() -> tuple:
            if mode < 2:
                patch, truncated = reader.code_diff(path, base, candidate)
                if mode == 0 and not truncated:
                    return mode, patch, _diff_columns(patch)
                return mode, patch, _unified_lines(patch)
            side = _SIDES[mode - 2]
            if (side, status) in {("baseline", "A"), ("candidate", "D")}:
                return mode, f"{path} is absent at the {side} revision.", []
            focus = None
            revision = (base, candidate)[mode - 2]
            if target.get("path") == path and target.get("side") == side:
                revision = target["revision"]
                focus = target["start"], target["end"]
            if hunk and hunk[0] == path:
                start = max(1, hunk[mode - 1])
                focus = start, start
            source = reader.code_source(side, path, revision)
            return mode, "", _source_lines(source, focus)

        job = _Job(self._code_token, work, self._code_events)
        self._pool.start(job)

    @Slot(int, object, str)
    def _code_loaded(self, token: int, value, error: str) -> None:
        if token != self._code_token:
            return
        if error:
            self.message.setText(error)
            return
        mode, patch, lines = value
        self.message.setText("")
        self._left_title.setText(("Baseline", "Unified diff", "Baseline", "Candidate")[mode])
        if mode == 0 and isinstance(lines, tuple) and lines[0]:
            self._right_title.setText("Candidate")
            _paint(self._left, lines[0])
            _paint(self._right, lines[1])
            self._set_hunks(lines[0])
        elif mode < 2 and patch:
            self._split.widget(1).hide()
            display = _unified_lines(patch) if isinstance(lines, tuple) else lines
            _paint(self._left, display)
            self._set_hunks(display)
        elif mode < 2:
            self.message.setText("No patch for this path between the selected revisions.")
        else:
            self.message.setText(patch)
            _paint(self._left, lines)

    def _set_hunks(self, lines: list[_Line]) -> None:
        current = -1
        path = self.files.currentData()
        for index, line in enumerate(lines):
            if line.kind == "hunk" and (hunk := _HUNK.match(line.text)):
                before, after = int(hunk[1]), int(hunk[2])
                self.hunks.addItem(f"−{before} / +{after}", (index, before, after))
                if self._hunk_focus == (path, before, after):
                    current = self.hunks.count() - 1
        self.hunks.setCurrentIndex(current)
        self.hunks.setVisible(self.hunks.count() > 0)
        if current >= 0:
            self._jump_hunk(current)

    @Slot(int)
    def _jump_hunk(self, index: int) -> None:
        hunk = self.hunks.itemData(index)
        if hunk is None:
            return
        number, before, after = hunk
        self._hunk_focus = self.files.currentData(), before, after
        block = self._left.document().findBlockByNumber(number)
        self._left.setTextCursor(QTextCursor(block))
        self._left.centerCursor()
