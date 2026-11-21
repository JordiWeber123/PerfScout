"""Interpret upstream pprof protobuf sample stacks and weights."""

import gzip
import heapq
import io
import re
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from ._common import (
    MAX_ARTIFACT_BYTES,
    Citation,
    ProfileError,
    display_text,
    read_artifact,
    require_params,
    result_limit,
)
from ._protobuf import Message, encode_bytes, encode_integer, signed_int64
from ._symbolize import symbolize

_MAX_STACK_LOCATIONS = 512
_MAX_INLINE_FRAMES = 128
_MAX_SAMPLE_TYPES = 64
_MAX_SAMPLE_LABELS = 64
_MAX_TOTAL_FRAMES = 1_000_000
_MAX_SAMPLE_CITATIONS = 3
_SAFE_BUILD_ID = re.compile(r"[a-zA-Z0-9_-]{1,256}")


@dataclass(frozen=True, slots=True)
class _Metric:
    name: str
    unit: str


@dataclass(frozen=True, slots=True)
class _Function:
    key: str
    name: str
    system_name: str
    file: str
    start_line: int

    def identity(self) -> tuple[str, str, int]:
        return self.name or self.system_name, self.file, self.start_line


@dataclass(frozen=True, slots=True)
class _Mapping:
    id: int
    start: int
    limit: int
    offset: int
    file: str
    build_id: str


@dataclass(frozen=True, slots=True)
class _Frame:
    location: int
    address: int
    function: _Function | None = None
    line: int = 0
    column: int = 0
    from_executable: bool = False

    @property
    def key(self) -> str:
        return self.function.key if self.function else f"location/{self.location}"

    @property
    def name(self) -> str:
        if self.function:
            return self.function.name or self.function.system_name
        return f"{self.address:#x}" if self.address else f"(location {self.location})"

    def matches(self, name: str) -> bool:
        return (
            self.key == name
            or self.name == name
            or (self.function is not None and self.function.system_name == name)
        )

    def describe(self) -> dict[str, Any]:
        function = self.function
        return {
            "function": display_text(self.name),
            "function_id": self.key,
            "system_name": display_text(function.system_name or None) if function else None,
            "source_file": display_text(function.file or None) if function else None,
            "function_start_line": (function.start_line or None) if function else None,
            "source_line": self.line or None,
            "source_column": self.column or None,
            "location_id": self.location,
            "address": f"{self.address:#x}" if self.address else None,
            "symbol_source": (
                ("executable" if self.from_executable else "profile") if function else None
            ),
        }


@dataclass(frozen=True, slots=True)
class _Location:
    id: int
    address: int
    mapping_id: int
    frames: tuple[_Frame, ...]


@dataclass(frozen=True, slots=True)
class _Sample:
    frames: tuple[_Frame, ...]
    values: tuple[int, ...]
    labels: tuple[tuple[str, str | int, str | None], ...]


@dataclass(slots=True)
class _Cost:
    frame: _Frame
    flat: int = 0
    cumulative: int = 0
    sample_count: int = 0
    example_samples: list[int] = field(default_factory=list)

    def add(self, index: int, value: int, *, is_leaf: bool = False) -> None:
        self.cumulative += value
        if is_leaf:
            self.flat += value
        self.sample_count += 1
        if len(self.example_samples) < _MAX_SAMPLE_CITATIONS:
            self.example_samples.append(index)


