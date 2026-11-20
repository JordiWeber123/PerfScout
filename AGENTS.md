# Repository Guidelines

## Project Structure & Module Organization

This is currently a specification-first repository: `PROJECT.md` defines an evidence-based CLI for comparing a baseline and candidate revision using profiler artifacts. No application source, tests, or build configuration exist yet.

When implementation begins, use a clear Go-oriented layout:

- `cmd/` for CLI entry points and flag wiring.
- `internal/` for investigation orchestration, worktrees, manifests, experiment records, and reports.
- `adapters/` for profiler integrations; begin with PAPI, Go benchmark output, and pprof CPU profiles.
- `testdata/` for small, versioned manifests and sanitized profiler fixtures.

Keep raw artifacts immutable and preserve profiler-specific fields; a common interface must not reduce CPU, GPU, and counter data to a single generic metric.

## Build, Test, and Development Commands

There are no runnable commands yet. Once Go tooling is committed, the expected baseline is:

```powershell
go build ./...  # Compile every package
go test ./...   # Run unit and integration tests
go vet ./...    # Run static analysis
```

Only add commands to this guide after their configuration and dependencies are checked in.

## Coding Style & Naming Conventions

Follow the formatter and idioms of the chosen language. For Go, run `gofmt` on changed files and `go vet ./...` before review. Use focused, domain-driven names such as `PprofAdapter`, `BaselineManifest`, and `ExperimentPlan`. Keep exported APIs small and document exported types.

Make units unambiguous: prefer fields such as `DurationNs` and `AllocBytes`, or include an explicit unit. Return structured results with evidence references instead of unbounded profile dumps.

## Testing Guidelines

Place tests beside the package they cover and use Go names such as `TestPprofAdapterReportsHotspots`. Prefer deterministic fixtures in `testdata/`. Test parsing, unit conversion, evidence references, revision selection, and hypothesis interpretation independently. Integration tests must use disposable repositories or detached worktrees so a user's working tree remains unchanged.

## Commits and Pull Requests

Recent history uses concise, imperative summaries (for example, `Little detail in PROJECT.md`). Continue that style: `Add pprof CPU adapter`. Keep commits narrowly scoped. Pull requests should describe behavior changes, validation performed, affected artifact formats or schema changes, and representative CLI/report output when useful.

## Security and Configuration

Treat repositories and profiling artifacts as untrusted. Never run recorded collection commands without explicit approval. Do not commit credentials, absolute local paths, machine-identifying details, or customer profile captures; sanitize fixtures before adding them.
