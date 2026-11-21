"""Bounded reads from a Git checkout's committed object database."""

import os
import re
import subprocess
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import BinaryIO

_COMMIT = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_GIT_TIMEOUT = 15
_MAX_SOURCE_BYTES = 4 * 1024 * 1024
_MAX_SOURCE_RESULT_BYTES = 64 * 1024
_MAX_SOURCE_LINES = 200
_MAX_DIFF_BYTES = 128 * 1024


def _read(stream: BinaryIO, limit: int) -> bytes:
    with stream:
        return stream.read(limit + 1)


def _git_executable() -> Path:
    executable = "git.exe" if os.name == "nt" else "git"
    for entry in os.get_exec_path():
        directory = Path(entry)
        if directory.is_absolute():
            candidate = directory / executable
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    raise FileNotFoundError("Git executable was not found on PATH")


def _git(directory: Path, args: list[str], limit: int, *, bare: bool = False) -> bytes:
    command = [
        str(_git_executable()),
        "--no-pager",
        "--no-replace-objects",
        "--literal-pathspecs",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "protocol.allow=never",
        "-C",
        str(directory),
        *(["--bare"] if bare else []),
        *args,
    ]
    # Ambient Git settings can redirect reads to another repository or object database.
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
    }
    environment.update(
        GIT_OPTIONAL_LOCKS="0",
        GIT_NO_LAZY_FETCH="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_ALLOW_PROTOCOL="",
    )
    with (
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        ) as process,
        ThreadPoolExecutor(max_workers=2) as readers,
    ):
        assert process.stdout is not None and process.stderr is not None
        stdout = readers.submit(_read, process.stdout, limit)
        stderr = readers.submit(_read, process.stderr, 16 * 1024)
        try:
            status = process.wait(timeout=_GIT_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise ValueError(f"Git {args[0]} timed out after {_GIT_TIMEOUT} seconds") from exc
        output = stdout.result()
        error = stderr.result().decode("utf-8", errors="replace").strip()
    if len(output) > limit:
        raise ValueError(f"Git {args[0]} output exceeds {limit} bytes")
    if status:
        raise ValueError(f"Git {args[0]} failed: {error or f'exit status {status}'}")
    return output


def _commit_object(commit: str) -> str:
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise ValueError("revision must be a full Git commit ID")
    return f"{commit.lower()}^{{commit}}"


def _git_path(path: str) -> str:
    if (
        not isinstance(path, str)
        or len(path) > 1024
        or "\\" in path
        or any(ord(char) < 32 for char in path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise ValueError(f"invalid repository-relative Git path: {path!r}")
    return path


class Repository:
    """Read revisions, source and diffs without changing a Git checkout."""

    def __init__(self, path: Path | str):
        directory = Path(path).resolve(strict=True)
        if not directory.is_dir():
            raise ValueError(f"repository path is not a directory: {directory}")
        root = _git(directory, ["rev-parse", "--show-toplevel"], 16 * 1024)
        self.root = Path(os.fsdecode(root.rstrip(b"\r\n"))).resolve(strict=True)
        git_dir = _git(self.root, ["rev-parse", "--absolute-git-dir"], 16 * 1024)
        self._git_dir = Path(os.fsdecode(git_dir.rstrip(b"\r\n"))).resolve(strict=True)

    def resolve(self, revision: str) -> str:
        """Resolve a revision expression to a full commit object ID."""
        if (
            not isinstance(revision, str)
            or not revision
            or len(revision) > 1024
            or "\0" in revision
        ):
            raise ValueError("expected a Git revision expression of at most 1024 characters")
        output = _git(
            self.root,
            ["rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
            1024,
        )
        commit = output.decode("ascii").strip()
        if not _COMMIT.fullmatch(commit):
            raise ValueError(f"Git returned an invalid commit ID for {revision!r}")
        return commit.lower()

    def source(self, sha: str, path: str, start: int, end: int) -> str:
        """Read an inclusive, one-based line range from a committed UTF-8 file."""
        if type(start) is not int or type(end) is not int or not 1 <= start <= end:
            raise ValueError("source lines must be a one-based inclusive range")
        if end - start + 1 > _MAX_SOURCE_LINES:
            raise ValueError(f"source ranges may contain up to {_MAX_SOURCE_LINES} lines")
        blob = self._source_blob(sha, path)
        lines = blob.split(b"\n")
        if lines[-1] == b"":
            lines.pop()
        if start > len(lines):
            raise ValueError(f"source start line {start} is past the end of {path!r}")
        selected = b"\n".join(lines[start - 1 : end])
        if end < len(lines) or blob.endswith(b"\n"):
            selected += b"\n"
        if len(selected) > _MAX_SOURCE_RESULT_BYTES:
            raise ValueError(f"source range exceeds {_MAX_SOURCE_RESULT_BYTES} bytes")
        try:
            return selected.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"committed path is not UTF-8 text: {path!r}") from exc

    def _source_blob(self, sha: str, path: str) -> bytes:
        object_name = f"{_commit_object(sha)}:{_git_path(path)}"
        blob = _git(self.root, ["cat-file", "blob", object_name], _MAX_SOURCE_BYTES)
        if b"\x00" in blob:
            raise ValueError(f"committed path is binary: {path!r}")
        return blob

    def source_file(self, sha: str, path: str) -> str:
        """Read a bounded committed UTF-8 file."""
        try:
            return self._source_blob(sha, path).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"committed path is not UTF-8 text: {path!r}") from exc

    def changed_files(self, base: str, candidate: str) -> list[tuple[str, str]]:
        """Return status and path pairs between two commits."""
        args = [
            "diff",
            "--name-status",
            "-z",
            "--no-renames",
            "--no-relative",
            "--no-ext-diff",
            _commit_object(base),
            _commit_object(candidate),
            "--",
        ]
        raw = _git(self._git_dir, args, _MAX_DIFF_BYTES, bare=True)
        if not raw:
            return []
        fields = raw.rstrip(b"\x00").split(b"\x00")
        return [
            (status.decode("ascii"), os.fsdecode(path))
            for status, path in zip(fields[::2], fields[1::2], strict=True)
        ]

    def diff(self, base: str, candidate: str, paths: Iterable[str] = ()) -> str:
        """Read a bounded unified patch between two commits, optionally scoped to paths."""
        if isinstance(paths, str):
            raise TypeError("diff paths must be a collection of repository-relative paths")
        selected = []
        length = 0
        for path in paths:
            path = _git_path(path)
            selected.append(path)
            length += len(path)
            if len(selected) > 32 or length > 12 * 1024:
                raise ValueError("diff accepts up to 32 paths totaling at most 12288 characters")
        args = [
            "diff",
            "--patch",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--no-relative",
            "--submodule=short",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--unified=3",
            _commit_object(base),
            _commit_object(candidate),
            "--",
            *selected,
        ]
        # Diff drivers can otherwise pick up an uncommitted .gitattributes from the checkout.
        return _git(self._git_dir, args, _MAX_DIFF_BYTES, bare=True).decode(
            "utf-8", errors="replace"
        )
