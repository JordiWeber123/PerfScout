"""Resolve saved citations and browse committed evidence without modifying the inputs."""

import json
import os
import time

import pytest
import test_investigation
from test_investigation import ScriptedAgent, git, snapshot
from test_profiles import _pprof_profile

from profiler_ai.cli import main
from profiler_ai.gui.reader import Reader
from profiler_ai.report import Claim

measured = test_investigation.measured


@pytest.fixture
def dossier(tmp_path, measured):
    for side in ("baseline", "candidate"):
        run = tmp_path / side
        (run / "cpu.pprof").write_bytes(_pprof_profile())
        path = run / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["profiles"].append({"format": "pprof", "kind": "cpu", "file": "cpu.pprof"})
        path.write_text(json.dumps(manifest), encoding="utf-8")

    class ProfileAgent(ScriptedAgent):
        def investigate(self, catalog, query):
            findings = super().investigate(catalog, query)
            (counter,) = query(
                "counters",
                {"profile": "candidate/1", "thread": "0", "event": "PAPI_TOT_INS"},
            )["rows"]
            (hotspot,) = query("hotspots", {"profile": "candidate/2", "limit": 1})["rows"]
            whole_diff = query("diff", {})["evidence"]
            findings.observations.append(
                Claim(
                    text="Capture evidence", evidence=[*counter["evidence"], *hotspot["evidence"]]
                )
            )
            findings.hypotheses.append(Claim(text="Revision changes", evidence=[whole_diff]))
            return findings

    output = tmp_path / "saved investigation"
    assert main([*measured, "--output", str(output)], provider=ProfileAgent()) == 0
    return Reader(output)


def test_citations_preserve_exact_values_and_provenance(tmp_path, dossier):
    checkout = tmp_path / "fixture"
    before = snapshot(checkout)
    index = dossier.directory / "evidence.sqlite3"
    raw_index = index.read_bytes()
    comparison = dossier.measurement(0)["comparison"]
    assert comparison["relative_change_percent"] == 100
    assert comparison["candidate"]["sample_count"] == 3
    reference = comparison["candidate"]["samples"][2]["evidence"]
    measurement = dossier.resolve(reference)
    assert measurement["locator"] == "line:3"
    assert measurement["text"] == "BenchmarkEncode-4 100 24 ns/op"
    assert measurement["line"] == 3 and measurement["side"] == "candidate"
    manifest_ref = next(
        ref for ref, item in dossier.data["citations"].items() if item.get("locator") == "manifest"
    )
    manifest = dossier.resolve(manifest_ref)
    assert json.loads(manifest["text"])["revision"] == dossier.data["revisions"]["baseline"]

    counter_ref, location_ref, sample_ref = dossier.data["findings"]["observations"][1]["evidence"]
    counter = dossier.resolve(counter_ref)
    assert counter["locator"].startswith("papi:#/threads/")
    assert counter["details"]["exact_value"] == "33007026164"
    assert counter["details"]["unit"] == "instructions"
    assert counter["details"]["thread"] == "0"
    location = dossier.resolve(location_ref)
    assert location["locator"] == "pprof:location/1"
    assert location["details"]["frames"][0]["source_line"] == 11
    sample = dossier.resolve(sample_ref)
    assert sample["locator"] == "pprof:sample/0"
    assert sample["details"]["weights"][1] == {
        "metric": "cpu",
        "unit": "nanoseconds",
        "value": 300,
    }

    source_ref = dossier.data["findings"]["hypotheses"][0]["evidence"][0]
    source = dossier.resolve(source_ref)
    assert source["kind"] == "source" and source["start"] == source["end"] == 2
    assert source["text"].strip() == "return bytes(bytearray(payload))"
    whole_diff = dossier.resolve(dossier.data["findings"]["hypotheses"][1]["evidence"][0])
    assert whole_diff["kind"] == "diff" and whole_diff["paths"] == []
    assert dossier.changed_files() == [("M", "encoder.py")]
    assert "-    return payload" in whole_diff["text"]
    assert "+    return bytes" in whole_diff["text"]
    assert "bytearray" not in dossier.code_source("baseline", "encoder.py")
    assert "bytearray" in dossier.code_source("candidate", "encoder.py")
    assert snapshot(checkout) == before
    assert index.read_bytes() == raw_index


