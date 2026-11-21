"""Native investigation window, document navigation, and background evidence loading."""

import html
import sys
from functools import partial
from pathlib import Path

from PySide6.QtCore import (
    QByteArray,
    QObject,
    QPoint,
    QRunnable,
    QSettings,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QToolTip,
)
from shiboken6 import isValid

from .chrome import Dock, Navigator, TopBar, Welcome, label
from .evidence_view import CodeView, EvidenceView
from .reader import Reader
from .report_view import ExperimentsView, ReportView
from .run_view import RunDialog
from .theme import apply_theme, icon
from .worker import RunController


class Completed(QObject):
    delivered = Signal(str, int, object, str)


class Load(QRunnable):
    def __init__(self, channel, ticket, function, signal):
        super().__init__()
        self.channel, self.ticket, self.function, self.signal = channel, ticket, function, signal

    def run(self):
        try:
            result, error = self.function(), ""
        except Exception as exc:  # noqa: BLE001 -- surface worker errors in the requesting pane
            result, error = None, str(exc) or type(exc).__name__
        self.signal.delivered.emit(self.channel, self.ticket, result, error)


class Workspace(QMainWindow):
    def __init__(self, settings=None):
        super().__init__()
        # Keep the settings namespace so saved layouts and recent reports carry over.
        self.settings = settings or QSettings("Profiler AI", "Workspace")
        self.reader = None
        self.close_after_run = False
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self.events = Completed(self)
        self.events.delivered.connect(self._loaded)
        self.tickets = {}
        self.callbacks = {}
        self.last_pin = None
        self.origin = None
        self.setWindowTitle("PerfScout")
        self.setWindowIcon(icon("brand"))
        self.setDockNestingEnabled(True)
        self.setTabPosition(Qt.DockWidgetArea.AllDockWidgetAreas, QTabWidget.TabPosition.North)
        self.resize(1280, 840)
        self.setMinimumSize(700, 470)
        self.top = TopBar(self.start_dialog, self.choose_directory, self.back)
        self.setMenuWidget(self.top)
        self.top.back.setEnabled(False)
        self.nav = Navigator()
        self.nav.selected.connect(self.navigate)
        self.report = ReportView()
        self.experiments = ExperimentsView()
        self.evidence = EvidenceView()
        self.code = CodeView()
        self.welcome = Welcome()
        self.welcome.openRequested.connect(self.choose_directory)
        self.welcome.newRequested.connect(self.start_dialog)
        self.welcome.recentRequested.connect(self.open_directory)
        recent = self.settings.value("recent", "")
        if recent and (Path(recent) / "report.json").is_file():
            self.welcome.recent.setText("Recent   ·   " + Path(recent).name)
            self.welcome.recent.setToolTip(recent)
            self.welcome.recent.show()
        self.pages = QStackedWidget()
        self.pages.addWidget(self.welcome)
        self.pages.addWidget(self.report)
        self.docks = {
            key: Dock(title, glyph, widget, self)
            for key, title, glyph, widget in [
                ("nav", "Navigator", "layout", self.nav),
                ("report", "Report", "report", self.pages),
                ("experiments", "Experiments", "experiments", self.experiments),
                ("evidence", "Evidence", "evidence", self.evidence),
                ("code", "Code and diff", "code", self.code),
            ]
        }
        self.reset_layout()
        menu = QMenu(self)
        for dock in self.docks.values():
            menu.addAction(dock.toggleViewAction())
        menu.addSeparator()
        menu.addAction("Reset layout", self.reset_layout)
        self.top.layout_button.setMenu(menu)
        self.nav.setEnabled(False)
        self.nav.profiles.hide()
        self._profile_menu = None
        for widget in (self.report, self.experiments):
            widget.evidenceRequested.connect(partial(self.pin, "resolve"))
            widget.measurementRequested.connect(partial(self.pin, "measurement"))
            widget.previewRequested.connect(partial(self.preview, "resolve"))
            widget.measurementPreviewRequested.connect(partial(self.preview, "measurement"))
            widget.previewDismissed.connect(self.dismiss_preview)
        self.report.experimentsRequested.connect(lambda: self.navigate("experiments"))
        self.evidence.openEvidence.connect(partial(self.pin, "resolve"))
        self.evidence.previewRequested.connect(partial(self.preview, "resolve"))
        self.evidence.previewDismissed.connect(self.dismiss_preview)
        self.evidence.openCode.connect(self.open_code)
        self.evidence.queryProfile.connect(
            lambda profile, op, params: self.pin("profile", profile, op, params)
        )
        self.evidence.locateRun.connect(self.locate_run)
        self.evidence.locateRepository.connect(self.locate_repository)
        self.code.locateRepository.connect(self.locate_repository)
        self.controller = RunController(self)
        self.controller.progress.connect(self._progress)
        self.controller.checkpoint.connect(self.open_directory)
        self.controller.finished.connect(self._finished)
        self.state_label = label("Ready")
        self.state_label.setContentsMargins(8, 2, 8, 2)
        self.statusBar().addWidget(self.state_label, 1)
        self.cancel_button = QPushButton("Cancel run")
        self.cancel_button.clicked.connect(self.controller.cancel)
        self.statusBar().addPermanentWidget(self.cancel_button)
        self.cancel_button.hide()
        self._shortcut("Ctrl+O", self.choose_directory)
        self._shortcut("Ctrl+N", self.start_dialog)
        self._shortcut("Alt+Left", self.back)
        self._shortcut("Escape", self.dismiss_preview)
        self._shortcut("F6", self.next_pane)
        geometry = self.settings.value("geometry")
        if isinstance(geometry, QByteArray):
            self.restoreGeometry(geometry)

    def _shortcut(self, sequence, callback):
        action = QAction(self)
        action.setShortcut(QKeySequence(sequence))
        action.triggered.connect(callback)
        self.addAction(action)

    def reset_layout(self):
        left, right = Qt.DockWidgetArea.LeftDockWidgetArea, Qt.DockWidgetArea.RightDockWidgetArea
        for dock in self.docks.values():
            dock.setFloating(False)
            self.removeDockWidget(dock)
        nav, report, experiments, evidence, code = self.docks.values()
        self.addDockWidget(left, nav)
        self.splitDockWidget(nav, report, Qt.Orientation.Horizontal)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, experiments)
        self.addDockWidget(right, evidence)
        self.tabifyDockWidget(evidence, code)
        report.show()
        nav.setVisible(self.reader is not None)
        evidence.hide()
        code.setVisible(self.reader is not None)
        experiments.setVisible(
            bool(self.reader and (self.reader.data.get("findings") or {}).get("experiments"))
        )
        self.resizeDocks(
            [nav, report, code],
            [185, self.width() // 2, self.width() * 2 // 5],
            Qt.Orientation.Horizontal,
        )
        self.resizeDocks(
            [report, experiments],
            [self.height() * 3 // 4, self.height() // 4],
            Qt.Orientation.Vertical,
        )
        report.raise_()
        self.nav.select("report")

    def _load(self, channel, function, callback):
        ticket = self.tickets.get(channel, 0) + 1
        self.tickets[channel] = ticket
        self.callbacks[channel] = callback
        self.pool.start(Load(channel, ticket, function, self.events))

    @Slot(str, int, object, str)
    def _loaded(self, channel, ticket, result, error):
        if self.tickets.get(channel) == ticket:
            self.callbacks.pop(channel)(result, error)

    def dismiss_preview(self):
        self.tickets["hover"] = self.tickets.get("hover", 0) + 1
        QToolTip.hideText()

    def preview(self, method, key, position: QPoint):
        if not self.reader:
            return

        def display(result, error):
            title = result.get("title", "Evidence") if result else "Evidence unavailable"
            body = (error or result.get("text", "")).strip()
            lines = body.splitlines()
            code = result and result.get("kind") in {"source", "diff"}
            if result and result.get("kind") == "diff":
                first_hunk = next((i for i, line in enumerate(lines) if line.startswith("@@")), 0)
                lines = lines[first_hunk:]
            if result and result.get("lines"):
                code = True
                lines = [
                    f"{'›' if number == result['line'] else ' '} {number:>4}  {line}"
                    for number, line in result["lines"]
                ]
            snippet = "\n".join(line[:94] for line in lines[:8])[:650]
            if len(lines) > 8:
                snippet += "\n…"
            content = html.escape(snippet)
            content = f"<pre>{content}</pre>" if code else content.replace("\n", "<br/>")
            tip = f'<div style="width:390px"><b>{html.escape(title)}</b><br/>{content}</div>'
            QToolTip.showText(position, tip, self, msecShowTime=12000)

        self._load("hover", partial(getattr(self.reader, method), key), display)

    def _show(self, key):
        dock = self.docks[key]
        previously_visible = dock.isVisible()
        dock.show()
        dock.raise_()
        if (
            key in {"code", "evidence"}
            and not previously_visible
            and not dock.isFloating()
            and self.dockWidgetArea(dock) == Qt.DockWidgetArea.RightDockWidgetArea
        ):
            inspector = int(self.width() * (0.52 if key == "code" else 0.36))
            self.resizeDocks(
                [self.docks["nav"], self.docks["report"], dock],
                [175, max(300, self.width() - inspector - 175), inspector],
                Qt.Orientation.Horizontal,
            )
        return dock

    def pin(self, method, *args):
        if not self.reader:
            return
        focus = QApplication.focusWidget()
        if focus and (self.report.isAncestorOf(focus) or self.experiments.isAncestorOf(focus)):
            self.origin = focus
        self.dismiss_preview()
        self.last_pin = (method, *args)
        self.top.back.setEnabled(True)
        self.statusBar().showMessage("Opening evidence…")

        def display(result, error):
            self.statusBar().clearMessage()
            if error:
                result = {"title": "Evidence unavailable", "kind": "gap", "text": error}
            if result["kind"] in {"diff", "source"}:
                self.open_code(result)
            else:
                self.evidence.show_result(result)
                self._show("evidence")
                self.nav.select("evidence")

        self._load("pin", partial(getattr(self.reader, method), *args), display)

    def open_code(self, result):
        self._show("code")
        self.nav.select("code")
        self.code.open_result(result)
        self.top.back.setEnabled(True)

    def back(self):
        if not self.reader:
            return
        self.statusBar().clearMessage()
        self.tickets["pin"] = self.tickets.get("pin", 0) + 1
        self.dismiss_preview()
        focus = self.origin if self.origin is not None and isValid(self.origin) else None
        key = "experiments" if focus and self.experiments.isAncestorOf(focus) else "report"
        self._show(key)
        self.nav.select(key)
        (focus or self.docks[key].widget()).setFocus()
        self.top.back.setEnabled(False)

    def next_pane(self):
        visible = [
            d for d in self.docks.values() if d.isVisible() and not d.visibleRegion().isEmpty()
        ]
        if visible:
            current = QApplication.focusWidget()
            index = next(
                (i for i, d in enumerate(visible) if current and d.isAncestorOf(current)), -1
            )
            pane = visible[(index + 1) % len(visible)].widget()
            pane.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def navigate(self, section):
        if not self.reader:
            return
        self.statusBar().clearMessage()
        self.tickets["pin"] = self.tickets.get("pin", 0) + 1
        self.dismiss_preview()
        self.nav.select(section)
        if section in {"report", "measurements", "findings", "gaps"}:
            self._show("report")
            self.report.jump(section)
        else:
            self._show(section)

    def choose_directory(self):
        path = QFileDialog.getExistingDirectory(self, "Open investigation directory")
        if path:
            self.open_directory(path)

    def open_directory(self, path):
        path = Path(path)
        self._load("document", partial(Reader, path), self._document_loaded)

    def _document_loaded(self, reader, error):
        self.statusBar().clearMessage()
        if error:
            QMessageBox.warning(self, "Could not open investigation", error)
            return
        first = self.reader is None
        same = not first and self.reader.directory == reader.directory
        self.dismiss_preview()
        self.tickets["pin"] = self.tickets.get("pin", 0) + 1
        self.reader = reader
        self.pages.setCurrentWidget(self.report)
        if not first and not same:
            self.report.set_report({})
            self.experiments.set_report({})
        self.report.set_report(reader.data)
        self.experiments.set_report(reader.data)
        self.code.set_reader(reader)
        self.nav.setEnabled(True)
        if not same:
            self.origin = self.last_pin = None
            self.top.back.setEnabled(False)
            self.evidence.show_result(
                {
                    "kind": "empty",
                    "title": "Inspect evidence",
                    "text": "Select a citation or measurement in the report to view its source here.",
                }
            )
            self.docks["nav"].show()
            if first:
                layout = self.settings.value("layout")
                if not isinstance(layout, QByteArray) or not self.restoreState(layout, 2):
                    self.reset_layout()
            self._show("report")
            self.nav.select("report")
        profiles = reader.data.get("profiles", {})
        menu = QMenu(self.nav)
        for profile_id, desc in profiles.items():
            side, _, number = profile_id.partition("/")
            capture = f"   ·   Capture {int(number) + 1}" if number.isdecimal() else ""
            action = menu.addAction(
                f"{side.capitalize()}   ·   {desc.get('format', 'Profile')}{capture}"
            )
            action.triggered.connect(lambda checked=False, key=profile_id: self.pin("profile", key))
        self.nav.profiles.setMenu(menu)
        if self._profile_menu:
            self._profile_menu.deleteLater()
        self._profile_menu = menu
        self.nav.profiles.setVisible(bool(profiles))
        self.top.name.setText(reader.directory.name)
        self.setWindowTitle(f"{reader.directory.name} — PerfScout")
        self.settings.setValue("recent", str(reader.directory))
        if not self.controller.active:
            status = reader.data.get("status", "").replace("_", " ").capitalize()
            self.state_label.setText(status + "   ·   " + reader.directory.name)

    def _relocate(self, side=None):
        if not self.reader:
            return
        path = QFileDialog.getExistingDirectory(
            self, "Locate " + (f"{side} run" if side else "repository")
        )
        if not path:
            return
        reader = self.reader

        def done(_, error):
            if self.reader is not reader:
                return
            if error:
                QMessageBox.warning(self, "Could not verify location", error)
                return
            self.code.refresh()
            if self.last_pin:
                self.pin(*self.last_pin)

        task = (
            partial(reader.set_run_directory, side, Path(path))
            if side
            else partial(reader.set_repository, Path(path))
        )
        self._load("location", task, done)

    def locate_run(self, side):
        self._relocate(side)

    def locate_repository(self):
        self._relocate()

    def start_dialog(self):
        if self.controller.active:
            return
        dialog = RunDialog(self)
        if dialog.exec():
            self.top.new.setEnabled(False)
            self.cancel_button.show()
            self.state_label.setText("Starting investigation…")
            self.controller.start(dialog.arguments())

    def _progress(self, stage):
        self.state_label.setText(stage)

    def _finished(self, directory, status, diagnostic):
        if self.close_after_run:
            QTimer.singleShot(0, self.close)
            return
        self.top.new.setEnabled(True)
        self.cancel_button.hide()
        if directory and (Path(directory) / "report.json").is_file():
            self.open_directory(directory)
        self.state_label.setText(
            status.replace("_", " ").capitalize() + ("   ·   " + diagnostic if diagnostic else "")
        )
        if diagnostic:
            QMessageBox.information(self, "Investigation " + status.replace("_", " "), diagnostic)

    def closeEvent(self, event):
        if self.controller.active:
            answer = QMessageBox.question(
                self,
                "Cancel investigation?",
                "Cancel the running investigation and close this window?",
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.close_after_run = True
                self.controller.cancel()
            event.ignore()
            return
        self.settings.setValue("geometry", self.saveGeometry())
        if self.reader:
            self.settings.setValue("layout", self.saveState(2))
        self.pool.clear()
        self.callbacks.clear()
        self.tickets.clear()
        super().closeEvent(event)


def launch(directory=None):
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("PerfScout")
    apply_theme(app)
    window = Workspace()
    window.show()
    if directory:
        window.open_directory(directory)
    return app.exec()
