import gzip
import json
from pathlib import Path
from urllib.parse import unquote

import pytest

from profiler_ai.profiles import ProfileError, load_profile

FIXTURES = Path(__file__).parent / "fixtures" / "profiles"


def _varint(value: int) -> bytes:
    chunks = []
    value %= 2**64
    while value > 127:
        chunks.append(value % 128 + 128)
        value //= 128
    return bytes([*chunks, value])


def _field(number: int, value: bytes | int) -> bytes:
    if isinstance(value, bytes):
        return _varint(number * 8 + 2) + _varint(len(value)) + value
    return _varint(number * 8) + _varint(value)


def _message(*fields: tuple[int, bytes | int]) -> bytes:
    return b"".join(_field(*field) for field in fields)


def _pprof_profile(*, symbolized: bool = True) -> bytes:
    strings = (
        "",
        "samples",
        "count",
        "cpu",
        "nanoseconds",
        "leaf",
        "inline",
        "root",
        "fixture.go",
        "thread",
        "worker",
    )
    fields = [(6, value.encode()) for value in strings]
    fields += [
        (1, _message((1, 1), (2, 2))),
        (1, _message((1, 3), (2, 4))),
        (11, _message((1, 3), (2, 4))),
        (12, 100),
        (10, 1000),
        (14, 3),
    ]
    for function_id, name in enumerate((5, 6, 7), start=1):
        fields.append((5, _message((1, function_id), (2, name), (4, 8), (5, function_id * 10))))
    for location_id, function_ids in {1: (1, 2), 2: (3,), 3: ()}.items():
        location_fields = [(1, location_id), (3, location_id * 1024)]
        if symbolized:
            for function_id in function_ids:
                line = _message((1, function_id), (2, function_id * 10 + 1), (3, 7))
                location_fields.append((4, line))
        fields.append((4, _message(*location_fields)))
    for stack, weights in [((1, 2, 1), (3, 300)), ((2,), (2, 200)), ((3, 2), (1, 100))]:
        # Both supported protobuf encodings of repeated values can appear in a profile.
        sample = _message(
            (1, b"".join(_varint(location_id) for location_id in stack)),
            (2, weights[0]),
            (2, weights[1]),
            (3, _message((1, 9), (2, 10))),
        )
        fields.append((2, sample))
    return gzip.compress(_message(*fields))


def _cite(locator: str) -> str:
    return locator


def test_pprof_preserves_weights_inline_order_and_recursive_cost(tmp_path):
    path = tmp_path / "cpu.pprof"
    path.write_bytes(_pprof_profile())
    profile = load_profile("pprof", path, None, _cite)
    description = profile.describe()
    assert description["default_metric_index"] == 1
    assert [(metric["unit"], metric["total_weight"]) for metric in description["metrics"]] == [
        ("count", 6),
        ("nanoseconds", 600),
    ]
    assert description["period"] == 100
    assert description["duration_ns"] == 1000
    assert description["sample_label_keys"] == ["thread"]
    assert description["unresolved_locations"] == 1
    assert "No matching executable" in description["gaps"][0]

    result = profile.query("hotspots", {"aggregation": "cumulative"}, 3)
    assert result["total_rows"] == 4
    rows = {row["function"]: row for row in result["rows"]}
    assert list(rows) == ["root", "inline", "leaf"]
    assert (rows["root"]["flat"], rows["root"]["cumulative"]) == (200, 600)
    assert (rows["leaf"]["flat"], rows["leaf"]["cumulative"]) == (300, 300)
    assert (rows["inline"]["flat"], rows["inline"]["cumulative"]) == (0, 300)
    assert rows["leaf"]["source_file"] == "fixture.go"
    assert rows["leaf"]["source_line"] == 11
    assert rows["leaf"]["source_column"] == 7
    assert rows["leaf"]["evidence"] == ["pprof:location/1", "pprof:sample/0"]

    callers = profile.query("callers", {"function": rows["leaf"]["function_id"], "metric": 0}, 10)
    assert [(row["function"], row["weight"]) for row in callers["rows"]] == [("inline", 3)]
    callees = profile.query("callees", {"function": "root"}, 10)
    assert [(row["function"], row["weight"]) for row in callees["rows"]] == [
        ("inline", 300),
        ("0xc00", 100),
    ]


