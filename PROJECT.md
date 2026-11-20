

This project is for a profiler tool with AI agents in mind. It's purpose is to take a repo, a baseline commit and a candidate commit. It should take sets of measuremeants from profiler output (through a tool like PAPI, Go's default profiler, NVIDIA Nsight Compute, etc) of both baseline and candidate then analyze the codebases to identify the reason for the difference in performance and propose future experiments

An LLM should do the work exploring, analyzing and writing reports/future experiments of the database
The tool should eventually support a "bring-your-own-agent" model.
Initially, it should use the Codex SDK as that's the platform the developer uses.


The user's working repository shouldn't be affected by this tool, but if needed detached worktrees could be used

The project is a CLI tool. GUI can potentially be added later: 

```bash
<toolname> investigate \
  --repo ./my-service \
  --base <baseline-commit> \
  --candidate <candidate-commit> \
  --baseline-run ./measurements/baseline \
  --candidate-run ./measurements/candidate
```

Each measurement should contain

| Artifact | Purpose |
|---|---|
| **Manifest** | Revision, commands, environment, workload identity, collection settings |
| **Raw benchmark output** | Preserve individual observations and metrics |
| **Raw profiles** | Preserve the original diagnostic evidence |
| **Matching executable, when needed** | Support symbolization and instruction-level inspection |
| **Execution logs** | Record failures, warnings, and incomplete runs |

A simplified proposed manifest (subject to change if implementation demands it)

schema_version: 1
revision: "<full-commit-sha>"

```yaml
benchmark:
  adapter: go-test
  package: "./internal/encoder"
  selector: "^BenchmarkEncode/Large$"
  results: "benchmark.txt"
  fixture_sha256: "<hash>"

environment:
  go_version: "<recorded-version>"
  os: "linux"
  arch: "amd64"
  cpu_model: "<recorded-model>"
  gomaxprocs: 4

profiles:
  - kind: cpu
    format: pprof
    file: "cpu.pprof"
    executable: "encoder.test"
    collection_command: ["..."]
```

The complete version should have more build flags, tool version, settings, etc

Due to the nature of different profiler's output being heterogenous, compressed, binary, etc; there should be profiler adapters, each responsible for decoding, querying, and explaining its measurements. You can invoke existing profiler tooling underneath an adapter; you don’t need to reimplement every parser. 

The agent could receive tools resembling:

```
compare_benchmarks(baseline, candidate, benchmark)
describe_profile(profile_id)
get_hotspots(profile_id, metric, aggregation, limit)
get_callers(profile_id, function)
get_callees(profile_id, function)
get_source(revision, file, line_range)
get_diff(base, candidate, paths)
```

These return structured results with units and evidence references. The model can ask follow-up questions instead of receiving a giant, truncated profile dump.

Preserve profiler-specific information. A common interface should not flatten CPU samples, GPU kernel traces, and cache counters into one generic “cost” number. Have shared concepts where they fit, plus specialized capabilities.

For a first version, support PAPI, Go benchmark output and pprof CPU profiles

The model’s job is to connect evidence, formulate hypotheses, and select discriminating experiments.

| Output | Example |
|---|---|
| **Observation** | The candidate’s large-input benchmark is slower across repeated runs |
| **Supporting evidence** | Benchmark comparison, increased allocations per operation, a changed loop |
| **Hypothesis** | A buffer now grows repeatedly inside that loop |
| **Alternative explanation** | Extra conversion work causes the slowdown independently of allocation |
| **Missing evidence** | No allocation profile identifies the allocation sites |
| **Next experiment** | Collect an allocation profile for the same workload |
| **Interpretation rule** | Allocation growth at the changed loop strengthens the hypothesis; growth elsewhere redirects the investigation |

The "Next experiment" part can do different things. It can generate tests, collect another profile, or modify code. Those actions serve different purposes and should be recorded separately.

| Experiment type | What the tool does | Question answered |
|---|---|---|
| **Repeat measurement** | Collect additional benchmark observations | Is this a stable difference or noise? |
| **Change diagnostic collection** | Collect allocation, blocking, or another relevant profile | What kind of work or waiting increased? |
| **Vary workload** | Sweep input size or concurrency while holding other conditions fixed | Under what conditions does the regression appear? |
| **Add a focused benchmark** | Isolate a suspicious operation | Can the suspected mechanism be reproduced separately? |
| **Perform an intervention** | Revert one change or introduce a controlled variant | Does changing the suspected cause change the outcome? |
| **Try a repair** | Apply a candidate optimization | Can performance improve while preserving behavior? |

Represent each proposed experiment as structured data containing:
- The hypothesis and rationale.
- Exact revision and any proposed patch.
- Workload, command, and collection settings.
- What remains controlled.
- Expected outcomes and how each would affect the hypothesis.
- Time/resource budget and required execution permission.
That makes an experiment reviewable before execution and interpretable afterward.

The execution loop is something like: 

1. Compare revisions and verify the measured regression.
2. Inspect existing profiles and relevant source.
3. Form competing hypotheses.
4. Run an experiment that distinguishes them.
5. Update the findings; repeat if needed.
6. Generate a repair when the evidence supports one.
7. Run correctness checks and fresh performance measurements.

Keep three versions when repairing: baseline, regressed candidate, and proposed fix. That shows whether the fix recovers the regression and whether it introduces another trade-off.

The first deliverable is an Evidence-based investigator