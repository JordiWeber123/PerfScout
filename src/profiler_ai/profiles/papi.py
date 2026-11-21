"""Adapter for native PAPI high-level JSON, including in-region reads."""

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote

from ._common import (
    Citation,
    ProfileError,
    display_text,
    read_artifact,
    require_params,
    result_limit,
)

_BUILTIN_UNITS = {
    "cycles": "cycles",
    "real_time_nsec": "nanoseconds",
    "perf::TASK-CLOCK": "nanoseconds",
    "PAPI_TOT_CYC": "cycles",
    "PAPI_TOT_INS": "instructions",
    "PAPI_FP_INS": "instructions",
    "PAPI_FP_OPS": "operations",
}
_REGION_METADATA = {"name", "parent_region_id", "region_count", "id"}
_MAX_READINGS = 200_000
_MIN_COUNTER = -(2**63)
_MAX_COUNTER = 2**63 - 1
_INTEGER = re.compile(r"[+-]?[0-9]{1,19}")
_READ_FIELD = re.compile(r"read_[1-9][0-9]*")
_FILE_ID = re.compile(r"rank_([0-9]{1,12})\.json")
_PREVIEW_LIMIT = 50


def _locator(*segments: str) -> str:
    pointer = "".join("/" + segment.replace("~", "~0").replace("/", "~1") for segment in segments)
    return "papi:#" + quote(pointer, safe="/~")


