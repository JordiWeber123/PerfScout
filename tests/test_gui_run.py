"""Exercise dialog validation and the subprocess protocol without a live agent."""

import json
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog

from profiler_ai.cli import main
from profiler_ai.gui.run_view import RunDialog
from profiler_ai.gui.worker import RunController

pytest_plugins = ["test_investigation"]


def app():
    return QApplication.instance() or QApplication([])


def test_dialog_prefills_and_validates(measured, tmp_path):
    application = app()
    dialog = RunDialog()
    dialog.repo.setText(str(tmp_path / "fixture"))
    dialog.baseline_run.setText(str(tmp_path / "baseline"))
    dialog.candidate_run.setText(str(tmp_path / "candidate"))
    dialog._prefill(dialog.baseline_run, dialog.base)
    dialog._prefill(dialog.candidate_run, dialog.candidate)
    assert (
        dialog.base.text()
        == json.loads((tmp_path / "baseline/manifest.json").read_text())["revision"]
    )
    dialog.output.setText(str(tmp_path / "fixture/forbidden"))
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "outside" in dialog.error.text()
    dialog.output.setText(str(tmp_path / "result"))
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.arguments()[-2:] == ["--agent", "codex"]
    assert application is not None


def test_reselect_refreshes_prefill_but_keeps_manual_revision(measured, tmp_path):
    application = app()
    dialog = RunDialog()
    dialog.repo.setText(str(tmp_path / "fixture"))
    dialog.baseline_run.setText(str(tmp_path / "baseline"))
    dialog._prefill(dialog.baseline_run, dialog.base)
    first = dialog.base.text()
    assert dialog.base_commit.text() == first[:12]
    dialog.baseline_run.setText(str(tmp_path / "candidate"))
    dialog._prefill(dialog.baseline_run, dialog.base)
    assert dialog.base.text() != first
    dialog.base.setText("HEAD~")
    dialog.base.textEdited.emit("HEAD~")
    dialog.baseline_run.setText(str(tmp_path / "baseline"))
    dialog._prefill(dialog.baseline_run, dialog.base)
    assert dialog.base.text() == "HEAD~"
    dialog.agent.setCurrentIndex(1)
    assert dialog.arguments()[-2:] == ["--agent", "none"]
    assert application is not None


def test_output_browse_proposes_new_child(tmp_path, monkeypatch):
    application = app()
    dialog = RunDialog()
    monkeypatch.setattr(
        "profiler_ai.gui.run_view.QFileDialog.getExistingDirectory",
        lambda *args: str(tmp_path),
    )
    dialog._browse(dialog.output)
    proposed = Path(dialog.output.text())
    assert proposed.parent == tmp_path
    assert proposed.name.startswith("investigation-")
    assert not proposed.exists()
    assert application is not None


def test_worker_emits_checkpoint_and_finishes_with_provenance(measured, tmp_path):
    application = app()
    controller = RunController()
    checkpoints, progress, results = [], [], []
    controller.checkpoint.connect(checkpoints.append)
    controller.progress.connect(progress.append)
    controller.finished.connect(lambda *result: results.append(result))
    output = tmp_path / "results"
    controller.start([*measured, "--output", str(output), "--agent", "none"])
    deadline = time.monotonic() + 30
    while not results and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.01)
    assert results == [(str(output), "measurements_only", "")]
    assert checkpoints == [str(output)]
    assert progress and not controller.active
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    source = report["provenance"]["profiles"]["candidate/1"]
    assert Path(source["file"]) == tmp_path / "candidate/papi.json"
    assert source["side"] == "candidate" and len(source["digest"]) == 64


def test_cancel_before_process_starts_reaps_worker(measured):
    application = app()
    controller = RunController()
    results = []
    controller.finished.connect(lambda *result: results.append(result))
    controller.start([*measured, "--agent", "none"])
    controller.cancel()
    deadline = time.monotonic() + 30
    while not results and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.01)
    assert results and results[0][1] == "cancelled"
    assert not controller.active


def test_agent_failure_preserves_announced_checkpoint(measured, tmp_path):
    class FailingProvider:
        def investigate(self, catalog, query):
            raise RuntimeError("offline failure")

    output = tmp_path / "failure"
    events = []
    assert (
        main(
            [*measured, "--output", str(output)],
            provider=FailingProvider(),
            progress=lambda *event: events.append(event),
        )
        == 1
    )
    assert ("checkpoint", str(output)) in events
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "agent_failed"
    assert report["benchmarks"]["comparisons"]
    assert "offline failure" in report["error"]
