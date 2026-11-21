# Profiler AI

Compare two measured Git revisions and investigate the performance difference with Codex. Install Git, Python 3.12 or newer, and uv, then run `uv sync --locked`. The default agent uses the local Codex SDK account configuration.

```sh
uv run profiler-ai investigate --repo ./service --base v1 --candidate v2 --baseline-run ./runs/v1 --candidate-run ./runs/v2 --output ./investigation
```

Choose a new output directory outside the repository and measurement bundles. Omit `--output` to use a unique temporary directory. Add `--agent none` to generate the measurements and evidence index offline.

## Measurement bundle

Each run directory contains a manifest and its referenced files. For example, replace the revision below with the full commit ID from the measured checkout.

```yaml
schema_version: 1
revision: "<full commit ID>"
benchmark:
  adapter: go-test
  results: benchmark.txt
  selector: BenchmarkEncode
environment:
  gomaxprocs: 4
profiles:
  - kind: cpu
    format: pprof
    file: cpu.pprof
    executable: encoder.test
  - kind: counters
    format: papi-hl-json
    file: rank_0.json
logs: [execution.log]
```

The benchmark file contains Go benchmark output, including individual repetitions. Record workload identity, build options, and collection settings in the `workload`, `build`, and `collection` mappings. The `profiles` and `logs` lists are optional. Include a matching executable for address-only profiles when available. Local symbolization uses `pprof` or `go tool pprof`. A profile marked `optional: true` can be absent.

## Results

`report.md` presents the findings and proposed experiments. `report.json` holds the structured findings and measurements. `evidence.sqlite3` maps citation IDs to content digests, artifact locations, or committed source and diffs. Retain the input bundles and Git objects to revisit those citations.

The command prints the result directory. Exit code 0 means the requested analysis completed, 1 means agent analysis failed after measurements were saved, and 2 indicates an input, invocation, or storage error.
