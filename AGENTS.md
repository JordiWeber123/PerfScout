# Repository Guidelines

## Structure and responsibilities

The Python package lives in `src/profiler_ai/`. Start at `cli.py` to follow a run from input validation through report generation. Keep Git object reads in `repository.py`, artifact indexing in `evidence.py`, and measurement calculations in `benchmarks.py` and `profiles/`. The agent chooses evidence queries through `queries.py`. Keep model-specific session code behind the provider interface in `agent.py`.

`PROJECT.md` describes the product. `PLAN.md` defines the first release and later milestones. The current investigator consumes existing measurement bundles and proposes experiments.

## Development commands

- `uv sync --locked` installs the package and development dependencies using Python 3.12 or newer.
- `uv run profiler-ai investigate --help` shows the installed CLI options.
- `uv run pytest` runs the offline suite. The live Codex investigation requires a configured account and explicit opt-in via `PROFILER_AI_LIVE_CODEX=1`.
- `uv run ruff check .` and `uv run ruff format --check .` check lint and formatting.

## Implementation and test conventions

Use four-space indentation, snake_case functions, and PascalCase classes. Preserve original units, sample counts, revision IDs, and citations when adding adapters. Keep the raw artifacts intact. Query committed Git objects and place investigation outputs outside the inspected checkout.

Add behavior-focused tests under `tests/test_*.py`. Use disposable Git repositories and small sanitized captures in `tests/fixtures/`. Check both the numeric result and its evidence locator. Use a scripted provider for deterministic integration tests.

## Commits and pull requests

History uses short descriptive subjects. Keep each change focused, for example `Preserve PAPI thread identities`. Describe changes to supported formats, report schemas, and CLI behavior in the pull request. Include the checks run and representative output when it clarifies a behavior change.