def _object(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProfileError(f"PAPI {description} must be a JSON object")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProfileError(f"PAPI JSON contains duplicate key {display_text(key, 80)!r}")
        result[key] = value
    return result


def _identity(value: Any, description: str) -> str:
    if type(value) in (str, int):
        result = str(value)
        if result and len(result) <= 128:
            return result
    raise ProfileError(f"PAPI {description} must be a nonempty string or integer ID")


def _counter(value: Any, locator: str) -> int:
    if isinstance(value, str) and _INTEGER.fullmatch(value):
        value = int(value)
    if type(value) is not int or not _MIN_COUNTER <= value <= _MAX_COUNTER:
        raise ProfileError(
            f"PAPI event at {display_text(locator, 160)} is not a signed 64-bit integer"
        )
    return value


def _constant(value: str) -> None:
    raise ProfileError(f"PAPI JSON contains invalid constant {value}")


@dataclass(frozen=True, slots=True)
class _Region:
    rank: str | None
    thread: str
    id: str
    name: str
    parent_id: str | None
    count: int | None
    path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Reading:
    region: _Region
    event: str
    field: str | None
    value: int

    def locator(self) -> str:
        path = (*self.region.path, self.event)
        return _locator(*path) if self.field is None else _locator(*path, self.field)


def _threads(document: dict[str, Any]) -> Iterator[tuple[str | None, str, dict, tuple[str, ...]]]:
    if "ranks" in document:
        ranks = (
            (rank_id, _object(rank_doc, "rank"), ("ranks", rank_id))
            for rank_id, rank_doc in _object(document["ranks"], "ranks").items()
        )
    else:
        raw_rank = document.get("rank", document.get("rank_id"))
        rank = None if raw_rank is None else _identity(raw_rank, "rank")
        ranks = iter([(rank, document, ())])
    for rank, rank_doc, path in ranks:
        threads = rank_doc.get("threads")
        if isinstance(threads, list):
            for index, data in enumerate(threads):
                thread = _object(data, "thread")
                name = _identity(thread.get("id"), "thread ID")
                yield rank, name, thread, (*path, "threads", str(index))
        else:
            for name, data in _object(threads, "threads").items():
                yield (
                    rank,
                    _identity(name, "thread ID"),
                    _object(data, "thread"),
                    (*path, "threads", name),
                )


def _regions(thread: dict[str, Any]) -> Iterator[tuple[str, str, dict, tuple[str, ...]]]:
    regions = thread.get("regions")
    if isinstance(regions, list):
        # Early high-level output used arrays of one-key objects named by region.
        for index, entry in enumerate(regions):
            for name, data in _object(entry, "region").items():
                yield str(index), name, _object(data, "region"), ("regions", str(index), name)
    else:
        for region_id, data in _object(regions, "regions").items():
            region = _object(data, "region")
            name = region.get("name")
            if not isinstance(name, str) or not name:
                raise ProfileError("PAPI region is missing its name")
            yield _identity(region_id, "region ID"), name, region, ("regions", region_id)


def _readings(region: _Region, data: dict[str, Any]) -> Iterator[_Reading]:
    for event, value in data.items():
        if event in _REGION_METADATA:
            continue
        if isinstance(value, dict):
            if "region_value" not in value or any(
                field != "region_value" and not _READ_FIELD.fullmatch(field) for field in value
            ):
                raise ProfileError(
                    "PAPI event read series must contain region_value and read_N fields"
                )
            series = value.items()
        else:
            series = [(None, value)]
        for field, reading in series:
            path = (*region.path, event) if field is None else (*region.path, event, field)
            yield _Reading(region, event, field, _counter(reading, _locator(*path)))


class PapiProfile:
    """Per-thread and per-region measurements with exact signed PAPI counter values."""

    def __init__(self, document: dict[str, Any], path: Path, cite: Citation):
        self._cite = cite
        self._definitions = _object(document.get("event_definitions", {}), "event definitions")
        self._readings: list[_Reading] = []
        self._threads: set[tuple[str | None, str]] = set()
        self._region_count = 0
        self._version = (
            display_text(document.get("papi_version"))
            if isinstance(document.get("papi_version"), str)
            else None
        )
        file_id = _FILE_ID.fullmatch(path.name)
        self._file_id = file_id[1] if file_id else None
        self._events: dict[str, None] = {}
        for rank, thread, thread_doc, thread_path in _threads(document):
            self._threads.add((rank, thread))
            for region_id, name, region_doc, region_path in _regions(thread_doc):
                region_path = (*thread_path, *region_path)
                parent_id = region_doc.get("parent_region_id")
                count = region_doc.get("region_count")
                region = _Region(
                    rank,
                    thread,
                    region_id,
                    name,
                    None if parent_id is None else _identity(parent_id, "parent region ID"),
                    None
                    if count is None
                    else _counter(count, _locator(*region_path, "region_count")),
                    region_path,
                )
                self._region_count += 1
                for reading in _readings(region, region_doc):
                    if len(self._readings) >= _MAX_READINGS:
                        raise ProfileError("PAPI profile exceeds the 200,000 reading limit")
                    self._readings.append(reading)
                    self._events[reading.event] = None

    @classmethod
    def load(cls, path: Path, cite: Citation) -> Self:
        try:
            document = json.loads(
                read_artifact(path), object_pairs_hook=_unique_object, parse_constant=_constant
            )
        except ProfileError:
            raise
        except (ValueError, RecursionError) as error:
            raise ProfileError("invalid PAPI high-level JSON") from error
        return cls(_object(document, "profile"), path, cite)

    def _event_fields(self, event: str) -> dict[str, str | None]:
        definition = _object(self._definitions.get(event, {}), "event definition")
        fields = {
            "event": display_text(event),
            "component": None,
            "event_type": "delta" if event in {"cycles", "real_time_nsec"} else None,
            "unit": _BUILTIN_UNITS.get(event),
        }
        for key, name in (("component", "component"), ("event_type", "type"), ("unit", "unit")):
            value = definition.get(name)
            if isinstance(value, str) and value:
                fields[key] = display_text(value)
        return fields

    def describe(self) -> dict[str, Any]:
        events = list(self._events)
        ranks = sorted({rank for rank, _ in self._threads if rank is not None})
        gaps = [] if self._readings else ["PAPI capture contains no counter readings."]
        if self._file_id and not ranks:
            gaps.append("PAPI filename contains a rank-style identifier. MPI rank is unconfirmed.")
        return {
            "format": "papi-hl-json",
            "capabilities": ["counters"],
            "papi_version": self._version,
            "events": [self._event_fields(event) for event in events[:_PREVIEW_LIMIT]],
            "event_count": len(events),
            "thread_count": len(self._threads),
            "region_count": self._region_count,
            "ranks": ranks[:_PREVIEW_LIMIT],
            "capture_file_id": self._file_id,
            "reading_count": len(self._readings),
            "gaps": gaps,
            "evidence": [self._cite(_locator())],
        }

    def query(self, operation: str, params: Mapping[str, Any], limit: int) -> dict[str, Any]:
        if operation != "counters":
            raise ProfileError("PAPI profiles support the counters query")
        require_params(
            params, "event", "region", "region_id", "thread", "rank", "reading", "offset"
        )
        limit = result_limit(limit)
        offset = params.get("offset", 0)
        if type(offset) is not int or offset < 0:
            raise ProfileError("PAPI query offset must be a nonnegative integer")
        filters = {key: _identity(value, key) for key, value in params.items() if key != "offset"}
        rows = []
        total_rows = 0
        for measurement in self._readings:
            region = measurement.region
            context = {
                "event": measurement.event,
                "region": region.name,
                "region_id": region.id,
                "thread": region.thread,
                "rank": region.rank,
                "reading": measurement.field or "region_value",
            }
            if any(context[key] != value for key, value in filters.items()):
                continue
            if offset <= total_rows < offset + limit:
                rows.append(
                    {
                        **context,
                        **self._event_fields(measurement.event),
                        "region": display_text(region.name),
                        "parent_region_id": region.parent_id,
                        "region_count": region.count,
                        "value": measurement.value,
                        "exact_value": str(measurement.value),
                        "evidence": [self._cite(measurement.locator())],
                    }
                )
            total_rows += 1
        return {
            "format": "papi-hl-json",
            "operation": operation,
            "limit": limit,
            "offset": offset,
            "total_rows": total_rows,
            "rows": rows,
            "gaps": [] if total_rows else ["PAPI capture has no readings matching the query."],
            "evidence": [self._cite(_locator())],
        }
