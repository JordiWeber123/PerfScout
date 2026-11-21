import json

import pytest

from profiler_ai.benchmarks import compare_benchmarks


def _compare(tmp_path, baseline, candidate, baseline_manifest=None, candidate_manifest=None):
    baseline_path = tmp_path / "baseline.txt"
    candidate_path = tmp_path / "candidate.txt"
    baseline_path.write_bytes(baseline.encode("utf-8") if isinstance(baseline, str) else baseline)
    candidate_path.write_bytes(
        candidate.encode("utf-8") if isinstance(candidate, str) else candidate
    )
    result = compare_benchmarks(
        baseline_path,
        candidate_path,
        baseline_manifest or {},
        candidate_manifest or {},
        lambda side, line: f"{side}:{line}",
    )
    json.dumps(result, allow_nan=False)
    return result


def _repetitions(values, unit="ns/op", benchmark="BenchmarkEncode/Large-4"):
    return "".join(f"{benchmark}\t1000\t{value} {unit}\n" for value in values)


def test_compares_repetitions_and_preserves_evidence_and_custom_metrics(tmp_path):
    header = "goos: linux\ngoarch: amd64\npkg: example.com/encoder\n"
    baseline = header + (
        "BenchmarkEncode/Large-4\n"
        "BenchmarkEncode/Large-4\t1000\t100 ns/op\t10 MB/s\t1.5 misses/op\t32 B/op\t2 allocs/op\n"
        "BenchmarkEncode/Large-4\t1200\t101 ns/op\t10 MB/s\t1.75 misses/op\t32 B/op\t2 allocs/op\n"
        "BenchmarkEncode/Large-4\t1200\t99 ns/op\t10 MB/s\t1.25 misses/op\t32 B/op\t2 allocs/op\n"
        "PASS\n"
        "ok\texample.com/encoder\t3.5s\n"
    )
    candidate = header + (
        "Unit misses/op better=lower\n"
        "BenchmarkEncode/Large-4\t1200\t.125 µs/op\t8000000 B/s\t2.5 misses/op\t32 B/op\t2 allocs/op\n"
        "BenchmarkEncode/Large-4\t1100\t124 ns/op\t8 MB/s\t2.25 misses/op\t32 B/op\t2 allocs/op\n"
        "BenchmarkEncode/Large-4\t1300\t1.26e2 ns/op\t8 MB/s\t2.75 misses/op\t32 B/op\t2 allocs/op\n"
    )
    result = _compare(tmp_path, baseline, candidate.replace("\n", "\r\n"))
    comparison = {row["unit"]: row for row in result["comparisons"]}
    timing = comparison["ns/op"]
    assert timing["benchmark"] == "BenchmarkEncode/Large-4"
    assert timing["status"] == "comparable"
    assert timing["baseline"]["median"] == 100
    assert timing["candidate"]["median"] == 125
    assert timing["baseline"]["min"] == 99
    assert timing["candidate"]["max"] == 126
    assert timing["baseline"]["sample_count"] == timing["candidate"]["sample_count"] == 3
    assert timing["absolute_change"] == timing["relative_change_percent"] == 25
    assert timing["direction"] == "increased"
    assert timing["candidate"]["samples"][0] == {
        "value": 125,
        "original_value": ".125",
        "original_unit": "µs/op",
        "iterations": 1200,
        "evidence": "candidate:5",
    }
    assert timing["evidence"] == [
        "baseline:5",
        "baseline:6",
        "baseline:7",
        "candidate:5",
        "candidate:6",
        "candidate:7",
    ]
    assert timing["baseline_configuration"]["pkg"] == "example.com/encoder"
    assert timing["baseline_configuration_evidence"]["pkg"] == "baseline:3"
    assert comparison["B/s"]["relative_change_percent"] == -20
    assert comparison["B/s"]["better"] == "higher"
    assert comparison["misses/op"]["relative_change_percent"] == pytest.approx(100 / 1.5)
    assert comparison["misses/op"]["better"] == "lower"
    assert result["unit_metadata"]["candidate"]["misses/op"]["better"]["evidence"] == "candidate:4"
    assert comparison["B/op"]["direction"] == "unchanged"
    assert comparison["allocs/op"]["relative_change_percent"] == 0


@pytest.mark.parametrize(
    ("baseline_values", "candidate_values", "gap", "status", "relative"),
    [
        ([10], [12], "insufficient_repetitions", "weak", 20),
        ([0, 0, 0], [1, 1, 1], "zero_baseline", "comparable", None),
        ([100, 110, 190], [101, 120, 125], "overlapping_observations", "weak", 100 / 11),
    ],
)
def test_distinguishes_limited_observations_from_incomparable_input(
    tmp_path,
    baseline_values,
    candidate_values,
    gap,
    status,
    relative,
):
    result = _compare(tmp_path, _repetitions(baseline_values), _repetitions(candidate_values))
    (row,) = result["comparisons"]
    assert row["status"] == status
    assert row["relative_change_percent"] == (None if relative is None else pytest.approx(relative))
    assert gap in {issue["kind"] for issue in result["gaps"]}


