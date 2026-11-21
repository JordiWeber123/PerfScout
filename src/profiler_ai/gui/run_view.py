"""Dialog for configuring an investigation of existing run bundles."""

from pathlib import Path
from uuid import uuid4

import yaml
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..cli import validate_output_path
from ..inputs import _ManifestLoader, load_run
from ..repository import Repository


class RunDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New investigation")
        self.resize(780, 580)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 26)
        layout.setSpacing(16)
        title = QLabel("New investigation")
        title.setStyleSheet("font-size: 22px; font-weight: 650;")
        layout.addWidget(title)
        subtitle = QLabel("Compare two existing measurement runs from a Git repository.")
        subtitle.setStyleSheet("color: #b1b0ac;")
        layout.addWidget(subtitle)
        form = QFormLayout()
        form.setVerticalSpacing(14)
        form.setHorizontalSpacing(18)
        layout.addLayout(form)
        self.repo = QLineEdit()
        self.base = QLineEdit()
        self.candidate = QLineEdit()
        self.baseline_run = QLineEdit()
        self.candidate_run = QLineEdit()
        self.output = QLineEdit()
        self.agent = QComboBox()
        self.agent.addItem("Codex", "codex")
        self.agent.addItem("Measurements only", "none")
        self._manual_revision = {self.base: False, self.candidate: False}
        self.base_commit = QLabel()
        self.candidate_commit = QLabel()
        self.base_commit.setStyleSheet("color: #ff921f;")
        self.candidate_commit.setStyleSheet("color: #ff921f;")
        for label, field, browse in [
            ("Repository", self.repo, True),
            ("Baseline run", self.baseline_run, True),
            ("Baseline revision", self.base, False),
            ("Candidate run", self.candidate_run, True),
            ("Candidate revision", self.candidate, False),
            ("Output directory (optional)", self.output, True),
        ]:
            if browse:
                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.addWidget(field)
                button = QPushButton("Browse…")
                button.clicked.connect(lambda _checked=False, f=field: self._browse(f))
                row_layout.addWidget(button)
                form.addRow(label, row)
            else:
                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.addWidget(field)
                row_layout.addWidget(
                    self.base_commit if field is self.base else self.candidate_commit
                )
                form.addRow(label, row)
        form.addRow("Agent", self.agent)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #f0a98f;")
        layout.addWidget(self.error)
        layout.addStretch()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        run_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        run_button.setText("Run investigation")
        run_button.setObjectName("primary")
        layout.addWidget(buttons)
        self.baseline_run.editingFinished.connect(
            lambda: self._prefill(self.baseline_run, self.base)
        )
        self.candidate_run.editingFinished.connect(
            lambda: self._prefill(self.candidate_run, self.candidate)
        )
        for revision in (self.base, self.candidate):
            revision.textEdited.connect(lambda _text, r=revision: self._mark_manual(r))
            revision.editingFinished.connect(lambda r=revision: self._show_commit(r))
        self.repo.editingFinished.connect(self._show_commits)

    def _browse(self, field):
        initial = field.text()
        if field is self.output and initial:
            initial = str(Path(initial).parent)
        selected = QFileDialog.getExistingDirectory(self, "Choose directory", initial)
        if selected:
            field.setText(
                str(Path(selected) / f"investigation-{uuid4().hex[:8]}")
                if field is self.output
                else selected
            )
            if field is self.baseline_run:
                self._prefill(field, self.base)
            elif field is self.candidate_run:
                self._prefill(field, self.candidate)

    def _prefill(self, field, revision):
        if self._manual_revision[revision] and revision.text().strip():
            return
        revision.clear()
        self._show_commit(revision)
        try:
            directory = Path(field.text())
            manifests = [
                directory / name
                for name in ("manifest.yaml", "manifest.yml", "manifest.json")
                if (directory / name).is_file()
            ]
            if len(manifests) == 1 and manifests[0].stat().st_size <= 1024 * 1024:
                data = yaml.load(
                    manifests[0].read_text(encoding="utf-8-sig"), Loader=_ManifestLoader
                )
                value = data.get("revision") if isinstance(data, dict) else None
                if isinstance(value, str):
                    load_run(directory, value)
                    revision.setText(value)
                    self._manual_revision[revision] = False
                    self._show_commit(revision)
        except (OSError, ValueError, yaml.YAMLError):
            pass

    def _mark_manual(self, revision):
        self._manual_revision[revision] = bool(revision.text().strip())

    def _show_commit(self, revision):
        label = self.base_commit if revision is self.base else self.candidate_commit
        try:
            if not self.repo.text().strip() or not revision.text().strip():
                label.clear()
                return
            sha = Repository(Path(self.repo.text().strip())).resolve(revision.text().strip())
            label.setText(sha[:12])
            label.setToolTip(sha)
        except (OSError, ValueError):
            label.clear()

    def _show_commits(self):
        self._show_commit(self.base)
        self._show_commit(self.candidate)

    def arguments(self):
        args = ["investigate"]
        for name, field in [
            ("repo", self.repo),
            ("base", self.base),
            ("candidate", self.candidate),
            ("baseline-run", self.baseline_run),
            ("candidate-run", self.candidate_run),
        ]:
            args.extend([f"--{name}", field.text().strip()])
        if self.output.text().strip():
            args.extend(["--output", self.output.text().strip()])
        return [*args, "--agent", self.agent.currentData()]

    def accept(self):
        try:
            if not all(
                field.text().strip()
                for field in (
                    self.repo,
                    self.base,
                    self.candidate,
                    self.baseline_run,
                    self.candidate_run,
                )
            ):
                raise ValueError("Complete the repository, revisions, and run directories.")
            repo = Repository(Path(self.repo.text().strip()))
            runs = [
                load_run(Path(field.text().strip()), repo.resolve(revision.text().strip()))
                for field, revision in (
                    (self.baseline_run, self.base),
                    (self.candidate_run, self.candidate),
                )
            ]
            if self.output.text().strip():
                validate_output_path(
                    Path(self.output.text().strip()), [repo.root, *(run.directory for run in runs)]
                )
        except (OSError, ValueError) as exc:
            self.error.setText(str(exc))
            return
        self.error.clear()
        self._show_commits()
        super().accept()
