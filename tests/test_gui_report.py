"""Exercise report navigation, evidence links and experiment details with Qt."""

import os
from copy import deepcopy

import pytest

QtCore = pytest.importorskip("PySide6.QtCore")
QtTest = pytest.importorskip("PySide6.QtTest")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from profiler_ai.gui.report_view import ExperimentsView, ReportView


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def report():
    revision = "b" * 40
    reference = "artifact:" + "c" * 64
    return {
        "revisions": {"baseline": "a" * 40, "candidate": revision},
        "provenance": {
            "runs": {"baseline": r"C:\work\baseline", "candidate": r"C:\work\candidate"},
        },
        "status": "complete",
        "findings": {
            "conclusion": "inconclusive",
            "observations": [{"text": "The median doubled.", "evidence": [reference, reference]}],
            "confidence_limits": "Three samples per revision.",
            "experiments": [
                {
                    "kind": "diagnostic",
                    "hypothesis": "The extra copy allocates.",
                    "rationale": "Track allocations.",
                    "revision": revision,
                    "workload": "Encode",
                    "command_or_patch": "go test -bench Encode -benchmem\n",
                    "collection_settings": "Five repetitions",
                    "controls": "Same fixture and machine",
                    "budget": "Five minutes",
                    "permission": "Run benchmark",
                    "interpretation": "More bytes per operation would support the hypothesis.",
                }
            ],
        },
        "benchmarks": {
            "comparisons": [
                {
                    "benchmark": "BenchmarkEncode",
                    "unit": "ns/op",
                    "baseline": {"median": 11, "sample_count": 3},
                    "candidate": {"median": 22, "sample_count": 3},
                    "relative_change_percent": 100,
                    "direction": "increased",
                    "better": "lower",
                    "status": "comparable",
                }
            ]
        },
        "citations": {
            reference: {
                "kind": "artifact",
                "path": r"C:\work\candidate\bench.txt",
                "locator": "line:12",
            }
        },
    }


def button(view, text):
    return next(child for child in view.findChildren(QtWidgets.QPushButton) if child.text() == text)