@pytest.mark.parametrize("legacy", [False, True])
def test_locate_moved_inputs_and_reject_changed_artifacts(tmp_path, dossier, legacy):
    if legacy:
        report = dossier.directory / "report.json"
        data = json.loads(report.read_text(encoding="utf-8"))
        data.pop("provenance", None)
        report.write_text(json.dumps(data), encoding="utf-8")
        dossier = Reader(dossier.directory)
    source_ref = dossier.data["findings"]["hypotheses"][0]["evidence"][0]
    candidate = (tmp_path / "candidate").rename(tmp_path / "moved candidate")
    repo = (tmp_path / "fixture").rename(tmp_path / "moved checkout")
    comparison = dossier.measurement(0)["comparison"]
    sample_ref = comparison["candidate"]["samples"][2]["evidence"]
    missing = dossier.resolve(sample_ref)
    assert missing["kind"] == "gap" and missing["action"] == "run"
    assert missing["recovery_side"] == "candidate"
    assert dossier.resolve(source_ref)["action"] == "repository"
    dossier.set_repository(repo)
    dossier.set_run_directory("candidate", candidate)
    assert "bytearray(payload)" in dossier.resolve(source_ref)["text"]
    assert dossier.resolve(sample_ref)["artifact_path"] == str(candidate / "bench.txt")
    assert dossier.profile("candidate/2", "hotspots", {"metric": 1})["query"]["total_weight"] == 600
    path = candidate / "bench.txt"
    path.write_bytes(path.read_bytes().replace(b"24 ns/op", b"25 ns/op"))
    tampered = dossier.resolve(sample_ref)
    assert tampered["kind"] == "gap"
    assert "SHA-256 does not match" in tampered["text"]


def test_profile_aggregate_references_resolve_without_an_index_write(dossier):
    index = dossier.directory / "evidence.sqlite3"
    before = index.read_bytes()
    result = dossier.profile("candidate/2", "hotspots", {"metric": 1, "aggregation": "cumulative"})
    assert result["query"]["rows"][0]["function"] == "root"
    reference = result["query"]["rows"][0]["evidence"][0]
    location = dossier.resolve(reference)
    assert location["locator"] == "pprof:location/2"
    assert location["details"]["frames"][0]["function"] == "root"
    assert index.read_bytes() == before


def test_large_patch_retains_file_navigation(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "big.txt").write_text("a" * 70_000 + "\n", encoding="utf-8")
    (repo / "small.txt").write_text("before\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "baseline")
    baseline = git(repo, "rev-parse", "HEAD")
    (repo / "big.txt").write_text("b" * 70_000 + "\n", encoding="utf-8")
    (repo / "small.txt").write_text("after\n", encoding="utf-8")
    git(repo, "commit", "-qam", "candidate")
    candidate = git(repo, "rev-parse", "HEAD")
    output = tmp_path / "results"
    output.mkdir()
    (output / "report.json").write_text(
        json.dumps(
            {
                "revisions": {"baseline": baseline, "candidate": candidate},
                "benchmarks": {"comparisons": [], "gaps": []},
                "profiles": {},
                "gaps": [],
                "findings": None,
                "status": "measurements_only",
                "provenance": {"repository": str(repo)},
                "citations": {
                    "whole": {
                        "reference": "whole",
                        "kind": "git",
                        "repository": str(repo),
                        "revision": candidate,
                        "locator": json.dumps({"diff_from": baseline, "paths": []}),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    reader = Reader(output)
    assert reader.resolve("whole")["truncated"]
    assert reader.changed_files() == [("M", "big.txt"), ("M", "small.txt")]
    patch, truncated = reader.code_diff("small.txt")
    assert not truncated and "-before\n+after" in patch


def test_code_view_loads_aligned_patch_and_cited_source(dossier):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QUrl
    from PySide6.QtWidgets import QApplication

    from profiler_ai.gui.evidence_view import CodeView, EvidenceView, _diff_columns

    left, right = _diff_columns("@@ -4,3 +5,2 @@\n keep\n-old\n-removed\n+new\n")
    assert [(line.number, line.text) for line in left[1:]] == [
        (4, "keep"),
        (5, "old"),
        (6, "removed"),
    ]
    assert [(line.number, line.text) for line in right[1:]] == [(5, "keep"), (6, "new"), (None, "")]
    app = QApplication.instance() or QApplication([])
    view = CodeView()
    view.show()

    def until(predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            app.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        assert predicate()

    view.set_reader(dossier)
    view.mode.setCurrentIndex(0)
    until(lambda: "return payload" in view._left.toPlainText())
    assert "return bytes" in view._right.toPlainText()
    assert view.hunks.count() == 1
    citation = dossier.data["findings"]["hypotheses"][0]["evidence"][0]
    view.open_result(dossier.resolve(citation))
    until(lambda: "return bytes" in view._left.toPlainText())
    assert view.mode.currentText() == "Candidate source"
    assert len(view._left.extraSelections()) == 1
    view.close()

    evidence = EvidenceView()
    evidence.show_result(dossier.measurement(0))
    shown, dismissed = [], []
    evidence.previewRequested.connect(lambda reference, _: shown.append(reference))
    evidence.previewDismissed.connect(lambda: dismissed.append(True))
    reference = dossier.measurement(0)["comparison"]["candidate"]["samples"][0]["evidence"]
    assert reference in evidence.details.toHtml()
    evidence.details.highlighted.emit(QUrl(f"evidence:{reference}"))
    until(lambda: shown == [reference])
    evidence.details.highlighted.emit(QUrl())
    assert dismissed == [True]