class _PprofData:
    def __init__(self, artifact: bytes):
        if artifact.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(artifact)) as stream:
                artifact = stream.read(MAX_ARTIFACT_BYTES + 1)
            if len(artifact) > MAX_ARTIFACT_BYTES:
                raise ProfileError("decompressed pprof profile exceeds the 32 MiB limit")
        self.profile = Message.profile(artifact)
        self.strings = self.profile.strings(6)
        if not self.strings or self.strings[0]:
            raise ProfileError("pprof profile is missing its initial empty string-table entry")
        self.metrics = tuple(self.metric(message) for message in self.profile.messages(1))
        if not 1 <= len(self.metrics) <= _MAX_SAMPLE_TYPES:
            raise ProfileError("pprof profile must contain 1 to 64 sample types")
        preferred = self.text(self.profile.integer(14))
        self.default_metric = next(
            (index for index, metric in enumerate(self.metrics) if metric.name == preferred),
            len(self.metrics) - 1,
        )
        self.mappings: dict[int, _Mapping] = {}
        for message in self.profile.messages(3):
            mapping_id = self._unique_id(message, self.mappings, "mapping")
            self.mappings[mapping_id] = _Mapping(
                mapping_id,
                message.integer(2),
                message.integer(3),
                message.integer(4),
                self.text(message.integer(5)),
                self.text(message.integer(6)),
            )
        self.functions: dict[int, _Function] = {}
        for message in self.profile.messages(5):
            function_id = self._unique_id(message, self.functions, "function")
            self.functions[function_id] = _Function(
                f"function/{function_id}",
                self.text(message.integer(2)),
                self.text(message.integer(3)),
                self.text(message.integer(4)),
                signed_int64(message.integer(5)),
            )
        self.locations: dict[int, _Location] = {}
        for message in self.profile.messages(4):
            location_id = self._unique_id(message, self.locations, "location")
            mapping_id = message.integer(2)
            if mapping_id and mapping_id not in self.mappings:
                raise ProfileError(f"pprof location {location_id} references an unknown mapping")
            address = message.integer(3)
            frames = []
            for line in message.messages(4):
                if len(frames) >= _MAX_INLINE_FRAMES:
                    raise ProfileError("pprof inline stack exceeds the 128-frame limit")
                function_id = line.integer(1)
                function = self.functions.get(function_id)
                if function_id and function is None:
                    raise ProfileError(
                        f"pprof location {location_id} references an unknown function"
                    )
                frames.append(
                    _Frame(
                        location_id,
                        address,
                        function if function and (function.name or function.system_name) else None,
                        signed_int64(line.integer(2)),
                        signed_int64(line.integer(3)),
                    )
                )
            self.locations[location_id] = _Location(
                location_id, address, mapping_id, tuple(frames) or (_Frame(location_id, address),)
            )

    def text(self, index: int) -> str:
        if not 0 <= index < len(self.strings):
            raise ProfileError("pprof string index is outside the string table")
        return self.strings[index]

    def metric(self, message: Message) -> _Metric:
        name = self.text(message.integer(1))
        unit = self.text(message.integer(2))
        if not name:
            raise ProfileError("pprof sample type is missing its name")
        return _Metric(name, unit)

    @staticmethod
    def _unique_id(message: Message, table: Mapping[int, Any], kind: str) -> int:
        value = message.integer(1)
        if value <= 0 or value in table:
            raise ProfileError(f"pprof contains an invalid or duplicate {kind} ID")
        return value

    def _labels(self, message: Message) -> tuple[tuple[str, str | int, str | None], ...]:
        labels = []
        for label in message.messages(3):
            if len(labels) >= _MAX_SAMPLE_LABELS:
                raise ProfileError("pprof sample exceeds the 64-label limit")
            key = self.text(label.integer(1))
            string_index = label.integer(2)
            if string_index:
                labels.append((key, self.text(string_index), None))
            else:
                labels.append(
                    (
                        key,
                        signed_int64(label.integer(3)),
                        self.text(label.integer(4)) or None,
                    )
                )
        return tuple(labels)

    def samples(self, resolved: Mapping[int, Sequence[_Frame]]) -> list[_Sample]:
        samples = []
        frame_count = 0
        for message in self.profile.messages(2):
            frames: list[_Frame] = []
            for location in message.integers(1, _MAX_STACK_LOCATIONS):
                if location not in self.locations:
                    raise ProfileError("pprof sample references an unknown location")
                frames.extend(resolved.get(location, self.locations[location].frames))
            frame_count += len(frames)
            if frame_count > _MAX_TOTAL_FRAMES:
                raise ProfileError("pprof profile exceeds the 1,000,000 sampled-frame limit")
            values = message.integers(2, _MAX_SAMPLE_TYPES)
            if len(values) != len(self.metrics):
                raise ProfileError("pprof sample weights do not match the declared sample types")
            samples.append(
                _Sample(tuple(frames), tuple(map(signed_int64, values)), self._labels(message))
            )
        return samples


