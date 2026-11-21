"""Read investigation results and resolve their committed and raw evidence."""

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable
from contextlib import closing
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from threading import Lock
from typing import BinaryIO

from ..evidence import EvidenceIndex, artifact_reference
from ..profiles._common import MAX_ARTIFACT_BYTES
from ..profiles.papi import PapiProfile
from ..profiles.pprof import PprofProfile, inspect_pprof
from ..report import Report
from ..repository import Repository

_SIDES = ("baseline", "candidate")
_TEXT_LIMIT = 128 * 1024
_DIGEST = re.compile(r"[a-f0-9]{64}")
_READ_ERRORS = (OSError, ValueError, sqlite3.Error, KeyError, TypeError, RecursionError)


def _shown(value) -> str:
    return "—" if value is None else str(value)


def _json(value) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if len(text) > _TEXT_LIMIT:
        return text[:_TEXT_LIMIT] + "\n… Preview truncated"
    return text


def _gap(title: str, error, **details) -> dict:
    return {**details, "kind": "gap", "title": title, "text": str(error)}


def _portable(path: str) -> PurePath:
    windows = PureWindowsPath(path)
    return windows if windows.drive or path.startswith("\\\\") else PurePosixPath(path)


def _within(path: str, root: str) -> PurePath | None:
    candidate, parent = _portable(path), _portable(root)
    if candidate.is_relative_to(parent):
        relative = candidate.relative_to(parent)
        if relative.parts and ".." not in relative.parts:
            return relative
    return None


def _text_preview(stream: BinaryIO) -> str:
    data = stream.read(_TEXT_LIMIT + 1)
    text = data[:_TEXT_LIMIT].decode("utf-8-sig", errors="replace")
    return text + ("\n… Preview truncated" if len(data) > _TEXT_LIMIT else "")


def _profile_bytes(stream: BinaryIO) -> bytes:
    data = stream.read(MAX_ARTIFACT_BYTES + 1)
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ValueError("Profile exceeds the 32 MiB artifact limit.")
    return data


def _line_preview(stream: BinaryIO, number: int) -> dict:
    """Read a cited line with three context lines on each side."""
    lines = []
    for index in range(1, number + 4):
        chunk = stream.readline(16 * 1024)
        if not chunk:
            break
        truncated = len(chunk) == 16 * 1024 and not chunk.endswith(b"\n")
        if index >= number - 3:
            text = chunk.decode("utf-8-sig" if index == 1 else "utf-8", errors="replace")
            lines.append((index, text.rstrip("\r\n") + ("…" if truncated else "")))
        if truncated:
            while chunk and not chunk.endswith(b"\n"):
                chunk = stream.readline(16 * 1024)
    for index, text in lines:
        if index == number:
            return {"text": text, "lines": lines, "line": number}
    raise ValueError(f"Line {number} is past the end of the artifact.")