def test_suppresses_deltas_for_workload_and_settings_mismatches(tmp_path):
    baseline = _repetitions([10, 11, 12])
    candidate = _repetitions([18, 19, 20])
    baseline_manifest = {
        "benchmark": {"package": "./encode", "results": "baseline.txt", "fixture_sha256": "a" * 64},
        "environment": {"cpu_model": "A"},
        "build": {"go_flags": ["-tags=fast"]},
        "collection": {"duration_seconds": 1},
        "workload": {"size_bytes": 512},
    }
    candidate_manifest = {
        "benchmark": {
            "package": "./encode",
            "results": "candidate.txt",
            "fixture_sha256": "b" * 64,
        },
        "environment": {"cpu_model": "B"},
        "build": {"go_flags": ["-tags=diagnostic"]},
        "collection": {"duration_seconds": 3},
        "workload": {"size_bytes": 1024},
    }
    result = _compare(tmp_path, baseline, candidate, baseline_manifest, candidate_manifest)
    (row,) = result["comparisons"]
    assert row["status"] == "incomparable"
    assert row["relative_change_percent"] is row["absolute_change"] is row["direction"] is None
    assert row["baseline"]["median"] == 11
    assert row["candidate"]["median"] == 19
    assert row["reasons"] == [
        "Run manifests differ in benchmark.fixture_sha256.",
        "Run manifests differ in workload.size_bytes.",
        "Run manifests differ in environment.cpu_model.",
        "Run manifests differ in build.go_flags.",
        "Run manifests differ in collection.duration_seconds.",
    ]


def test_keeps_configurations_names_and_units_separate(tmp_path):
    baseline = (
        "pkg: one\n"
        + _repetitions([10, 11, 12])
        + "pkg: two\n"
        + _repetitions([30, 31, 32], "attempts/op")
        + _repetitions([1], benchmark="BenchmarkEncode/Small-8")
    )
    candidate = (
        "pkg: one\n"
        + _repetitions([20, 21, 22])
        + "pkg: three\n"
        + _repetitions([60, 61, 62], "attempts/op")
        + _repetitions([1], benchmark="BenchmarkEncode/Small-4")
    )
    result = _compare(tmp_path, baseline, candidate)
    matched = [row for row in result["comparisons"] if row["status"] != "incomparable"]
    (row,) = matched
    assert row["unit"] == "ns/op"
    assert row["relative_change_percent"] == pytest.approx(1000 / 11)
    assert row["baseline_configuration"]["pkg"] == row["candidate_configuration"]["pkg"] == "one"
    (wrong_package,) = [row for row in result["comparisons"] if row["unit"] == "attempts/op"]
    assert wrong_package["status"] == "incomparable"
    assert wrong_package["relative_change_percent"] is None
    assert wrong_package["baseline_configuration"]["pkg"] == "two"
    assert wrong_package["candidate_configuration"]["pkg"] == "three"


def test_does_not_convert_different_custom_units(tmp_path):
    result = _compare(tmp_path, _repetitions([3], "misses/op"), _repetitions([4], "faults/op"))
    assert {row["unit"] for row in result["comparisons"]} == {"misses/op", "faults/op"}
    assert all(row["status"] == "incomparable" for row in result["comparisons"])
    assert all(row["relative_change_percent"] is None for row in result["comparisons"])


def test_reports_invalid_and_failed_output_with_line_locators(tmp_path):
    baseline = b"BenchmarkEncode/Large-4 1000 NaN ns/op\r\n\xff\r\nFAIL\r\n"
    result = _compare(tmp_path, baseline, _repetitions([10, 11, 12]))
    assert [(gap["kind"], gap["evidence"]) for gap in result["gaps"][:4]] == [
        ("invalid_benchmark_line", ["baseline:1"]),
        ("invalid_benchmark_line", ["baseline:2"]),
        ("benchmark_command_failed", ["baseline:3"]),
        ("no_benchmark_results", []),
    ]
    (row,) = result["comparisons"]
    assert row["status"] == "incomparable"
    assert row["baseline"]["sample_count"] == 0


def test_partial_results_are_weak_and_report_exact_integer_measurements(tmp_path):
    large = 9_007_199_254_740_993
    baseline = _repetitions([large] * 3, "B/op") + "BenchmarkEncode/Large-4 1000 10\n"
    result = _compare(tmp_path, baseline, _repetitions([large + 1] * 3, "B/op"))
    (row,) = result["comparisons"]
    assert row["status"] == "weak"
    assert row["absolute_change"] == 1
    assert row["baseline"]["median"] == large
    assert row["baseline"]["samples"][0]["value"] == large
    assert result["gaps"][0]["evidence"] == ["baseline:4"]
