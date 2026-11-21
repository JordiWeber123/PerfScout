"""Local evidence queries shared by agent providers."""

import json


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}]")
    return value


class Queries:
    def __init__(self, repository, revisions, index, benchmarks, profiles, manifests):
        self.repository = repository
        self.revisions = revisions
        self.index = index
        self.benchmarks = benchmarks
        self.profiles = profiles
        self.manifests = manifests

    def __call__(self, operation: str, params: dict) -> dict:
        if not isinstance(params, dict):
            raise TypeError("Expected an object of query parameters")
        limit = integer(params.get("limit", 10), "limit", 1, 50)
        if operation in {"benchmarks", "samples"}:
            rows = self.benchmarks["comparisons"]
            offset = integer(params.get("offset", 0), "offset", 0, 2**31 - 1)
            if operation == "samples":
                row_id = integer(params["row"], "row", 0, len(rows) - 1)
                side = params["side"]
                if side not in self.revisions:
                    raise ValueError("Unknown revision side")
                table = rows[row_id][side]["samples"]
                result = {
                    "rows": table[offset : offset + limit],
                    "total": len(table),
                    "unit": rows[row_id]["unit"],
                }
            else:
                summaries = []
                for i, row in enumerate(rows[offset : offset + limit], offset):
                    summary = {
                        k: row[k]
                        for k in (
                            "benchmark",
                            "unit",
                            "status",
                            "reasons",
                            "relative_change_percent",
                            "direction",
                            "better",
                            "absolute_change",
                        )
                    }
                    summary["row"] = i
                    for side in self.revisions:
                        summary[side] = {
                            k: row[side][k] for k in ("sample_count", "median", "min", "max")
                        }
                        summary[side]["evidence"] = [
                            s["evidence"] for s in row[side]["samples"][:2]
                        ]
                    summaries.append(summary)
                result = {"rows": summaries, "total": len(rows)}
        elif operation == "settings":
            side, section = params["side"], params["section"]
            result = {
                "settings": self.benchmarks["settings"][side][section],
                "evidence": self.manifests[side],
            }
        elif operation in {"describe_profile", "hotspots", "callers", "callees", "counters"}:
            profile = self.profiles[params["profile"]]
            args = {k: v for k, v in params.items() if k not in {"profile", "limit"}}
            result = (
                profile.describe()
                if operation == "describe_profile"
                else profile.query(operation, args, limit)
            )
        elif operation == "source":
            sha = self.revisions[params["side"]]
            start = integer(params["start"], "start", 1, 2**31 - 1)
            end = integer(params["end"], "end", start, start + 99)
            path = params["path"]
            content = self.repository.source(sha, path, start, end)
            # The repository reader clamps requests that extend past EOF.
            actual_end = start + content.count("\n") - int(content.endswith("\n"))
            locator = json.dumps(
                {"path": path, "start": start, "end": actual_end}, ensure_ascii=False
            )
            result = {
                "text": content,
                "evidence": self.index.record_git(self.repository.root, sha, locator),
            }
        elif operation == "diff":
            paths = params.get("paths", [])
            if (
                not isinstance(paths, list)
                or len(paths) > 16
                or not all(isinstance(p, str) for p in paths)
            ):
                raise ValueError("paths must be an array of up to 16 repository paths")
            base, head = self.revisions["baseline"], self.revisions["candidate"]
            content = self.repository.diff(base, head, paths)
            locator = json.dumps({"diff_from": base, "paths": paths})
            result = {
                "text": content[:12000],
                "truncated": len(content) > 12000,
                "evidence": self.index.record_git(self.repository.root, head, locator),
            }
        else:
            raise ValueError(f"Unknown evidence query: {operation}")
        if len(json.dumps(result)) > 32000:
            raise ValueError(
                "Result exceeds the query budget. Narrow the selection or lower limit."
            )
        return result
