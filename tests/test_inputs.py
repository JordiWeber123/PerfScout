import json
import subprocess
from pathlib import Path

import pytest

from profiler_ai.inputs import load_run
from profiler_ai.repository import Repository


@pytest.fixture
def run_directory(tmp_path):
    directory = tmp_path / "run"
    directory.mkdir()
    (directory / "benchmark.txt").write_text("BenchmarkEncoder-4 5 20 ns/op\n", encoding="utf-8")
    (directory / "execution.log").write_text("completed\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "revision": "a" * 40,
        "benchmark": {
            "adapter": "go-test",
            "package": "./internal/encoder",
            "selector": "^BenchmarkEncoder$",
            "results": "benchmark.txt",
            "fixture_sha256": "B" * 64,
        },
        "build": {"go_flags": ["-trimpath"]},
        "collection": {"count": 5},
        "environment": {"gomaxprocs": 4, "os": "linux"},
        "workload": {"dataset": "large"},
        "logs": ["execution.log"],
        "profiles": [
            {"kind": "cpu", "format": "pprof", "file": "profiles/cpu.pb.gz", "optional": True}
        ],
        "operator_note": "Synthetic fixture",
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory, manifest


def test_load_run_preserves_collection_metadata(run_directory):
    directory, manifest = run_directory
    run = load_run(directory, manifest["revision"].upper())

    assert run.manifest_path == directory / "manifest.json"
    assert run.artifact(run.manifest.benchmark.results) == directory / "benchmark.txt"
    assert run.manifest.benchmark.fixture_sha256 == "b" * 64
    assert run.manifest.build == {"go_flags": ["-trimpath"]}
    assert run.manifest.collection == {"count": 5}
    assert run.manifest.logs == ["execution.log"]
    assert run.manifest.model_dump()["operator_note"] == "Synthetic fixture"
    with pytest.raises(FileNotFoundError, match="profiles/cpu.pb.gz"):
        run.artifact(run.manifest.profiles[0].file)


def test_run_rejects_revision_mismatch_and_invalid_profiles(run_directory):
    directory, manifest = run_directory
    with pytest.raises(ValueError, match="records revision .* expected"):
        load_run(directory, "b" * 40)

    manifest["profiles"][0]["optional"] = False
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="profiles/cpu.pb.gz"):
        load_run(directory, manifest["revision"])


@pytest.mark.parametrize("path", ["../benchmark.txt", r"a\..\log", "C:outside", "//host/file"])
def test_run_rejects_unsafe_artifact_paths(run_directory, path):
    directory, manifest = run_directory
    manifest["profiles"][0]["file"] = path
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid relative artifact path"):
        load_run(directory, manifest["revision"])


def test_run_rejects_symlinks_that_escape_the_run_directory(run_directory):
    directory, manifest = run_directory
    outside = directory.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        (directory / "linked.txt").symlink_to(outside)
    except OSError:
        pytest.skip("creating symlinks requires filesystem support")

    run = load_run(directory, manifest["revision"])
    with pytest.raises(ValueError, match="escapes the run directory"):
        run.artifact("linked.txt")


def test_run_rejects_ambiguous_yaml_fields(tmp_path):
    (tmp_path / "manifest.yaml").write_text(
        "schema_version: 1\nschema_version: 2\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="manifest keys must be unique strings"):
        load_run(tmp_path, "a" * 40)


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Profiler Fixture",
            "-c",
            "user.email=profiler@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        text=True,
        encoding="utf-8",
    ).strip()


def test_repository_reads_commits_without_mutating_checkout(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    src = tmp_path / "encode.go"
    other = tmp_path / "extra.txt"
    src.write_bytes(b"package encoder\nconst size = 1\n")
    other.write_bytes(b"baseline\n")
    _git(tmp_path, "add", "--", "encode.go", "extra.txt")
    _git(tmp_path, "commit", "-qm", "baseline")
    src.write_bytes(b"package encoder\nconst size = 2\n")
    other.write_bytes(b"candidate\n")
    _git(tmp_path, "commit", "-qam", "candidate")
    src.write_bytes(b"staged\n")
    _git(tmp_path, "add", "--", "encode.go")
    src.write_bytes(b"unstaged\n")
    (tmp_path / "untracked.txt").write_text("preserve", encoding="utf-8")
    (tmp_path / ".gitattributes").write_text("*.go -diff\n", encoding="utf-8")
    _git(tmp_path, "add", "--", ".gitattributes")
    head = _git(tmp_path, "rev-parse", "HEAD")
    prior_status = _git(tmp_path, "status", "--porcelain")
    prior_index = (tmp_path / ".git" / "index").read_bytes()

    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing.git"))
    repo = Repository(tmp_path)
    base, candidate = repo.resolve("HEAD~1"), repo.resolve("HEAD")
    assert candidate == head
    assert repo.source(base, "encode.go", 2, 5) == "const size = 1\n"
    assert repo.source(candidate, "encode.go", 2, 2) == "const size = 2\n"
    diff = repo.diff(base, candidate, ["encode.go"])
    assert "-const size = 1\n+const size = 2\n" in diff
    assert "extra.txt" not in diff
    with pytest.raises(ValueError, match="invalid repository-relative Git path"):
        repo.source(candidate, "../other.txt", 1, 1)
    with pytest.raises(ValueError, match="Git rev-parse failed"):
        repo.resolve("--help")

    monkeypatch.delenv("GIT_DIR")
    assert _git(tmp_path, "rev-parse", "HEAD") == head
    assert (tmp_path / ".git" / "index").read_bytes() == prior_index
    assert _git(tmp_path, "status", "--porcelain") == prior_status
    assert src.read_bytes() == b"unstaged\n"
