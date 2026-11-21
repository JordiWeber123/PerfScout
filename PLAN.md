# Build the evidence-based performance investigator

## First release and stack

Ship a CLI that answers three questions about two measured revisions: **What changed in performance? Which code changes could explain it? What experiment would distinguish the leading explanations?** The first release analyzes supplied artifacts and ends with a cited report and proposed experiments. Its supported inputs are Go benchmark text, pprof CPU profiles, and PAPI high-level JSON.

**Use Python 3.12+ for the application.** Most of the work is reading heterogeneous artifacts, comparing measurements, querying Git, and coordinating an agent. Python fits those tasks and has an official Codex SDK. TypeScript also has an official SDK, and Go has strong pprof libraries. Python is the better fit here because it combines direct SDK access, statistical tooling, and local SQLite storage in one application runtime. The profiler formats determine the adapter implementations. [Codex SDK documentation](https://developers.openai.com/codex/sdk/) · [pprof documentation](https://pkg.go.dev/github.com/google/pprof)

Use `argparse` for the CLI, Pydantic for validated manifests and reports, PyYAML for manifest loading, and `sqlite3` for the evidence index. Package with `pyproject.toml` and `uv`. In the first implementation change, amend `AGENTS.md` to replace the Go scaffolding with the actual Python layout, `pytest` conventions, and Ruff checks. Retain its guidance on units, evidence references, sanitized fixtures, and repository isolation.

## Implementation milestones

1. **Deliver a working benchmark comparison first.** Implement `perfscout investigate --repo PATH --base REV --candidate REV --baseline-run DIR --candidate-run DIR`, with optional `--output DIR`. Resolve both revisions to full commit IDs and validate them against the run manifests. Capture workload identity, environment, build and collection settings, artifact paths, and execution logs. Store results in a per-investigation directory outside the target repository. Read committed source and diffs through Git’s object database so the command works even when the user has uncommitted edits.

2. **Build the evidence index and three adapters.** Register each artifact by digest and give each observation a locator that resolves to its source. The Go benchmark adapter should retain individual repetitions and custom metrics, match comparable workloads and units, and report sample counts, medians, spread, and relative changes. Report insufficient repetitions or mismatched settings explicitly. The pprof adapter should decode the upstream profile protobuf, preserve sample types and units, and use weighted stacks for flat cost, cumulative cost, callers, and callees. Use a matching executable via pprof when symbolization requires it. The PAPI adapter should ingest native high-level JSON and preserve event names, counter precision, regions, and thread or rank identity where supplied. [pprof format](https://pkg.go.dev/github.com/google/pprof) · [PAPI high-level JSON example](https://icl.utk.edu/files/publications/2020/icl-utk-1322-2020.pdf)

3. **Connect Codex to bounded evidence queries.** Define a provider-independent interface for requesting evidence and submitting findings. Expose benchmark comparisons, profile descriptions, hotspots, call edges, PAPI counters, revision-specific source lines, and diffs. Return units, provenance, and result limits with each query. Implement the first provider with the Python Codex SDK in a read-only session. Let the agent choose follow-up queries while the application owns calculations and validates evidence references. If the agent fails, retain the completed comparisons and diagnose the failed investigation stage.

4. **Produce a reviewable report and experiment plan.** Write `report.json` as the canonical structured result and render `report.md` from it. Include the measured observation, supporting citations, competing hypotheses, missing evidence, and confidence limits. Each proposed experiment must identify its type, rationale, revision, workload, command or patch where applicable, controls, budget, permissions, and how possible outcomes would change the conclusion. A finding of *inconclusive* is a valid result.

## Acceptance criteria

- Run the installed CLI against a two-commit fixture with repeated benchmarks and a known changed function. Verify the numerical delta independently and resolve every report citation to its artifact or committed source lines.
- Test noisy measurements, a single repetition, a zero baseline, mismatched workloads, and a manifest pointing to the wrong revision. Distinguish a valid but weak comparison from an invalid input.
- Verify pprof flat and cumulative costs on a known call stack, and verify per-thread PAPI event counts against a captured high-level JSON fixture. A missing symbol or optional profile should produce a specific evidence gap.
- Run end-to-end tests with a scripted agent on Windows and Linux, including a dirty target checkout. Verify its HEAD, index, and working files after investigation. Use one opt-in live Codex smoke test to check the real SDK integration.

## Later milestones

Add approved experiment execution in disposable checkouts, recording each run as new evidence. Then add interventions and repairs with correctness checks and fresh **baseline, candidate, and proposed-fix** measurements. Use the provider interface to add a second agent and the adapter interface to add Nsight Compute once representative captures are available.
