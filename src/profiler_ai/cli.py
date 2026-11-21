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


def output_directory(requested: Path | None, protected: list[Path]) -> Path:
    destination = requested or Path(tempfile.gettempdir()) / f"profiler-ai-{uuid4().hex}"
    destination = destination.resolve()
    if any(destination.is_relative_to(directory) for directory in protected):
        raise ValueError(
            "Choose an output directory outside the inspected repository and run directories"
        )
    destination.mkdir(parents=True, exist_ok=False)
    return destination


def load_inputs(runs, index):
    profiles, descriptions, gaps, manifests, digests = {}, {}, [], {}, {}
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
    return profiles, descriptions, gaps, manifests, digests


def investigate(args, provider: Provider | None) -> tuple[Path, Report]:
    repo = Repository(args.repo)
    revisions = {"baseline": repo.resolve(args.base), "candidate": repo.resolve(args.candidate)}
    runs = {side: load_run(getattr(args, f"{side}_run"), sha) for side, sha in revisions.items()}
    out = output_directory(args.output, [repo.root, *(r.directory for r in runs.values())])
    with closing(EvidenceIndex(out / "evidence.sqlite3")) as index:
        profiles, descriptions, gaps, manifests, digests = load_inputs(runs, index)
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
        )
        write_report(out, report)
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


def main(argv=None, *, provider: Provider | None = None) -> int:
    parser = argparse.ArgumentParser(prog="profiler-ai")
    command = parser.add_subparsers(dest="command", required=True).add_parser("investigate")
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
    try:
        out, report = investigate(args, provider)
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
