import hashlib
import json

import pytest

from profiler_ai.evidence import EvidenceIndex


def test_artifact_citations_resolve_after_reopening_index(tmp_path):
    baseline = tmp_path / "baseline.txt"
    candidate = tmp_path / "candidate.txt"
    data = b"BenchmarkEncoder-4 5 20 ns/op\n"
    baseline.write_bytes(data)
    candidate.write_bytes(data)
    db_path = tmp_path / "evidence.sqlite3"
    index = EvidenceIndex(db_path)
    try:
        digest = index.register(baseline)
        assert digest == hashlib.sha256(data).hexdigest()
        assert index.register(candidate) == digest
        reference = index.cite(digest, "line:1")
        assert index.cite(digest, "line:1") == reference
        assert index.cite(digest, "line:2") != reference
        with pytest.raises(ValueError, match="unregistered artifact digest"):
            index.cite("1" * 64, "line:1")
    finally:
        index.close()

    index = EvidenceIndex(db_path)
    try:
        found = index.lookup(reference)
        assert found == {
            "reference": reference,
            "kind": "artifact",
            "digest": digest,
            "path": str(baseline),
            "paths": [str(baseline), str(candidate)],
            "size_bytes": len(data),
            "locator": "line:1",
        }
        assert index.lookup("unknown") is None
        assert baseline.read_bytes() == candidate.read_bytes() == data
    finally:
        index.close()


def test_git_evidence_preserves_revision_and_locator(tmp_path):
    db = EvidenceIndex(tmp_path / "evidence.sqlite3")
    revision = "a" * 40
    locator = json.dumps({"path": "src/encode.go", "start": 4, "end": 10})
    try:
        reference = db.record_git(tmp_path, revision.upper(), locator)
        assert db.record_git(tmp_path, revision, locator) == reference
        assert db.record_git(tmp_path, "b" * 40, locator) != reference
        assert db.lookup(reference) == {
            "reference": reference,
            "kind": "git",
            "repository": str(tmp_path),
            "revision": revision,
            "locator": locator,
        }
    finally:
        db.close()
