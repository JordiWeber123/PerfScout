"""Dialog for configuring an investigation of existing run bundles."""

from pathlib import Path

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

from ..inputs import load_run
from ..repository import Repository


class RunDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New investigation")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        self.repo = QLineEdit()
        self.base = QLineEdit()
        self.candidate = QLineEdit()
        self.baseline_run = QLineEdit()
        self.candidate_run = QLineEdit()
        self.output = QLineEdit()
        self.agent = QComboBox()
        self.agent.addItems(["codex", "none"])
        for label, field, browse in [
            ("Repository", self.repo, True),
            ("Baseline revision", self.base, False),
            ("Candidate revision", self.candidate, False),
            ("Baseline run", self.baseline_run, True),
            ("Candidate run", self.candidate_run, True),
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
                form.addRow(label, field)
        form.addRow("Agent", self.agent)
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.baseline_run.editingFinished.connect(lambda: self._prefill(self.baseline_run, self.base))
        self.candidate_run.editingFinished.connect(lambda: self._prefill(self.candidate_run, self.candidate))

    def _browse(self, field):
        selected = QFileDialog.getExistingDirectory(self, "Choose directory", field.text())
        if selected:
            field.setText(selected)
            if field is self.baseline_run:
                self._prefill(field, self.base)
            elif field is self.candidate_run:
                self._prefill(field, self.candidate)

    def _prefill(self, field, revision):
        if revision.text().strip():
            return
        try:
            directory = Path(field.text())
            manifests = [directory / name for name in ("manifest.yaml", "manifest.yml", "manifest.json") if (directory / name).is_file()]
            if len(manifests) == 1 and manifests[0].stat().st_size <= 1024 * 1024:
                data = yaml.safe_load(manifests[0].read_text(encoding="utf-8-sig"))
                value = data.get("revision") if isinstance(data, dict) else None
                if isinstance(value, str):
                    load_run(directory, value)
                    revision.setText(value)
        except (OSError, ValueError, yaml.YAMLError):
            pass

    def arguments(self):
        args = ["investigate"]
        for name, field in [("repo", self.repo), ("base", self.base), ("candidate", self.candidate), ("baseline-run", self.baseline_run), ("candidate-run", self.candidate_run)]:
            args.extend([f"--{name}", field.text().strip()])
        if self.output.text().strip():
            args.extend(["--output", self.output.text().strip()])
        return [*args, "--agent", self.agent.currentText()]

    def accept(self):
        try:
            if not all(field.text().strip() for field in (self.repo, self.base, self.candidate, self.baseline_run, self.candidate_run)):
                raise ValueError("Complete the repository, revisions, and run directories.")
            repo = Repository(Path(self.repo.text().strip()))
            runs = [load_run(Path(field.text().strip()), repo.resolve(revision.text().strip())) for field, revision in ((self.baseline_run, self.base), (self.candidate_run, self.candidate))]
            if self.output.text().strip():
                output = Path(self.output.text().strip()).resolve()
                if output.exists():
                    raise ValueError("The output directory must not already exist.")
                if any(output.is_relative_to(path) for path in (repo.root, *(run.directory for run in runs))):
                    raise ValueError("Choose an output directory outside the repository and run directories.")
        except (OSError, ValueError) as exc:
            self.error.setText(str(exc))
            return
        self.error.clear()
        super().accept()
