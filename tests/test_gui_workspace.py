"""Exercise the desktop entry point from a saved investigation through evidence and back."""

import json
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QSettings
from PySide6.QtWidgets import QApplication, QToolTip

from profiler_ai.gui.app import Workspace


def wait_for(app, predicate):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_open_preview_dock_and_restore(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    directory = tmp_path / "run"
    directory.mkdir()
    (directory / "report.json").write_text(
        json.dumps(
            {
                "revisions": {"baseline": "a" * 40, "candidate": "b" * 40},
                "benchmarks": {"comparisons": [], "gaps": []},
                "profiles": {},
                "gaps": [],
                "findings": None,
                "status": "measurements_only",
                "citations": {},
            }
        )
    )
    window = Workspace(settings)
    window.show()
    window.open_directory(directory)
    wait_for(app, lambda: window.reader is not None)
    assert window.pages.currentWidget() == window.report
    window.preview("measurement", 0, QPoint(120, 120))
    wait_for(app, lambda: "Measurement unavailable" in QToolTip.text())
    window.pin("measurement", 0)
    dock = window.docks["evidence"]
    wait_for(app, dock.isVisible)
    assert window.top.back.isEnabled()
    window.back()
    assert window.nav.buttons["report"].isChecked()
    dock.setFloating(True)
    assert dock.isFloating()
    window.close()
    restored = Workspace(settings)
    restored.show()
    restored.open_directory(directory)
    wait_for(app, lambda: restored.reader is not None)
    assert restored.docks["evidence"].isFloating()
    restored.reset_layout()
    assert not restored.docks["evidence"].isVisible()
    restored.close()
