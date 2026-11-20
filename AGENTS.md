# Repository Guidelines

## Project Structure & Module Organization

This repository currently contains the product specification in `PROJECT.md`; implementation, tests, and build configuration have not yet been created. The project will become a CLI for evidence-based performance investigations.

When adding the first implementation, keep concerns separate:

- `cmd/` for the CLI entry point and command wiring.
- `internal/` for orchestration, repository/worktree handling, experiment records, and reporting.
- `adapters/` for profiler-specific integrations (initially PAPI, Go benchmark output, and pprof CPU profiles).
- `testdata/` for small, versioned profiler fixtures and manifests; never add large raw captures unless essential.

Preserve source artifacts and manifests. Adapters should return structured, evidence-referenced results without flattening profiler-specific data.

## Build, Test, and Development Commands

There are no runnable commands yet. Add canonical commands when tooling is introduced. A Go-oriented initial layout would normally support:

```powershell
go build ./...       # compile all packages
go test ./...        # run unit and integration tests
go vet ./...         # run static checks
```

Only document commands whose configuration and dependencies are committed.

## Coding Style & Naming Conventions

Follow the formatter and idioms of the selected language; for Go, run `gofmt` and `go vet`. Use domain-focused names such as `BaselineManifest`, `PprofAdapter`, and `ExperimentPlan`. Keep public APIs small. Do not hide units: use `DurationNs`, `AllocBytes`, or an explicit unit field.

## Testing Guidelines

Add tests alongside covered code, using behavior names such as `TestPprofAdapterReportsHotspots`. Prefer deterministic fixtures in `testdata/`; redact host paths, tokens, and machine-identifying details. Test parsing, unit conversion, evidence references, revision selection, and experiment interpretation independently. Integration tests must use disposable repositories or detached worktrees.

## Commit & Pull Request Guidelines

The repository has no commits yet, so no convention can be inferred. Use short, imperative subjects such as `Add pprof CPU adapter`. Keep commits focused. Pull requests should explain the behavior change, validation, affected formats, and schema changes. Include representative CLI or report output when helpful; never include sensitive profiles or credentials.

## Security & Configuration

Treat repositories and profiler artifacts as untrusted input. Never execute recorded collection commands automatically without explicit user approval. Keep credentials, absolute local paths, and raw customer artifacts out of version control.