class Reader:
    """An opened investigation. Each query can run on a separate worker thread."""

    def __init__(self, directory: Path):
        self.directory = Path(directory).resolve(strict=True)
        self.data = Report.model_validate_json(
            (self.directory / "report.json").read_bytes()
        ).model_dump()
        self._lock = Lock()
        self._recovered_runs: dict[str, Path] = {}
        self._preview_citations: dict[str, dict] = {}
        provenance = self.data.get("provenance") or {}
        self._profiles = provenance.get("profiles") or {}
        self._recorded_runs = {
            side: str(path)
            for side, path in (provenance.get("runs") or {}).items()
            if side in _SIDES and isinstance(path, str) and path
        }
        manifest_paths = [
            _portable(item["path"]).parent
            for item in self.data["citations"].values()
            if item.get("locator") == "manifest" and item.get("path")
        ]
        for side, path in zip(_SIDES, manifest_paths, strict=False):
            self._recorded_runs.setdefault(side, str(path))
        self._repository_path = provenance.get("repository") or next(
            (
                item["repository"]
                for item in self.data["citations"].values()
                if item.get("repository")
            ),
            None,
        )
        self._repository: Repository | None = None

    def set_repository(self, path: Path) -> None:
        repository = Repository(path)
        for revision in self.data["revisions"].values():
            repository.resolve(revision)
        with self._lock:
            self._repository_path = str(repository.root)
            self._repository = repository

    def set_run_directory(self, side: str, path: Path) -> None:
        if side not in _SIDES:
            raise ValueError("Choose Baseline or Candidate for the run directory.")
        directory = Path(path).resolve(strict=True)
        if not directory.is_dir():
            raise ValueError(f"Run path is not a directory: {directory}")
        with self._lock:
            self._recovered_runs[side] = directory

    def _repo(self) -> Repository:
        with self._lock:
            repository, path = self._repository, self._repository_path
        if repository is not None:
            return repository
        if not path:
            raise ValueError("Locate the repository containing the measured commits.")
        try:
            repository = Repository(path)
        except (OSError, ValueError) as exc:
            raise ValueError(f"Repository unavailable at {path}.\n{exc}") from exc
        with self._lock:
            if self._repository_path == path:
                self._repository = repository
        return repository

    def _lookup(self, reference: str) -> dict | None:
        if reference in self.data["citations"]:
            return self.data["citations"][reference]
        with self._lock:
            preview = self._preview_citations.get(reference)
        if preview is not None:
            return preview
        database = self.directory / "evidence.sqlite3"
        if database.is_file():
            with closing(EvidenceIndex(database, read_only=True)) as index:
                return index.lookup(reference)
        return None

    def _artifact_paths(self, item: dict) -> list[tuple[Path, str | None]]:
        paths = item.get("paths") or ([item["path"]] if item.get("path") else [])
        side_hint = item.get("side")
        with self._lock:
            overrides = self._recovered_runs.copy()
        recorded, recovered = [], []
        for original in paths:
            sides = [
                (side, relative)
                for side, root in self._recorded_runs.items()
                if (relative := _within(original, root)) is not None
            ]
            if Path(original).is_absolute():
                recorded.append((Path(original), sides[0][0] if sides else side_hint))
            if not sides and side_hint in _SIDES:
                sides = [(side_hint, PurePath(_portable(original).name))]
            for side, relative in sides:
                if side in overrides and relative.parts:
                    candidate = overrides[side].joinpath(*relative.parts).resolve()
                    if candidate.is_relative_to(overrides[side]):
                        recovered.append((candidate, side))
        candidates = list(dict.fromkeys(recovered + recorded))
        if side_hint in _SIDES:
            candidates.sort(key=lambda entry: entry[1] != side_hint)
        return candidates

    def _verified[T](self, item: dict, read: Callable[[BinaryIO], T]) -> tuple[Path, T, str | None]:
        digest, size = item.get("digest"), item.get("size_bytes")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValueError("This artifact has no valid recorded SHA-256 digest.")
        problems = []
        for path, side in self._artifact_paths(item):
            try:
                if not stat.S_ISREG(path.stat().st_mode):
                    continue
                with path.open("rb") as stream:
                    before = os.fstat(stream.fileno())
                    if size is not None and before.st_size != size:
                        problems.append(f"{path}\nFile size differs from the recorded artifact.")
                        continue
                    if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                        problems.append(f"{path}\nSHA-256 does not match the recorded artifact.")
                        continue
                    stream.seek(0)
                    value = read(stream)
                    after = os.fstat(stream.fileno())
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        problems.append(f"{path}\nArtifact changed during verification.")
                        continue
                return path, value, side
            except OSError as exc:
                problems.append(f"{path}\n{exc.strerror or exc}")
        raise FileNotFoundError("\n\n".join(problems) or "No recorded artifact path is accessible.")

    def _artifact_side(self, item: dict) -> str | None:
        if item.get("side") in _SIDES:
            return item["side"]
        paths = item.get("paths") or [item.get("path")]
        sides = [
            side
            for side, root in self._recorded_runs.items()
            if any(path and _within(path, root) for path in paths)
        ]
        return sides[0] if len(sides) == 1 else None

    def resolve(self, reference: str) -> dict:
        try:
            item = self._lookup(reference)
        except _READ_ERRORS as exc:
            return _gap("Evidence index unavailable", exc, reference=reference)
        if not item:
            return _gap(
                "Evidence unavailable",
                "This citation could not be found in the report or evidence index.",
                reference=reference,
            )
        try:
            if item["kind"] == "git":
                return self._resolve_git(item)
            if item["kind"] == "artifact":
                return self._resolve_artifact(item)
            return _gap("Unsupported evidence", item.get("locator"), **item)
        except _READ_ERRORS as exc:
            return _gap(
                "Committed evidence unavailable"
                if item.get("kind") == "git"
                else "Artifact unavailable",
                exc,
                **item,
                action="repository" if item.get("kind") == "git" else "run",
                recovery_side=self._artifact_side(item),
            )

    def _resolve_git(self, item: dict) -> dict:
        target = json.loads(item["locator"])
        repo = self._repo()
        revision = item["revision"]
        if "diff_from" in target:
            paths = target.get("paths", [])
            patch, truncated = self._bounded_diff(repo, target["diff_from"], revision, paths)
            title = f"{paths[0]} · Committed diff" if len(paths) == 1 else "Committed diff"
            return {
                **item,
                "kind": "diff",
                "title": title,
                "text": patch,
                "paths": paths,
                "baseline": target["diff_from"],
                "truncated": truncated,
            }
        path, start, end = target["path"], target["start"], target["end"]
        side = next((side for side in _SIDES if self.data["revisions"].get(side) == revision), None)
        return {
            **item,
            "kind": "source",
            "title": f"{path} · " + (f"Line {start}" if start == end else f"Lines {start}–{end}"),
            "path": path,
            "start": start,
            "end": end,
            "side": side,
            "text": repo.source(revision, path, start, end),
        }

    def _resolve_artifact(self, item: dict) -> dict:
        locator = item["locator"]
        name = _portable(item.get("path") or "Artifact").name
        view = {"title": name}
        if match := re.fullmatch(r"line:([1-9][0-9]{0,9})", locator):
            number = int(match[1])
            path, lines, side = self._verified(item, lambda stream: _line_preview(stream, number))
            view.update(lines, title=f"{name} · Line {number}")
        elif locator.startswith(("papi:#", "pprof:")):
            path, data, side = self._verified(item, _profile_bytes)
            if locator.startswith("papi:#"):
                details = PapiProfile.from_bytes(data, path, lambda loc: loc).inspect(locator)
                title = details.get("event") or "PAPI capture"
            else:
                details = inspect_pprof(data, locator)
                title = "pprof " + locator.removeprefix("pprof:").replace("/", " ")
            details.pop("evidence", None)
            view.update(title=title, text=_json(details), details=details)
        elif locator == "executable":
            path, _, side = self._verified(item, lambda _: None)
            view.update(
                title="Matching executable", text="Executable artifact verified by SHA-256."
            )
        else:
            path, content, side = self._verified(item, _text_preview)
            title = f"{side.title()} manifest" if locator == "manifest" and side else name
            view.update(title=title, text=content)
        return {**item, **view, "artifact_path": str(path), "side": side}

    def measurement(self, row: int) -> dict:
        rows = self.data["benchmarks"].get("comparisons", [])
        if type(row) is not int or not 0 <= row < len(rows):
            return _gap("Measurement unavailable", "The selected benchmark row was not found.")
        comparison = rows[row]
        unit = comparison["unit"]
        delta = comparison.get("relative_change_percent")
        sections = []
        for side in _SIDES:
            values = comparison[side]
            sections.append(
                f"{side.title()}: median {_shown(values.get('median'))} {unit} · "
                f"{values['sample_count']} samples · "
                f"range {_shown(values.get('min'))}–{_shown(values.get('max'))}"
            )
        absolute = comparison.get("absolute_change")
        if delta is not None:
            sections.append(
                f"Change: {delta:+.4g}% ({absolute:+.4g} {unit})"
                if absolute is not None
                else f"Change: {delta:+.4g}%"
            )
        sections.extend(comparison.get("reasons", []))
        return {
            "kind": "measurement",
            "title": f"{comparison['benchmark']} · {unit}",
            "text": "\n".join(sections),
            "comparison": comparison,
            "row": row,
        }

    def profile(
        self, profile_id: str, operation: str | None = None, params: dict | None = None
    ) -> dict:
        summary = self.data["profiles"].get(profile_id)
        if not summary:
            return _gap("Profile unavailable", f"No capture was recorded for {profile_id}.")
        result = {
            "kind": "profile",
            "title": f"{profile_id} · {summary['format']}",
            "text": "",
            "profile_id": profile_id,
            "summary": summary,
            "operation": operation,
            "params": params or {},
        }
        if operation is None:
            return result
        try:
            original = self._profile_artifact(profile_id, summary)
            path, data, _ = self._verified(original, _profile_bytes)
            citations = {}

            def cite(locator: str) -> str:
                reference = artifact_reference(original["digest"], locator)
                citations[reference] = {**original, "reference": reference, "locator": locator}
                return reference

            profile = (
                PprofProfile.from_bytes(data, cite)
                if summary["format"] == "pprof"
                else PapiProfile.from_bytes(data, path, cite)
            )
            query = profile.query(operation, params or {}, 30)
            with self._lock:
                self._preview_citations.update(citations)
            if summary.get("locations_symbolized_with_executable"):
                result["text"] = "This preview uses the symbols embedded in the capture."
            return {**result, "query": query}
        except _READ_ERRORS as exc:
            return {
                **result,
                "text": f"Profile query unavailable.\n{exc}",
                "action": "run",
                "recovery_side": profile_id.partition("/")[0],
            }

    def _profile_artifact(self, profile_id: str, summary: dict) -> dict:
        if entry := self._profiles.get(profile_id):
            path = entry["file"]
            side = entry["side"]
            if not _portable(path).is_absolute() and (root := self._recorded_runs.get(side)):
                path = str(_portable(root) / path)
            return {"kind": "artifact", "path": path, "digest": entry["digest"], "side": side}
        for ref in summary.get("evidence", []):
            item = self._lookup(ref)
            if item and item.get("locator") in {"papi:#", "pprof:profile"}:
                return {**item, "side": profile_id.partition("/")[0]}
        raise ValueError("The capture's artifact record is unavailable.")

    def changed_files(
        self, base: str | None = None, candidate: str | None = None
    ) -> list[tuple[str, str]]:
        revisions = self.data["revisions"]
        return self._repo().changed_files(
            base or revisions["baseline"], candidate or revisions["candidate"]
        )

    def code_source(self, side: str, path: str, revision: str | None = None) -> str:
        return self._repo().source_file(revision or self.data["revisions"][side], path)

    def code_diff(
        self, path: str, base: str | None = None, candidate: str | None = None
    ) -> tuple[str, bool]:
        revisions = self.data["revisions"]
        return self._bounded_diff(
            self._repo(),
            base or revisions["baseline"],
            candidate or revisions["candidate"],
            [path],
        )

    @staticmethod
    def _bounded_diff(
        repo: Repository, base: str, candidate: str, paths: list[str]
    ) -> tuple[str, bool]:
        try:
            return repo.diff(base, candidate, paths), False
        except ValueError as exc:
            if "output exceeds" not in str(exc):
                raise
            target = "this file" if len(paths) == 1 else "these commits"
            return f"The patch for {target} exceeds the Git preview limit.", True
