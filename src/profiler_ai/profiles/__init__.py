from collections.abc import Callable
from pathlib import Path

from ._common import Citation, Profile, ProfileError
from .papi import PapiProfile
from .pprof import PprofProfile

__all__ = ["Profile", "ProfileError", "load_profile"]


def load_profile(
    format: str,
    path: Path,
    executable: Path | None,
    cite: Citation,
    *,
    cite_executable: Callable[[], str] | None = None,
) -> Profile:
    """Read a profile and attach a callback for citations against its original artifact."""
    if format == "pprof":
        return PprofProfile.load(path, executable, cite, cite_executable)
    if format in {"papi", "papi-hl", "papi-hl-json", "papi-json"}:
        return PapiProfile.load(path, cite)
    raise ProfileError(f"unsupported profile format: {format}")
