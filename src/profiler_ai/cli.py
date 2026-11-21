"""Command-line entry point for investigations of previously collected runs."""

import argparse
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from .agent import CodexProvider, Provider
from .benchmarks import compare_benchmarks
from .evidence import EvidenceIndex
from .inputs import load_run
from .profiles import load_profile
from .queries import Queries
from .report import Findings, Report, write_report
from .repository import Repository


def validate_output_path(destination: Path, protected: list[Path]) -> Path:
    """Validate an output path without creating it; creation must still be exclusive."""
    destination = destination.resolve()
    if any(destination.is_relative_to(directory) for directory in protected):
        raise ValueError(
            "Choose an output directory outside the inspected repository and run directories"
        )
    if destination.exists():
        raise ValueError("The output directory must not already exist")
    return destination


def output_directory(requested: Path | None, protected: list[Path]) -> Path:
    destination = requested or Path(tempfile.gettempdir()) / f"perfscout-{uuid4().hex}"
    destination = validate_output_path(destination, protected)
    destination.mkdir(parents=True, exist_ok=False)
    return destination


def load_inputs(runs, index):
    profiles, descriptions, gaps, manifests, digests, provenance = {}, {}, [], {}, {}, {}
    for side, run in runs.items():
        manifests[side] = index.cite(index.register(run.manifest_path), "manifest")
        digests[side] = index.register(run.artifact(run.manifest.benchmark.results))
        for name in run.manifest.logs:
            index.register(run.artifact(name))
        if not run.manifest.profiles:
            gaps.append(f"{side}: no profiles supplied.")
        for number, entry in enumerate(run.manifest.profiles):
            key = f"{side}/{number}"
            try:
                path = run.artifact(entry.file)
            except FileNotFoundError:
                if not entry.optional:
                    raise
                gaps.append(f"{key}: optional profile {entry.file} is missing.")
                continue
            digest = index.register(path)
            provenance[key] = {"side": side, "file": str(path), "digest": digest}
            executable = None
            executable_citation = None
            if entry.executable:
                try:
                    executable = run.artifact(entry.executable)
                except FileNotFoundError:
                    gaps.append(f"{key}: executable {entry.executable} is missing.")
                else:
                    exe_digest = index.register(executable)
                    executable_citation = index.cite(exe_digest, "executable")
            profile = load_profile(
                entry.format,
                path,
                executable,
                lambda loc, d=digest: index.cite(d, loc),
                cite_executable=(lambda ref=executable_citation: ref)
                if executable_citation
                else None,
            )
            description = profile.describe()
            descriptions[key] = description
            profiles[key] = profile
            gaps.extend(f"{key}: {gap}" for gap in description.get("gaps", []))
    return profiles, descriptions, gaps, manifests, digests, provenance


def investigate(args, provider: Provider | None, progress=None) -> tuple[Path, Report]:
    notify = progress or (lambda event, value: None)
    notify("progress", "Validating repository and runs")
    repo = Repository(args.repo)
    revisions = {"baseline": repo.resolve(args.base), "candidate": repo.resolve(args.candidate)}
    runs = {side: load_run(getattr(args, f"{side}_run"), sha) for side, sha in revisions.items()}
    out = output_directory(args.output, [repo.root, *(r.directory for r in runs.values())])
    with closing(EvidenceIndex(out / "evidence.sqlite3")) as index:
        notify("progress", "Indexing measurements and profiles")
        profiles, descriptions, gaps, manifests, digests, profile_sources = load_inputs(runs, index)
        baseline, candidate = runs["baseline"], runs["candidate"]
        benchmarks = compare_benchmarks(
            baseline.artifact(baseline.manifest.benchmark.results),
            candidate.artifact(candidate.manifest.benchmark.results),
            baseline.manifest.model_dump(mode="json", exclude_none=True),
            candidate.manifest.model_dump(mode="json", exclude_none=True),
            lambda side, line: index.cite(digests[side], f"line:{line}"),
        )
        for gap in benchmarks["gaps"]:
            if gap["message"].startswith("Run manifests differ"):
                gap["evidence"] = list(manifests.values())
        report = Report(
            revisions=revisions,
            benchmarks=benchmarks,
            profiles=descriptions,
            gaps=gaps,
            findings=None,
            status="measurements_only",
            citations={ref: index.lookup(ref) for ref in manifests.values()},
            provenance={
                "repository": str(repo.root),
                "runs": {side: str(run.directory) for side, run in runs.items()},
                "profiles": profile_sources,
            },
        )
        write_report(out, report)
        notify("checkpoint", str(out))
        if args.agent == "none":
            return out, report
        catalog = {
            "revisions": revisions,
            "manifests": manifests,
            "benchmark_rows": len(benchmarks["comparisons"]),
            "profiles": {k: v["format"] for k, v in descriptions.items()},
            "gaps": [s[:500] for s in gaps[:16]],
            "benchmark_gaps": [g["message"][:500] for g in benchmarks["gaps"][:16]],
        }
        try:
            notify("progress", "Investigating evidence with the agent")
            query = Queries(repo, revisions, index, benchmarks, profiles, manifests)
            findings = Findings.model_validate(
                (provider or CodexProvider()).investigate(catalog, query)
            )
            citations = {}
            for claim in findings.observations + findings.hypotheses:
                for ref in claim.evidence:
                    entry = index.lookup(ref)
                    if entry is None:
                        raise ValueError(f"Agent cited unknown evidence {ref!r}")
                    citations[ref] = entry
            if any(exp.revision not in revisions.values() for exp in findings.experiments):
                raise ValueError("Experiment references an unmeasured revision")
            report.findings = findings
            report.citations.update(citations)
            report.status = "complete"
        except Exception as exc:  # noqa: BLE001
            # Provider/transport failures must preserve the measurement-only checkpoint.
            report.status = "agent_failed"
            report.error = f"Agent analysis: {type(exc).__name__}: {exc}"
        write_report(out, report)
    return out, report


def main(argv=None, *, provider: Provider | None = None, progress=None) -> int:
    parser = argparse.ArgumentParser(prog="perfscout")
    commands = parser.add_subparsers(dest="command", required=True)
    gui = commands.add_parser("gui", help="Open the desktop investigation workspace")
    gui.add_argument("directory", nargs="?", type=Path, help="Existing investigation directory")
    command = commands.add_parser("investigate")
    command.add_argument("--repo", required=True, type=Path)
    command.add_argument("--base", required=True)
    command.add_argument("--candidate", required=True)
    for side in ("baseline", "candidate"):
        command.add_argument(f"--{side}-run", required=True, type=Path)
    command.add_argument(
        "--output", type=Path, help="New investigation directory (default: a temporary path)"
    )
    command.add_argument(
        "--agent",
        choices=("codex", "none"),
        default="codex",
        help="Request Codex findings or stop after recording measurements",
    )
    args = parser.parse_args(argv)
    if args.command == "gui":
        try:
            from .gui.app import launch
        except ModuleNotFoundError as exc:
            if exc.name != "PySide6":
                raise
            print(
                "The desktop app requires Python 3.12–3.14 and the gui extra. "
                "Launch it with uv run --extra gui perfscout gui.",
                file=sys.stderr,
            )
            return 2
        return launch(args.directory)
    try:
        out, report = investigate(args, provider, progress)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"Investigation failed: {exc}", file=sys.stderr)
        return 2
    print(out)
    if report.status == "agent_failed":
        print(report.error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