def test_report_previews_and_pins_exact_evidence_and_measurement(qt_app, report):
    view = ReportView()
    view.set_report(report)
    view.resize(480, 750)
    view.show()
    view.activateWindow()
    assert QtTest.QTest.qWaitForWindowActive(view)
    reference = next(iter(report["citations"]))
    citations = view.findChildren(QtWidgets.QPushButton)
    links = [link for link in citations if link.text() == "12"]
    assert len(links) == 1
    (link,) = links
    assert link.accessibleName() == "Candidate · bench.txt · Line 12"
    assert all(reference not in label.text() for label in view.findChildren(QtWidgets.QLabel))

    preview = QtTest.QSignalSpy(view.previewRequested)
    dismissal = QtTest.QSignalSpy(view.previewDismissed)
    pin = QtTest.QSignalSpy(view.evidenceRequested)
    button(view, "View proposed experiments  →").setFocus()
    link.setFocus(QtCore.Qt.FocusReason.TabFocusReason)
    assert preview.wait(900)
    assert preview.at(0) == [
        reference,
        link.mapToGlobal(QtCore.QPoint(link.width() // 2, link.height())),
    ]
    QtTest.QTest.keyClick(link, QtCore.Qt.Key.Key_Return)
    assert pin.count() == 1 and pin.at(0) == [reference]
    assert dismissal.count() == 1

    measurement = button(view, "+100%  ›")
    drilldown = QtTest.QSignalSpy(view.measurementRequested)
    measurement_preview = QtTest.QSignalSpy(view.measurementPreviewRequested)
    measurement.setFocus(QtCore.Qt.FocusReason.TabFocusReason)
    assert measurement_preview.wait(900)
    assert measurement_preview.at(0)[0] == 0
    measurement.click()
    assert drilldown.count() == 1 and drilldown.at(0) == [0]
    assert dismissal.count() == 2

    QtTest.QTest.mouseMove(view, QtCore.QPoint(5, 5))
    QtTest.QTest.mouseMove(link, link.rect().center())
    assert preview.wait(900)
    assert preview.at(1)[0] == reference
    QtTest.QTest.mouseMove(view, QtCore.QPoint(5, 5))
    if dismissal.count() < 3:
        assert dismissal.wait(900)
    assert dismissal.count() == 3
    view.close()


def test_large_citation_group_discloses_remaining_lines(qt_app, report):
    claim = report["findings"]["observations"][0]
    (reference,) = report["citations"]
    original = report["citations"][reference]
    for number in range(13, 19):
        ref = f"artifact:additional-{number}"
        report["citations"][ref] = {**original, "locator": f"line:{number}"}
        claim["evidence"].append(ref)
    baseline = "artifact:baseline"
    report["citations"][baseline] = {
        **original,
        "path": r"C:\work\baseline\bench.txt",
        "locator": "line:5",
    }
    claim["evidence"].append(baseline)
    view = ReportView()
    view.set_report(report)
    view.resize(480, 700)
    view.show()
    qt_app.processEvents()
    assert button(view, "5").accessibleName() == "Baseline · bench.txt · Line 5"
    assert button(view, "15").isVisible()
    assert not any(child.text() == "18" for child in view.findChildren(QtWidgets.QPushButton))
    disclosure = button(view, "+3")
    assert disclosure.accessibleName() == "Show 3 more citations for Candidate · bench.txt"
    QtTest.QTest.keyClick(disclosure, QtCore.Qt.Key.Key_Space)
    extra = button(view, "18")
    pin = QtTest.QSignalSpy(view.evidenceRequested)
    assert extra.isVisible()
    extra.click()
    assert pin.at(0) == ["artifact:additional-18"]
    disclosure.click()
    assert not extra.isVisible()
    view.close()


def test_report_section_scroll_survives_view_changes_and_refresh(qt_app, report):
    first_row = report["benchmarks"]["comparisons"][0]
    report["benchmarks"]["comparisons"] *= 20
    report["gaps"] = ["Candidate counter capture is missing."]
    view = ReportView()
    view.set_report(report)
    view.resize(480, 330)
    view.show()
    scroll = view.findChild(QtWidgets.QScrollArea).verticalScrollBar()

    view.jump("measurements")
    qt_app.processEvents()
    assert 0 < scroll.value() < scroll.maximum()
    scroll.setValue(scroll.value() + 100)
    position = scroll.value()
    view.hide()
    updated = deepcopy(report)
    updated["gaps"].append("The counter capture needs more repetitions.")
    view.set_report(updated)
    view.show()
    qt_app.processEvents()
    assert scroll.value() == position

    view.jump("gaps")
    qt_app.processEvents()
    assert scroll.value() == scroll.maximum()
    new_report = deepcopy(updated)
    new_report["revisions"]["candidate"] = "d" * 40
    new_report["benchmarks"]["comparisons"] = [first_row]
    view.set_report(new_report)
    qt_app.processEvents()
    assert scroll.value() == 0
    view.close()


def test_experiment_expands_and_copies_original_command(qt_app, report):
    view = ExperimentsView()
    view.set_report(report)
    view.resize(480, 650)
    view.show()
    qt_app.processEvents()
    details = view.findChild(QtWidgets.QPlainTextEdit, "experimentCommand")
    assert details.isHidden() or not details.isVisible()

    button(view, "Show details").click()
    assert details.isVisible() and details.isReadOnly()
    button(view, "Copy").click()
    command = report["findings"]["experiments"][0]["command_or_patch"]
    assert qt_app.clipboard().text() == command
    assert button(view, "Copied").isVisible()
    assert any(
        label.text() == "Five repetitions" and label.isVisible()
        for label in view.findChildren(QtWidgets.QLabel)
    )

    refreshed = deepcopy(report)
    refreshed["findings"]["experiments"][0]["budget"] = "Ten minutes"
    view.set_report(refreshed)
    assert button(view, "Hide details").isChecked()
    view.close()
