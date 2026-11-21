"""Validation and path resolution for previously collected profiler runs."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from yaml.constructor import ConstructorError
from yaml.events import AliasEvent
from yaml.nodes import MappingNode

_MANIFEST_NAMES = ("manifest.yaml", "manifest.yml", "manifest.json")
_MANIFEST_LIMIT = 1024 * 1024
_COMMIT = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_Nonempty = Annotated[str, StringConstraints(min_length=1)]
_WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³"
}


class _ManifestLoader(yaml.SafeLoader):
    """Reject ambiguous keys and bound YAML structure before constructing values."""

    _depth = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.peek_event()
        if isinstance(event, AliasEvent) or self._depth >= 64:
            raise ConstructorError(
                None,
                None,
                "YAML aliases and nesting beyond 64 levels are unsupported",
                event.start_mark,
            )
        self._depth += 1
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[str, Any]:
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in mapping:
                raise ConstructorError(
                    None, None, "manifest keys must be unique strings", key_node.start_mark
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


class _ManifestModel(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)


class Benchmark(_ManifestModel):
    """Identity and raw output of a Go benchmark collection."""

    adapter: Literal["go-test"]
    results: _Nonempty
    package: _Nonempty | None = None
    selector: _Nonempty | None = None
    fixture_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{64}$")] | None = None

    @field_validator("fixture_sha256")
    @classmethod
    def _normalize_digest(cls, value: str | None) -> str | None:
        return value.lower() if value else None


class Profile(_ManifestModel):
    """Location and collection metadata of one profiler artifact."""

    kind: _Nonempty
    format: _Nonempty
    file: _Nonempty
    executable: _Nonempty | None = None
    optional: bool = False


class Manifest(_ManifestModel):
    """Versioned description of one measured revision."""

    schema_version: Annotated[int, Field(ge=1, le=1)]
    revision: _Nonempty
    benchmark: Benchmark
    profiles: list[Profile] = Field(default_factory=list)
    environment: dict[str, Any] = Field(default_factory=dict)
    workload: dict[str, Any] = Field(default_factory=dict)
    build: dict[str, Any] = Field(default_factory=dict)
    collection: dict[str, Any] = Field(default_factory=dict)
    logs: list[_Nonempty] = Field(default_factory=list)

    @field_validator("revision")
    @classmethod
    def _normalize_revision(cls, value: str) -> str:
        if not _COMMIT.fullmatch(value):
            raise ValueError("revision must be a full Git commit ID")
        return value.lower()


def _artifact_path(directory: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("artifact path must be a nonempty relative string")
    portable = relative.replace("\\", "/")
    parts = portable.split("/")
    if any(
        part in {"", ".", ".."}
        or part.endswith((".", " "))
        or ":" in part
        or part.split(".", 1)[0].rstrip().upper() in _WINDOWS_DEVICES
        or any(ord(char) < 32 for char in part)
        for part in parts
    ):
        raise ValueError(f"invalid relative artifact path {relative!r}")
    try:
        candidate = directory.joinpath(*parts).resolve()
    except RuntimeError as exc:
        raise ValueError(f"unable to resolve artifact path {relative!r}") from exc
    if candidate == directory or not candidate.is_relative_to(directory):
        raise ValueError(f"artifact path escapes the run directory: {relative!r}")
    return candidate


@dataclass(frozen=True)
class Run:
    """Validated manifest and artifact paths for one run directory."""

    directory: Path
    manifest_path: Path
    manifest: Manifest

    def artifact(self, relative: str) -> Path:
        """Resolve an existing regular file within this run directory."""
        path = _artifact_path(self.directory, relative)
        if not path.exists():
            raise FileNotFoundError(f"artifact {relative!r} is missing in {self.directory}")
        if not path.is_file():
            raise ValueError(f"artifact {relative!r} must be a regular file")
        return path


def load_run(directory: Path, expected_revision: str) -> Run:
    """Read and validate a manifest against a resolved Git commit ID."""
    directory = Path(directory).resolve(strict=True)
    if not directory.is_dir():
        raise ValueError(f"run path is not a directory: {directory}")
    if not isinstance(expected_revision, str) or not _COMMIT.fullmatch(expected_revision):
        raise ValueError("expected_revision must be a full Git commit ID")

    names = [name for name in _MANIFEST_NAMES if (directory / name).exists()]
    if not names:
        raise FileNotFoundError(f"no run manifest found in {directory}")
    if len(names) > 1:
        raise ValueError(f"multiple run manifests found in {directory}: {', '.join(names)}")
    manifest_path = _artifact_path(directory, names[0])
    if not manifest_path.is_file():
        raise ValueError(f"run manifest must be a regular file: {manifest_path}")

    with manifest_path.open("rb") as stream:
        raw = stream.read(_MANIFEST_LIMIT + 1)
    if len(raw) > _MANIFEST_LIMIT:
        raise ValueError(f"run manifest exceeds {_MANIFEST_LIMIT} bytes: {manifest_path}")
    try:
        manifest = Manifest.model_validate(
            yaml.load(raw.decode("utf-8-sig"), Loader=_ManifestLoader)
        )
    except (yaml.YAMLError, ValueError) as exc:
        raise ValueError(f"invalid run manifest {manifest_path}: {exc}") from exc
    if manifest.revision != expected_revision.lower():
        raise ValueError(
            f"run manifest {manifest_path} records revision {manifest.revision}; "
            f"expected {expected_revision.lower()}"
        )

    run = Run(directory=directory, manifest_path=manifest_path, manifest=manifest)
    run.artifact(manifest.benchmark.results)
    for log in manifest.logs:
        run.artifact(log)
    for profile in manifest.profiles:
        try:
            run.artifact(profile.file)
        except FileNotFoundError:
            if not profile.optional:
                raise
        if profile.executable is not None:
            _artifact_path(directory, profile.executable)
    return run
