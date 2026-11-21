"""Persistent references to artifacts and committed Git evidence."""

import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path
from typing import Any

_COMMIT = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    digest TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS artifact_paths (
    digest TEXT NOT NULL REFERENCES artifacts(digest),
    path TEXT NOT NULL,
    PRIMARY KEY (digest, path)
);
CREATE TABLE IF NOT EXISTS citations (
    reference TEXT PRIMARY KEY,
    digest TEXT REFERENCES artifacts(digest),
    locator TEXT NOT NULL,
    repository TEXT,
    revision TEXT,
    CHECK (
        (digest IS NOT NULL AND repository IS NULL AND revision IS NULL)
        OR (digest IS NULL AND repository IS NOT NULL AND revision IS NOT NULL)
    )
);
"""


def _reference(kind: str, locator: str, *identities: str) -> str:
    if not isinstance(locator, str) or not locator.strip() or len(locator) > 8192:
        raise ValueError("evidence locator must be a nonempty string of up to 8192 characters")
    data = json.dumps([*identities, locator], ensure_ascii=True, separators=(",", ":"))
    return f"{kind}:{hashlib.sha256(data.encode('ascii')).hexdigest()}"


class EvidenceIndex:
    """Register artifacts by content and look up references in a SQLite index."""

    def __init__(self, path: Path):
        self._database = sqlite3.connect(path)
        try:
            self._database.execute("PRAGMA foreign_keys = ON")
            self._database.executescript(_SCHEMA)
        except sqlite3.Error:
            self._database.close()
            raise

    def register(self, path: Path) -> str:
        """Return the SHA-256 digest of a regular file and record its path."""
        path = Path(path).resolve(strict=True)
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError(f"evidence artifact must be a regular file: {path}")
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            after = os.fstat(stream.fileno())
            current = path.stat()
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or after.st_size != current.st_size
            or after.st_mtime_ns != current.st_mtime_ns
            or not os.path.samestat(after, current)
        ):
            raise ValueError(f"evidence artifact changed while it was being read: {path}")
        with self._database:
            self._database.execute(
                "INSERT OR IGNORE INTO artifacts (digest, size_bytes) VALUES (?, ?)",
                (digest, before.st_size),
            )
            self._database.execute(
                "INSERT OR IGNORE INTO artifact_paths (digest, path) VALUES (?, ?)",
                (digest, str(path)),
            )
        return digest

    def cite(self, digest: str, locator: str) -> str:
        """Record the location of an observation within a registered artifact."""
        reference = _reference("artifact", locator, digest)
        with self._database:
            cursor = self._database.execute("SELECT 1 FROM artifacts WHERE digest = ?", (digest,))
            if cursor.fetchone() is None:
                raise ValueError(f"unregistered artifact digest: {digest!r}")
            self._database.execute(
                "INSERT OR IGNORE INTO citations (reference, digest, locator) VALUES (?, ?, ?)",
                (reference, digest, locator),
            )
        return reference

    def record_git(self, repo: Path, sha: str, locator: str) -> str:
        """Record an observation at a named location in the Git object database."""
        if not isinstance(sha, str) or not _COMMIT.fullmatch(sha):
            raise ValueError("Git evidence requires a full commit ID")
        repository = str(Path(repo).resolve(strict=True))
        revision = sha.lower()
        reference = _reference("git", locator, repository, revision)
        with self._database:
            self._database.execute(
                """
                INSERT OR IGNORE INTO citations (reference, locator, repository, revision)
                VALUES (?, ?, ?, ?)
                """,
                (reference, locator, repository, revision),
            )
        return reference

    def lookup(self, reference: str) -> dict[str, Any] | None:
        """Return a citation's provenance as JSON-compatible values, if registered."""
        row = self._database.execute(
            "SELECT digest, locator, repository, revision FROM citations WHERE reference = ?",
            (reference,),
        ).fetchone()
        if row is None:
            return None
        digest, locator, repository, revision = row
        if digest is None:
            return {
                "reference": reference,
                "kind": "git",
                "repository": repository,
                "revision": revision,
                "locator": locator,
            }

        rows = self._database.execute(
            """
            SELECT path, size_bytes FROM artifacts JOIN artifact_paths USING (digest)
            WHERE digest = ? ORDER BY path
            """,
            (digest,),
        ).fetchall()
        return {
            "reference": reference,
            "kind": "artifact",
            "digest": digest,
            "path": rows[0][0],
            "paths": [path for path, _ in rows],
            "size_bytes": rows[0][1],
            "locator": locator,
        }

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._database.close()