def _profile_data(artifact: bytes) -> _PprofData:
    try:
        return _PprofData(artifact)
    except (UnicodeError, EOFError, gzip.BadGzipFile, zlib.error) as error:
        raise ProfileError("invalid compressed or encoded pprof profile") from error


def _address_probe(mapping: _Mapping, addresses: set[int]) -> bytes:
    # Construct a minimal local profile. The original file names, URLs, labels,
    # frame filters, weights and other mappings stay outside the subprocess.
    build_id = mapping.build_id if _SAFE_BUILD_ID.fullmatch(mapping.build_id) else ""
    encoded_mapping = (
        encode_integer(1, 1)
        + encode_integer(2, mapping.start)
        + encode_integer(3, mapping.limit)
        + encode_integer(4, mapping.offset)
        + encode_integer(6, 3)
    )
    parts = [
        encode_bytes(1, encode_integer(1, 1) + encode_integer(2, 2)),
        encode_bytes(3, encoded_mapping),
        encode_bytes(6, b""),
        encode_bytes(6, b"samples"),
        encode_bytes(6, b"count"),
        encode_bytes(6, build_id.encode("ascii")),
    ]
    for index, address in enumerate(sorted(addresses), 1):
        parts.append(encode_bytes(2, encode_integer(1, index) + encode_integer(2, 1)))
        parts.append(
            encode_bytes(
                4,
                encode_integer(1, index) + encode_integer(2, 1) + encode_integer(3, address),
            )
        )
    return gzip.compress(b"".join(parts))


def _symbolize_missing(
    raw: _PprofData, executable: Path, mapping: _Mapping, unresolved: set[int]
) -> dict[int, tuple[_Frame, ...]]:
    pending = {
        location.id: location
        for location in (raw.locations[location_id] for location_id in sorted(unresolved))
        if location.address
        and location.mapping_id == mapping.id
        and all(frame.function is None for frame in location.frames)
    }
    if not pending:
        return {}
    addresses = {location.address for location in pending.values()}
    if len(addresses) > 10_000:
        raise ProfileError("local pprof symbolization exceeds the 10,000-address limit")
    enriched = _profile_data(symbolize(executable, _address_probe(mapping, addresses)))
    symbols = {location.address: location.frames for location in enriched.locations.values()}
    existing = {function.identity(): function for function in raw.functions.values()}
    result = {}
    for location_id, location in pending.items():
        frames = []
        for frame in symbols.get(location.address, ()):
            function = frame.function
            if function is not None:
                function = existing.get(
                    function.identity(),
                    _Function(
                        f"symbolized/{function.key.rsplit('/', 1)[-1]}",
                        function.name,
                        function.system_name,
                        function.file,
                        function.start_line,
                    ),
                )
            frames.append(
                _Frame(
                    location_id,
                    location.address,
                    function,
                    frame.line,
                    frame.column,
                    True,
                )
            )
        if any(frame.function is not None for frame in frames):
            result[location_id] = tuple(frames)
    return result


