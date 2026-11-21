from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

type Citation = Callable[[str], str]

MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_RESULT_ROWS = 100


class ProfileError(ValueError):
    """An unsupported artifact, malformed profile, or invalid profile query."""


class Profile(Protocol):
    """A loaded profiler artifact with format-specific evidence queries."""

    def describe(self) -> dict[str, Any]: ...

    def query(self, operation: str, params: Mapping[str, Any], limit: int) -> dict[str, Any]: ...


def read_artifact(path: Path) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(MAX_ARTIFACT_BYTES + 1)
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ProfileError("profile exceeds the 32 MiB artifact limit")
    return data


def result_limit(limit: int) -> int:
    if type(limit) is not int or limit < 1:
        raise ProfileError("profile query limit must be a positive integer")
    return min(limit, MAX_RESULT_ROWS)


def require_params(params: Mapping[str, Any], *supported: str) -> None:
    unsupported = params.keys() - set(supported)
    if unsupported:
        raise ProfileError(f"unsupported profile query parameter: {min(unsupported)}")


def display_text(value: str | None, limit: int = 512) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return value[:limit] + "…"
