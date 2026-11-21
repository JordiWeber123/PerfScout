"""Read Go benchmark text and compare comparable repetitions."""

import re
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException, localcontext
from math import isfinite
from pathlib import Path
from statistics import median

_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_ITERATIONS = re.compile(r"[+]?[0-9]{1,19}")
_SETTING_SECTIONS = ("benchmark", "workload", "environment", "build", "collection")
_UNIT_CONVERSIONS = {
    "us/op": ("ns/op", 1_000),
    "µs/op": ("ns/op", 1_000),
    "μs/op": ("ns/op", 1_000),
    "ms/op": ("ns/op", 1_000_000),
    "s/op": ("ns/op", 1_000_000_000),
    "sec/op": ("ns/op", 1_000_000_000),
    **{
        f"{prefix}B/{denominator}": (f"B/{denominator}", factor)
        for denominator in ("op", "s")
        for prefix, factor in (
            ("k", 1_000),
            ("K", 1_000),
            ("M", 1_000_000),
            ("G", 1_000_000_000),
            ("Ki", 1_024),
            ("Mi", 1_048_576),
            ("Gi", 1_073_741_824),
        )
    },
}
_DEFAULT_BETTER = {"ns/op": "lower", "B/op": "lower", "allocs/op": "lower", "B/s": "higher"}


@dataclass(frozen=True)
class _Sample:
    value: Decimal
    original_value: str
    original_unit: str
    iterations: int
    evidence: str


@dataclass
class _Workload:
    configuration: dict[str, str]
    configuration_evidence: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, list[_Sample]] = field(default_factory=lambda: defaultdict(list))


