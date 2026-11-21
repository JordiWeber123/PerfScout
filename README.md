# PerfScout

Compare two measured Git revisions and investigate the performance difference with Codex. Install Git, Python 3.12 or newer, and uv, then run `uv sync --locked`. The default agent uses the local Codex SDK account configuration.

```sh
uv run perfscout investigate --repo ./service --base v1 --candidate v2 --baseline-run ./runs/v1 --candidate-run ./runs/v2 --output ./investigation
```

Choose a new output directory outside the repository and measurement bundles. Omit `--output` to use a unique temporary directory. Add `--agent none` to generate the measurements and evidence index offline.

## Desktop workspace

The optional Qt interface supports Python 3.12–3.14. Open a saved investigation by passing the directory containing `report.json`.

```sh
uv sync --locked --extra gui
uv run --extra gui perfscout gui ./investigation
```

Run `uv run --extra gui perfscout gui` to open the start window. **New investigation** takes a repository and two measurement bundles. Choose **Codex** for agent findings or **Measurements only** for an offline comparison. You can read the measurements as soon as they are saved. Opening a saved report works offline. If the checkout or a run directory has moved, use **Locate repository** or **Locate baseline/candidate run** in the affected pane.

In a report, hover or focus a citation to preview it, then click or press Enter to inspect it beside the finding. Select a metric to see individual repetitions. Open **Code and diff** to compare committed files, **Profiles** to explore captured measurements, or **Experiments** to read the proposed controls and expected outcomes.

Drag a pane by its title to dock or float it. The layout menu in the top right reopens panes and resets the layout. Use **Ctrl+O** to open a report, **Ctrl+N** to configure a run, **F6** to move between visible panes, **Escape** to dismiss a preview, and **Alt+Left** to return to the report.

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
