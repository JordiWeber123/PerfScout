"""Validated investigation findings and their human-readable rendering."""

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Claim(Model):
    text: Text
    evidence: list[Text] = Field(min_length=1)


class Experiment(Model):
    kind: Literal["repeat", "diagnostic", "workload", "benchmark", "intervention", "repair"]
    hypothesis: Text
    rationale: Text
    revision: Text
    workload: Text
    command_or_patch: Text
    collection_settings: Text
    controls: Text
    budget: Text
    permission: Text
    interpretation: Text


class Findings(Model):
    conclusion: Literal["supported", "inconclusive"]
    observations: list[Claim] = Field(min_length=1)
    hypotheses: list[Claim]
    alternatives: list[Text]
    missing_evidence: list[Text]
    confidence_limits: Text
    experiments: list[Experiment] = Field(min_length=1)


class Report(Model):
    schema_version: Literal[1] = 1
    revisions: dict[str, str]
    benchmarks: dict
    profiles: dict
    gaps: list[str]
    findings: Findings | None
    status: Literal["complete", "agent_failed", "measurements_only"]
    error: str | None = None
    citations: dict[str, dict]
    provenance: dict | None = None


def render(report: Report) -> str:
    lines = ["# Performance investigation", "", f"Status: {report.status}", ""]
    for side, revision in report.revisions.items():
        lines.append(f"- {side.title()}: `{revision}`")
    lines.extend(["", "## Measurements", ""])
    for row in report.benchmarks["comparisons"]:
        delta = row.get("relative_change_percent")
        change = f"{delta:+.3g}%" if delta is not None else "undetermined"
        before, after = row["baseline"], row["candidate"]
        levels = [
            f"{side['median']} (n={side['sample_count']})" if side["sample_count"] else "no samples"
            for side in (before, after)
        ]
        lines.append(
            f"- {row['benchmark']} ({row['unit']}): "
            f"{levels[0]} → {levels[1]}, "
            f"{change} ({row['status']})."
        )
    gaps = [*report.gaps, *(g["message"] for g in report.benchmarks["gaps"])]
    if gaps:
        lines.extend(["", "## Measurement gaps", "", *(f"- {g}" for g in gaps)])
    if report.error:
        lines.extend(["", "## Agent failure", "", report.error])
    if f := report.findings:
        lines.extend(["", f"## Findings ({f.conclusion})", ""])
        for title, claims in [("Observations", f.observations), ("Hypotheses", f.hypotheses)]:
            lines.extend([f"### {title}", ""])
            lines.extend(f"- {c.text} [{', '.join(c.evidence)}]" for c in claims)
            lines.append("")
        for title, items in [
            ("Alternative explanations", f.alternatives),
            ("Missing evidence", f.missing_evidence),
        ]:
            lines.extend([f"### {title}", "", *(f"- {s}" for s in items), ""])
        lines.extend(["### Confidence limits", "", f.confidence_limits, "", "## Experiments", ""])
        for experiment in f.experiments:
            lines.extend([f"### {experiment.hypothesis}", ""])
            for key, value in experiment.model_dump(exclude={"hypothesis"}).items():
                lines.append(f"- {key.replace('_', ' ').capitalize()}: {value}")
            lines.append("")
    if report.citations:
        lines.extend(["", "## Cited evidence", ""])
        for ref, item in report.citations.items():
            lines.append(
                f"- `{ref}` — {item.get('path') or item.get('revision')} {item['locator']}"
            )
    return "\n".join(lines) + "\n"


def write_report(directory: Path, report: Report) -> None:
    for name, content in [
        (
            "report.json",
            json.dumps(report.model_dump(), ensure_ascii=False, indent=2, allow_nan=False),
        ),
        ("report.md", render(report)),
    ]:
        temporary = directory / (name + ".tmp")
        temporary.write_text(content + ("" if content.endswith("\n") else "\n"), encoding="utf-8")
        temporary.replace(directory / name)
