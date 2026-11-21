"""Verify the Codex dialogue offline, including the installed SDK's call signatures."""

import json
from unittest.mock import create_autospec

from openai_codex import ApprovalMode, Codex, ExternalMessage, Sandbox, Thread

from profiler_ai.agent import CodexProvider


def test_codex_reads_query_result_before_submitting_findings(monkeypatch):
    session = create_autospec(Codex, instance=True)
    session.__enter__.return_value = session
    thread = create_autospec(Thread, instance=True)
    session.thread_start.return_value = thread
    monkeypatch.setattr("openai_codex.Codex", lambda config: session)
    checked = []

    def respond(message):
        assert isinstance(message, ExternalMessage)
        checked.append(json.loads(message.content))
        if len(checked) == 1:
            action = {"query": "benchmarks", "params": {"limit": 1}}
        else:
            action = {
                "findings": {
                    "conclusion": "inconclusive",
                    "observations": [
                        {
                            "text": "One observation",
                            "evidence": ["E999" if len(checked) == 2 else "E1"],
                        }
                    ],
                    "hypotheses": [],
                    "alternatives": [],
                    "missing_evidence": ["More repetitions"],
                    "confidence_limits": "Only one run.",
                    "experiments": [
                        {
                            "kind": "repeat",
                            "hypothesis": "The difference persists",
                            "rationale": "Check variance",
                            "revision": "candidate",
                            "workload": "Encode",
                            "command_or_patch": "go test -bench Encode",
                            "collection_settings": "-count 10",
                            "controls": "Same input",
                            "budget": "1 min",
                            "permission": "Run benchmark",
                            "interpretation": "Consistent deltas would strengthen the hypothesis.",
                        }
                    ],
                }
            }
        from types import SimpleNamespace

        return SimpleNamespace(final_response=json.dumps(action))

    thread.run.side_effect = respond

    def query(name, params):
        assert (name, params) == ("benchmarks", {"limit": 1})
        return {"rows": [{"evidence": ["artifact:" + "a" * 64]}]}

    findings = CodexProvider().investigate(
        {"benchmark_rows": 1, "revisions": {"candidate": "abc"}}, query
    )
    assert checked[1] == {"rows": [{"evidence": ["E1"]}]}
    assert "Unknown citation" in checked[2]["error"]
    assert findings.observations[0].evidence == ["artifact:" + "a" * 64]
    assert findings.experiments[0].revision == "abc"
    assert session.thread_start.call_args.kwargs["sandbox"] == Sandbox.read_only
    assert session.thread_start.call_args.kwargs["approval_mode"] == ApprovalMode.deny_all
