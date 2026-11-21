"""Exercise the installed CLI and the same investigation with an offline provider."""

import hashlib
import json
import os
import shutil
import subprocess
import sysconfig
from pathlib import Path

import pytest

from profiler_ai.cli import main
from profiler_ai.evidence import EvidenceIndex
from profiler_ai.report import Findings


def git(repo, *args):
    return subprocess.check_output(
        [
            "git",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Fixture",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(repo),
            *args,
        ],
        text=True,
    ).strip()


@pytest.fixture
def measured(tmp_path):
    repo = tmp_path / "fixture"
    repo.mkdir()
    git(repo, "init", "-q")
    source = repo / "encoder.py"
    for side, text, samples in [
        ("baseline", "return payload", [10, 11, 12]),
        ("candidate", "return bytes(bytearray(payload))", [20, 22, 24]),
    ]:
        source.write_text(f"def encode(payload):\n    {text}\n", encoding="utf-8")
        git(repo, "add", "encoder.py")
        git(repo, "commit", "-qm", side)
        run = tmp_path / side
        run.mkdir()
        (run / "bench.txt").write_text(
            "".join(f"BenchmarkEncode-4 100 {sample} ns/op\n" for sample in samples),
            encoding="utf-8",
        )
        shutil.copyfile(
            Path(__file__).parent / "fixtures/profiles/papi-capture.json", run / "papi.json"
        )
        manifest = {
            "schema_version": 1,
            "revision": git(repo, "rev-parse", "HEAD"),
            "benchmark": {"adapter": "go-test", "results": "bench.txt"},
            "profiles": [
                {"format": "pprof", "kind": "cpu", "file": "missing.pb", "optional": True},
                {"format": "papi-hl-json", "kind": "counters", "file": "papi.json"},
            ],
        }
        (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    source.write_text("staged unrelated work\n", encoding="utf-8")
    git(repo, "add", "encoder.py")
    source.write_text("unstaged unrelated work\n", encoding="utf-8")
    (repo / "scratch").write_text("untracked", encoding="utf-8")
    return [
        "investigate",
        "--repo",
        str(repo),
        "--base",
        "HEAD~",
        "--candidate",
        "HEAD",
        "--baseline-run",
        str(tmp_path / "baseline"),
        "--candidate-run",
        str(tmp_path / "candidate"),
    ]


class ScriptedAgent:
    def investigate(self, catalog, query):
        (row,) = query("benchmarks", {})["rows"]
        assert row["relative_change_percent"] == 100
        assert row["baseline"]["sample_count"] == 3
        assert row["candidate"]["median"] == 22
        sample = query("samples", {"row": 0, "side": "candidate", "limit": 1})["rows"][0]
        source = query("source", {"side": "candidate", "path": "encoder.py", "start": 2, "end": 2})
        diff = query("diff", {"paths": ["encoder.py"]})
        assert "bytearray(payload)" in source["text"] and "+    return bytes" in diff["text"]
        assert any("missing.pb" in gap for gap in catalog["gaps"])
        profile = query("describe_profile", {"profile": "candidate/1"})
        assert profile["thread_count"] == 1
        (counter,) = query(
            "counters", {"profile": "candidate/1", "thread": "0", "event": "PAPI_TOT_INS"}
        )["rows"]
        assert counter["value"] == 33_007_026_164
        return Findings.model_validate(
            {
                "conclusion": "inconclusive",
                "observations": [
                    {
                        "text": "Median time doubled.",
                        "evidence": [*row["baseline"]["evidence"], sample["evidence"]],
                    }
                ],
                "hypotheses": [
                    {
                        "text": "A new copy may account for part of the cost.",
                        "evidence": [source["evidence"], diff["evidence"]],
                    }
                ],
                "alternatives": ["Run-to-run variation"],
                "missing_evidence": ["Allocation samples"],
                "confidence_limits": "Three observations per revision.",
                "experiments": [
                    {
                        "kind": "diagnostic",
                        "hypothesis": "The extra copy allocates.",
                        "revision": catalog["revisions"]["candidate"],
                        "rationale": "Locate allocations.",
                        "workload": "Encode",
                        "command_or_patch": "go test -bench Encode -benchmem",
                        "controls": "Same fixture and environment",
                        "budget": "Five minutes",
                        "permission": "Run benchmark",
                        "collection_settings": "Five repetitions",
                        "interpretation": "Higher bytes/op would support the hypothesis.",
                    }
                ],
            }
        )


def snapshot(repo):
    return (
        git(repo, "rev-parse", "HEAD"),
        (repo / ".git/index").read_bytes(),
        (repo / "encoder.py").read_bytes(),
        (repo / "scratch").read_bytes(),
    )


def test_scripted_agent_resolves_citations_against_committed_evidence(tmp_path, measured):
    out = tmp_path / "results"
    repo = tmp_path / "fixture"
    before = snapshot(repo)
    assert main([*measured, "--output", str(out)], provider=ScriptedAgent()) == 0
    assert snapshot(repo) == before
    report = json.loads((out / "report.json").read_text("utf-8"))
    assert report["status"] == "complete"
    index = EvidenceIndex(out / "evidence.sqlite3")
    try:
        for ref, citation in report["citations"].items():
            assert index.lookup(ref) == citation
            if citation["kind"] == "artifact":
                raw = Path(citation["path"]).read_bytes()
                assert hashlib.sha256(raw).hexdigest() == citation["digest"]
                if citation["locator"].startswith("line:"):
                    assert raw.splitlines()[int(citation["locator"][5:]) - 1].startswith(
                        b"Benchmark"
                    )
            else:
                assert citation["revision"] == git(repo, "rev-parse", "HEAD")
                assert "encoder.py" in citation["locator"]
    finally:
        index.close()
    assert "Median time doubled." in (out / "report.md").read_text("utf-8")


def test_installed_command_and_output_isolation(tmp_path, measured):
    program = shutil.which("perfscout", path=sysconfig.get_path("scripts"))
    assert program is not None
    out = tmp_path / "measurements"
    subprocess.run([program, *measured, "--output", str(out), "--agent", "none"], check=True)
    report = json.loads((out / "report.json").read_text("utf-8"))
    assert report["status"] == "measurements_only"
    assert report["benchmarks"]["comparisons"][0]["relative_change_percent"] == 100
    assert main([*measured, "--output", str(tmp_path / "fixture/forbidden")]) == 2
    assert not (tmp_path / "fixture/forbidden").exists()


def test_unknown_agent_citation_keeps_benchmark_results(tmp_path, measured):
    class BadCitation(ScriptedAgent):
        def investigate(self, catalog, query):
            result = super().investigate(catalog, query)
            result.observations[0].evidence = ["invented"]
            return result

    out = tmp_path / "failure"
    assert main([*measured, "--output", str(out)], provider=BadCitation()) == 1
    report = json.loads((out / "report.json").read_text("utf-8"))
    assert report["status"] == "agent_failed" and "unknown evidence" in report["error"]
    assert report["benchmarks"]["comparisons"] and report["findings"] is None


@pytest.mark.skipif(os.getenv("PROFILER_AI_LIVE_CODEX") != "1", reason="opt-in live Codex session")
def test_live_codex_investigation(tmp_path, measured):
    out = tmp_path / "live"
    assert main([*measured, "--output", str(out)]) == 0
    assert json.loads((out / "report.json").read_text("utf-8"))["findings"]["experiments"]