def test_pprof_uses_local_symbol_probe_and_keeps_original_sample_citations(tmp_path, monkeypatch):
    path = tmp_path / "cpu.pprof"
    path.write_bytes(_pprof_profile(symbolized=False))

    def run_symbolizer(executable: Path, probe: bytes) -> bytes:
        assert executable == tmp_path / "benchmark.test"
        plain = gzip.decompress(probe)
        assert b"fixture.go" not in plain
        assert b"worker" not in plain
        return _pprof_profile()

    monkeypatch.setattr("profiler_ai.profiles.pprof.symbolize", run_symbolizer)
    profile = load_profile(
        "pprof", path, tmp_path / "benchmark.test", _cite, cite_executable=lambda: "executable"
    )
    assert profile.describe()["locations_symbolized_with_executable"] == 2
    result = profile.query("hotspots", {}, 1)
    assert result["rows"][0]["function"] == "leaf"
    assert result["rows"][0]["symbol_source"] == "executable"
    assert result["rows"][0]["evidence"] == ["pprof:location/1", "pprof:sample/0", "executable"]


def test_pprof_rejects_truncated_profile(tmp_path):
    path = tmp_path / "cpu.pprof"
    path.write_bytes(gzip.compress(b"\x32\x10\x00"))
    with pytest.raises(ProfileError, match="truncated field"):
        load_profile("pprof", path, None, _cite)


def _resolve_papi(document, reference):
    result = document
    for piece in unquote(reference.removeprefix("papi:#")).split("/")[1:]:
        piece = piece.replace("~1", "/").replace("~0", "~")
        result = result[int(piece) if isinstance(result, list) else piece]
    return result


def test_papi_reads_recorded_native_high_level_counts():
    path = FIXTURES / "papi-capture.json"
    profile = load_profile("papi-hl-json", path, None, _cite)
    assert profile.describe()["thread_count"] == 1
    records = profile.query("counters", {"thread": "0"}, 100)["rows"]
    readings = {record["event"]: record["value"] for record in records}
    assert readings["PAPI_TOT_INS"] == 33_007_026_164
    assert readings["perf::TASK-CLOCK"] == 8_886_388_468
    document = json.loads(path.read_bytes())
    for record in records:
        assert int(_resolve_papi(document, record["evidence"][0])) == record["value"]


def test_papi_preserves_event_attributes_threads_nested_regions_and_integer_precision():
    path = FIXTURES / "papi-threads.json"
    profile = load_profile("papi", path, None, _cite)
    assert profile.describe()["thread_count"] == 2
    params = {"event": "PAPI_TOT_CYC", "rank": 7, "region": "outer"}
    first_page = profile.query("counters", params, 1)
    assert first_page["total_rows"] == 2
    assert first_page["rows"][0]["value"] == 9_007_199_254_740_993
    [thread_two] = profile.query("counters", {**params, "offset": 1}, 50)["rows"]
    assert thread_two["value"] == 9_007_199_254_740_995
    assert thread_two["exact_value"] == "9007199254740995"
    assert thread_two["thread"] == "1"
    assert thread_two["rank"] == "7"

    [reading] = profile.query("counters", {"reading": "read_1"}, 50)["rows"]
    assert reading["parent_region_id"] == "1"
    assert reading["value"] == 5
    [instant] = profile.query("counters", {"event": "example:::state/a~b"}, 50)["rows"]
    assert (instant["value"], instant["unit"], instant["event_type"]) == (-2, "states", "instant")
    document = json.loads(path.read_bytes())
    for record in (reading, instant):
        assert int(_resolve_papi(document, record["evidence"][0])) == record["value"]
