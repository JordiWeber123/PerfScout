"""Run local pprof on an address-only probe against the supplied executable."""

import os
import shutil
import subprocess
from concurrent.futures import Future
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic
from typing import BinaryIO

from ._common import MAX_ARTIFACT_BYTES, ProfileError

_TIMEOUT_S = 45


def _tool(name: str) -> str | None:
    if os.name == "nt":
        name += ".exe"
    for entry in os.get_exec_path():
        path = Path(entry)
        if path.is_absolute() and (tool := shutil.which(str(path / name))):
            return tool
    return None


def _read_output(stream: BinaryIO, output: Future[bytes]) -> None:
    try:
        chunks = []
        size = 0
        while chunk := stream.read(64 * 1024):
            size += len(chunk)
            if size > MAX_ARTIFACT_BYTES:
                raise ProfileError("local pprof produced too much symbolization output")
            chunks.append(chunk)
        output.set_result(b"".join(chunks))
    except (OSError, ProfileError) as error:
        output.set_exception(error)


def symbolize(executable: Path, probe: bytes) -> bytes:
    command = None
    if pprof := _tool("pprof"):
        command = [pprof]
    elif go := _tool("go"):
        command = [go, "tool", "pprof"]
    if command is None:
        raise ProfileError("local pprof tooling is unavailable (install pprof or Go)")
    if not executable.is_file():
        raise ProfileError("the supplied matching executable is unavailable")
    with TemporaryDirectory(prefix="profiler-ai-pprof-", ignore_cleanup_errors=True) as directory:
        profile = Path(directory, "addresses.pb.gz")
        profile.write_bytes(probe)
        env = {
            **os.environ,
            "PPROF_BINARY_PATH": directory,
            "DEBUGINFOD_URLS": "",
            "GOENV": "off",
            "GOFLAGS": "",
            "GOTOOLCHAIN": "local",
            "GOPROXY": "off",
            "GOWORK": "off",
        }
        command.extend(["-proto", "-symbolize=local", str(executable.resolve()), str(profile)])
        try:
            start = monotonic()
            with subprocess.Popen(
                command,
                cwd=directory,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            ) as process:
                assert process.stdout is not None
                output: Future[bytes] = Future()
                Thread(target=_read_output, args=(process.stdout, output), daemon=True).start()
                try:
                    data = output.result(timeout=_TIMEOUT_S)
                    status = process.wait(timeout=max(0, _TIMEOUT_S - (monotonic() - start)))
                finally:
                    if process.poll() is None:
                        process.kill()
                if status != 0:
                    raise ProfileError("local pprof failed to symbolize the supplied executable")
        except TimeoutError as error:
            raise ProfileError("local pprof symbolization timed out") from error
        except subprocess.TimeoutExpired as error:
            raise ProfileError("local pprof symbolization timed out") from error
        except OSError as error:
            raise ProfileError("local pprof could not read the supplied executable") from error
    return data