@dataclass
class _BenchmarkFile:
    workloads: dict[str, dict[tuple[tuple[str, str], ...], _Workload]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    unit_metadata: dict[str, dict[str, dict[str, str]]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    gaps: list[dict] = field(default_factory=list)
    failed: bool = False
    incomplete: bool = False


def _json_number(value: Decimal) -> int | float | None:
    approximate = float(value)
    if not isfinite(approximate) or (value != 0 and approximate == 0):
        return None
    return int(value) if value == value.to_integral_value() else approximate


def _read_sample_fields(fields: list[str]) -> tuple[int, dict[str, tuple[Decimal, str, str]]]:
    _, iterations_text, *metrics = fields
    if not _ITERATIONS.fullmatch(iterations_text) or not 0 < int(iterations_text) < 2**63:
        raise ValueError("invalid iteration count")
    if not metrics or len(metrics) % 2:
        raise ValueError("expected measurement value and unit pairs")
    values = {}
    for original_value, original_unit in zip(metrics[::2], metrics[1::2], strict=True):
        if not _NUMBER.fullmatch(original_value):
            raise ValueError("invalid measurement value")
        unit, factor = _UNIT_CONVERSIONS.get(original_unit, (original_unit, 1))
        if unit in values:
            raise ValueError("duplicate measurement unit")
        value = Decimal(original_value) * factor
        if _json_number(value) is None:
            raise ValueError("measurement value is outside the supported numeric range")
        values[unit] = (value, original_value, original_unit)
    return int(iterations_text), values


def _read_unit_metadata(fields: list[str], result: _BenchmarkFile, evidence: str) -> bool:
    if len(fields) < 3:
        return False
    _, original_unit, *fields = fields
    unit, _ = _UNIT_CONVERSIONS.get(original_unit, (original_unit, 1))
    metadata = result.unit_metadata[unit]
    valid = True
    for item in fields:
        key, equals, value = item.partition("=")
        if not equals or not key or (key in metadata and metadata[key]["value"] != value):
            valid = False
            continue
        metadata.setdefault(
            key, {"value": value, "original_unit": original_unit, "evidence": evidence}
        )
    return valid


def _config_line(line: str) -> tuple[str, str] | None:
    key, colon, value = line.partition(":")
    if (
        colon
        and key
        and key[0].islower()
        and not any(char.isspace() or char.isupper() for char in key)
        and (not value or value[0] in " \t")
    ):
        return key, value.lstrip(" \t")
    return None


def _read_benchmarks(path: Path, side: str, cite: Callable[[str, int], str]) -> _BenchmarkFile:
    result = _BenchmarkFile()
    config: dict[str, str] = {}
    config_lines: dict[str, int] = {}
    evidence_by_line: dict[int, str] = {}

    def evidence(line_number: int) -> str:
        if line_number not in evidence_by_line:
            evidence_by_line[line_number] = cite(side, line_number)
        return evidence_by_line[line_number]

    def gap(kind: str, message: str, line_number: int | None = None) -> None:
        result.gaps.append(
            {
                "kind": kind,
                "side": side,
                "message": message,
                "evidence": [evidence(line_number)] if line_number is not None else [],
            }
        )

    with path.open("rb") as file:
        for line_number, raw_line in enumerate(file, 1):
            try:
                line = raw_line.decode("utf-8-sig" if line_number == 1 else "utf-8").rstrip("\r\n")
            except UnicodeDecodeError:
                gap(
                    "invalid_benchmark_line",
                    "Benchmark output contains an invalid UTF-8 sequence.",
                    line_number,
                )
                result.incomplete = True
                continue

            fields = line.split()
            if not fields or line[0].isspace():
                continue
            name = fields[0]
            if name.startswith("Benchmark"):
                # go test -v emits the name by itself when a benchmark starts.
                if len(fields) == 1 and line == name:
                    continue
                try:
                    if len(fields) < 2:
                        raise ValueError("missing iteration count")
                    iterations, values = _read_sample_fields(fields)
                except (ValueError, DecimalException) as error:
                    gap(
                        "invalid_benchmark_line",
                        f"Invalid benchmark measurement ({error}).",
                        line_number,
                    )
                    result.incomplete = True
                    continue

                key = tuple(sorted(config.items()))
                groups = result.workloads[name]
                if key not in groups:
                    groups[key] = _Workload(
                        config.copy(),
                        {key: evidence(number) for key, number in config_lines.items()},
                    )
                workload = groups[key]
                for unit, (value, original_value, original_unit) in values.items():
                    workload.metrics[unit].append(
                        _Sample(
                            value, original_value, original_unit, iterations, evidence(line_number)
                        )
                    )
            elif name == "Unit":
                if not _read_unit_metadata(fields, result, evidence(line_number)):
                    gap(
                        "invalid_unit_metadata",
                        "Invalid or conflicting benchmark unit metadata.",
                        line_number,
                    )
            elif name == "FAIL" or line.startswith(("--- FAIL:", "panic:", "fatal error:")):
                gap(
                    "benchmark_command_failed",
                    "Go benchmark output reports a failure.",
                    line_number,
                )
                result.failed = True
            elif setting := _config_line(line):
                key, value = setting
                if value:
                    config[key] = value
                    config_lines[key] = line_number
                else:
                    config.pop(key, None)
                    config_lines.pop(key, None)

    if not result.workloads:
        gap("no_benchmark_results", "No valid Go benchmark measurements were found.")
    return result


def _settings(manifest: dict) -> dict:
    settings = {section: manifest.get(section) or {} for section in _SETTING_SECTIONS}
    settings["benchmark"] = {
        key: value for key, value in settings["benchmark"].items() if key != "results"
    }
    return settings


def _setting_differences(baseline: dict, candidate: dict) -> list[str]:
    differences = []
    for section in _SETTING_SECTIONS:
        before, after = baseline[section], candidate[section]
        for key in sorted(before.keys() | after.keys()):
            if before.get(key) != after.get(key):
                differences.append(f"Run manifests differ in {section}.{key}.")
    return differences


def _workload_pairs(
    baseline: _BenchmarkFile,
    candidate: _BenchmarkFile,
) -> Iterator[tuple[str, _Workload | None, _Workload | None]]:
    for name in sorted(baseline.workloads.keys() | candidate.workloads.keys()):
        before = baseline.workloads.get(name, {})
        after = candidate.workloads.get(name, {})
        for config in sorted(before.keys() & after.keys()):
            yield name, before[config], after[config]

        baseline_only = sorted(before.keys() - after.keys())
        candidate_only = sorted(after.keys() - before.keys())
        if len(baseline_only) == len(candidate_only) == 1:
            # Show both measurements for an unambiguous configuration mismatch.
            yield name, before[baseline_only[0]], after[candidate_only[0]]
        else:
            for config in baseline_only:
                yield name, before[config], None
            for config in candidate_only:
                yield name, None, after[config]


def _summarize(samples: list[_Sample]) -> dict:
    values = [sample.value for sample in samples]
    return {
        "sample_count": len(samples),
        "median": _json_number(median(values)) if values else None,
        "min": _json_number(min(values)) if values else None,
        "max": _json_number(max(values)) if values else None,
        "samples": [
            {
                "value": _json_number(sample.value),
                "original_value": sample.original_value,
                "original_unit": sample.original_unit,
                "iterations": sample.iterations,
                "evidence": sample.evidence,
            }
            for sample in samples
        ],
    }


def _compare_samples(
    name: str,
    unit: str,
    baseline: _Workload,
    candidate: _Workload,
    reasons: list[str],
    better: str | None,
    incomplete: bool,
) -> tuple[dict, list[tuple[str, str]]]:
    before = baseline.metrics.get(unit, [])
    after = candidate.metrics.get(unit, [])
    issues = [("incomparable_settings", reason) for reason in reasons]
    status = "incomparable"
    absolute_change = relative_change = direction = None

    if not before or not after:
        missing = "baseline" if not before else "candidate"
        issues.append(
            ("unmatched_benchmark_metric", f"No {missing} measurement for this workload and unit.")
        )
    elif not reasons:
        status = "comparable"
        baseline_values = [sample.value for sample in before]
        candidate_values = [sample.value for sample in after]
        baseline_median = median(baseline_values)
        candidate_median = median(candidate_values)
        change = candidate_median - baseline_median
        absolute_change = _json_number(change)
        direction = "increased" if change > 0 else "decreased" if change < 0 else "unchanged"
        if baseline_median == 0:
            issues.append(
                ("zero_baseline", "Relative change is undefined for a zero baseline median.")
            )
        else:
            relative_change = _json_number(change / baseline_median * 100)
        if absolute_change is None or (baseline_median != 0 and relative_change is None):
            issues.append(
                ("change_out_of_range", "A change is outside the supported numeric range.")
            )

        if min(len(before), len(after)) < 3:
            status = "weak"
            issues.append(
                (
                    "insufficient_repetitions",
                    "At least three repetitions per side are needed to assess spread.",
                )
            )
        baseline_min, baseline_max = min(baseline_values), max(baseline_values)
        candidate_min, candidate_max = min(candidate_values), max(candidate_values)
        if max(baseline_min, candidate_min) <= min(baseline_max, candidate_max) and (
            baseline_min != baseline_max or candidate_min != candidate_max
        ):
            status = "weak"
            issues.append(
                (
                    "overlapping_observations",
                    "Observed ranges overlap, so the median change may reflect variability.",
                )
            )
        if incomplete:
            status = "weak"
            issues.append(
                ("incomplete_benchmark_output", "Some benchmark output could not be parsed.")
            )

    return {
        "benchmark": name,
        "unit": unit,
        "status": status,
        "reasons": [message for _, message in issues],
        "baseline": _summarize(before),
        "candidate": _summarize(after),
        "baseline_configuration": baseline.configuration,
        "candidate_configuration": candidate.configuration,
        "baseline_configuration_evidence": baseline.configuration_evidence,
        "candidate_configuration_evidence": candidate.configuration_evidence,
        "absolute_change": absolute_change,
        "relative_change_percent": relative_change,
        "direction": direction,
        "better": better,
        "evidence": list(dict.fromkeys(sample.evidence for sample in before + after)),
    }, issues


def compare_benchmarks(
    baseline_path: Path,
    candidate_path: Path,
    baseline_manifest: dict,
    candidate_manifest: dict,
    cite: Callable[[str, int], str],
) -> dict:
    """Return descriptive comparisons with a citation for each Go benchmark row."""
    baseline_settings = _settings(baseline_manifest)
    candidate_settings = _settings(candidate_manifest)
    differences = _setting_differences(baseline_settings, candidate_settings)

    with localcontext() as context:
        context.prec = 34
        baseline = _read_benchmarks(baseline_path, "baseline", cite)
        candidate = _read_benchmarks(candidate_path, "candidate", cite)
        return _compare_files(
            baseline, candidate, baseline_settings, candidate_settings, differences
        )


def _compare_files(
    baseline: _BenchmarkFile,
    candidate: _BenchmarkFile,
    baseline_settings: dict,
    candidate_settings: dict,
    differences: list[str],
) -> dict:
    gaps = baseline.gaps + candidate.gaps
    comparisons = []
    if baseline.failed or candidate.failed:
        differences = [*differences, "Go benchmark output reports a command failure."]
    for name, before, after in _workload_pairs(baseline, candidate):
        reasons = differences.copy()
        if before and after:
            changed = sorted(
                key
                for key in before.configuration.keys() | after.configuration.keys()
                if before.configuration.get(key) != after.configuration.get(key)
            )
            if changed:
                reasons.append(
                    f"Go benchmark output records different values for {', '.join(changed)}."
                )
        before = before or _Workload({})
        after = after or _Workload({})
        for unit in sorted(before.metrics.keys() | after.metrics.keys()):
            baseline_metadata = baseline.unit_metadata.get(unit, {}).get("better", {})
            candidate_metadata = candidate.unit_metadata.get(unit, {}).get("better", {})
            better_values = {
                metadata["value"]
                for metadata in (baseline_metadata, candidate_metadata)
                if metadata
            }
            better = next(iter(better_values)) if len(better_values) == 1 else None
            if not better_values:
                better = _DEFAULT_BETTER.get(unit)

            comparison, issues = _compare_samples(
                name,
                unit,
                before,
                after,
                reasons,
                better if better in {"higher", "lower"} else None,
                baseline.incomplete or candidate.incomplete,
            )
            comparisons.append(comparison)
            for kind, message in issues:
                gaps.append(
                    {
                        "kind": kind,
                        "message": message,
                        "benchmark": name,
                        "unit": unit,
                        "evidence": comparison["evidence"],
                    }
                )
            if len(better_values) > 1:
                gaps.append(
                    {
                        "kind": "conflicting_unit_metadata",
                        "benchmark": name,
                        "unit": unit,
                        "message": "The two files disagree about whether a higher or lower value is better.",
                        "evidence": [baseline_metadata["evidence"], candidate_metadata["evidence"]],
                    }
                )

    return {
        "comparisons": comparisons,
        "gaps": gaps,
        "settings": {"baseline": baseline_settings, "candidate": candidate_settings},
        "unit_metadata": {
            "baseline": dict(baseline.unit_metadata),
            "candidate": dict(candidate.unit_metadata),
        },
        "method": {
            "change": (
                "Candidate median minus baseline median. Relative change is this difference divided "
                "by the baseline median and multiplied by 100."
            ),
            "spread": "Observed minimum and maximum.",
            "uncertainty": "Sample counts and observed ranges provide descriptive checks.",
        },
    }