class PprofProfile:
    """A weighted pprof stack profile with independent metrics and attributed edges."""

    def __init__(
        self,
        raw: _PprofData,
        executable: Path | None,
        cite: Citation,
        cite_executable: Callable[[], str] | None,
    ):
        self._cite = cite
        self._cite_executable = cite_executable
        self._metrics = raw.metrics
        self._default_metric = raw.default_metric
        self._duration_ns = signed_int64(raw.profile.integer(10))
        self._time_ns = signed_int64(raw.profile.integer(9))
        self._period = signed_int64(raw.profile.integer(12))
        self._period_type = next((raw.metric(metric) for metric in raw.profile.messages(11)), None)
        self._gaps: list[str] = []
        self._samples = raw.samples({})
        unresolved = {
            frame.location
            for sample in self._samples
            for frame in sample.frames
            if frame.function is None
        }
        resolved: dict[int, tuple[_Frame, ...]] = {}
        if executable is not None and unresolved:
            mapping = next(iter(raw.mappings.values()), _Mapping(0, 0, 0, 0, "", ""))
            try:
                resolved = _symbolize_missing(raw, executable, mapping, unresolved)
            except ProfileError as error:
                self._gaps.append(str(error))
            else:
                if resolved:
                    self._samples = raw.samples(resolved)
                    if not _SAFE_BUILD_ID.fullmatch(mapping.build_id):
                        self._gaps.append(
                            "The supplied executable was used for local symbolization. "
                            "The capture has no build ID usable for matching by pprof."
                        )
                else:
                    self._gaps.append(
                        "Local pprof found no additional symbols using the supplied executable."
                    )
        self._symbolized_locations = len(resolved)
        self._label_keys = sorted({key for sample in self._samples for key, *_ in sample.labels})
        self._totals = (
            tuple(map(sum, zip(*(sample.values for sample in self._samples), strict=True)))
            if self._samples
            else (0,) * len(self._metrics)
        )
        unknown = {
            frame.location
            for sample in self._samples
            for frame in sample.frames
            if frame.function is None
        }
        self._unknown_locations = len(unknown)
        if unknown:
            gap = f"Function symbols are missing for {len(unknown)} sampled pprof locations."
            if executable is None:
                gap += " No matching executable was supplied for local symbolization."
            self._gaps.append(gap)
        stackless = sum(not sample.frames for sample in self._samples)
        if stackless:
            self._gaps.append(f"{stackless} pprof sample records have no stack.")
        if not self._samples:
            self._gaps.append("pprof profile contains no sample records.")

    @classmethod
    def load(
        cls,
        path: Path,
        executable: Path | None,
        cite: Citation,
        cite_executable: Callable[[], str] | None = None,
    ) -> Self:
        return cls(_profile_data(read_artifact(path)), executable, cite, cite_executable)

    def _artifact_evidence(self) -> list[str]:
        evidence = [self._cite("pprof:profile")]
        if self._symbolized_locations and self._cite_executable is not None:
            evidence.append(self._cite_executable())
        return evidence

    def _metric_fields(self, index: int) -> dict[str, Any]:
        metric = self._metrics[index]
        return {
            "metric": display_text(metric.name),
            "metric_index": index,
            "unit": display_text(metric.unit) or None,
            "total_weight": self._totals[index],
        }

    def describe(self) -> dict[str, Any]:
        period_type = None
        if self._period_type:
            period_type = {
                "metric": display_text(self._period_type.name),
                "unit": display_text(self._period_type.unit) or None,
            }
        return {
            "format": "pprof",
            "capabilities": ["hotspots", "callers", "callees"],
            "metrics": [self._metric_fields(index) for index in range(len(self._metrics))],
            "default_metric_index": self._default_metric,
            "sample_records": len(self._samples),
            "sample_label_keys": [display_text(key, 128) for key in self._label_keys[:50]],
            "sample_label_key_count": len(self._label_keys),
            "time_ns": self._time_ns or None,
            "duration_ns": self._duration_ns or None,
            "period": self._period or None,
            "period_type": period_type,
            "unresolved_locations": self._unknown_locations,
            "locations_symbolized_with_executable": self._symbolized_locations,
            "gaps": self._gaps.copy(),
            "evidence": self._artifact_evidence(),
        }

    def _metric_index(self, metric: str | int | None) -> int:
        if metric is None:
            return self._default_metric
        if type(metric) is int and 0 <= metric < len(self._metrics):
            return metric
        if isinstance(metric, str):
            matches = [index for index, item in enumerate(self._metrics) if item.name == metric]
            if len(matches) == 1:
                return matches[0]
            if matches:
                raise ProfileError("ambiguous pprof metric name; use its zero-based metric index")
        raise ProfileError(
            "unknown pprof metric; use the sample type name or zero-based metric index"
        )

    def _hotspots(self, metric: int) -> dict[str, _Cost]:
        costs: dict[str, _Cost] = {}
        for index, sample in enumerate(self._samples):
            seen = set()
            for depth, frame in enumerate(sample.frames):
                if frame.key in seen:
                    continue
                seen.add(frame.key)
                if frame.key not in costs:
                    costs[frame.key] = _Cost(frame)
                costs[frame.key].add(index, sample.values[metric], is_leaf=depth == 0)
        return costs

    def _edges(
        self,
        metric: int,
        function: str,
        operation: str,
    ) -> tuple[dict[str, _Cost], bool]:
        costs: dict[str, _Cost] = {}
        matched = False
        for index, sample in enumerate(self._samples):
            seen = set()
            for depth, frame in enumerate(sample.frames):
                if not frame.matches(function):
                    continue
                matched = True
                neighbor_index = depth + (1 if operation == "callers" else -1)
                if not 0 <= neighbor_index < len(sample.frames):
                    continue
                neighbor = sample.frames[neighbor_index]
                if neighbor.key in seen:
                    continue
                seen.add(neighbor.key)
                if neighbor.key not in costs:
                    costs[neighbor.key] = _Cost(neighbor)
                costs[neighbor.key].add(index, sample.values[metric])
        return costs, matched

    def _row(self, cost: _Cost, operation: str) -> dict[str, Any]:
        evidence = [
            self._cite(f"pprof:location/{cost.frame.location}"),
            *(self._cite(f"pprof:sample/{sample}") for sample in cost.example_samples),
        ]
        if cost.frame.from_executable and self._cite_executable is not None:
            evidence.append(self._cite_executable())
        row = {
            **cost.frame.describe(),
            "sample_records": cost.sample_count,
            "example_sample_citations": len(cost.example_samples),
            "evidence": evidence,
        }
        if operation == "hotspots":
            return {**row, "flat": cost.flat, "cumulative": cost.cumulative}
        return {**row, "weight": cost.cumulative}

    def query(self, operation: str, params: Mapping[str, Any], limit: int) -> dict[str, Any]:
        limit = result_limit(limit)
        gaps = self._gaps.copy()
        if operation == "hotspots":
            require_params(params, "metric", "aggregation")
            metric = self._metric_index(params.get("metric"))
            aggregation = params.get("aggregation", "flat")
            if aggregation not in ("flat", "cumulative"):
                raise ProfileError("pprof hotspot aggregation must be flat or cumulative")
            costs = self._hotspots(metric)
            order = str(aggregation)
            context = {"aggregation": order}
        elif operation in ("callers", "callees"):
            require_params(params, "metric", "function")
            metric = self._metric_index(params.get("metric"))
            function = params.get("function")
            if not isinstance(function, str) or not function or len(function) > 65_536:
                raise ProfileError("pprof edge query requires a function name or ID")
            costs, matched = self._edges(metric, function, operation)
            order = "cumulative"
            context = {"function": display_text(function)}
            if not matched:
                gaps.append("No sampled pprof frame matches the requested function.")
            elif not costs:
                gaps.append(f"No sampled {operation} were found for the requested function.")
        else:
            raise ProfileError("pprof profiles support hotspots, callers, and callees queries")
        selected = heapq.nsmallest(
            limit,
            costs.values(),
            key=lambda cost: (-abs(getattr(cost, order)), cost.frame.name, cost.frame.key),
        )
        return {
            "format": "pprof",
            "operation": operation,
            **self._metric_fields(metric),
            **context,
            "limit": limit,
            "total_rows": len(costs),
            "rows": [self._row(cost, operation) for cost in selected],
            "gaps": gaps,
            "evidence": self._artifact_evidence(),
        }
