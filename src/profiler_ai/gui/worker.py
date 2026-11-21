"""Run investigations in an isolated process and relay checkpoint events to Qt."""

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal


def _stop_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class RunController(QObject):
    progress = Signal(str)
    checkpoint = Signal(str)
    finished = Signal(str, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._active = False
        self._cancelled = False
        self._process = None

    @property
    def active(self):
        with self._lock:
            return self._active

    def start(self, args):
        with self._lock:
            if self._active:
                raise RuntimeError("An investigation is already running")
            self._active = True
            self._cancelled = False
        threading.Thread(target=self._run, args=(list(args),), daemon=True).start()

    def cancel(self):
        with self._lock:
            if not self._active:
                return
            self._cancelled = True
            process = self._process
        if process is not None:
            threading.Thread(target=_stop_tree, args=(process,), daemon=True).start()

    def _run(self, args):
        directory = ""
        status = "failed"
        diagnostic = ""
        try:
            kwargs = (
                {"start_new_session": True}
                if os.name != "nt"
                else {
                    "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.CREATE_NO_WINDOW
                }
            )
            with tempfile.TemporaryFile(mode="w+b") as errors:
                process = subprocess.Popen(
                    [sys.executable, "-m", "profiler_ai.gui.worker", "--child", *args],
                    stdout=subprocess.PIPE,
                    stderr=errors,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    **kwargs,
                )
                with self._lock:
                    self._process = process
                    cancelled = self._cancelled
                if cancelled:
                    _stop_tree(process)
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                        kind, value = event["event"], event["value"]
                        if kind == "progress":
                            self.progress.emit(str(value))
                        elif kind == "checkpoint":
                            directory = str(value)
                            self.checkpoint.emit(directory)
                        elif kind == "finished":
                            status, diagnostic = value["status"], value["diagnostic"]
                    except (ValueError, KeyError, TypeError):
                        continue
                code = process.wait()
                errors.seek(0)
                stderr = errors.read().decode("utf-8", errors="replace").strip()
                if code and not diagnostic:
                    diagnostic = stderr or f"Investigation process exited with code {code}."
        except (OSError, ValueError) as exc:
            diagnostic = str(exc)
        finally:
            with self._lock:
                cancelled = self._cancelled
                self._process = None
                self._active = False
            if cancelled:
                status = "cancelled"
                diagnostic = "Investigation cancelled."
            self.finished.emit(directory, status, diagnostic)


def _child(args):
    from ..cli import main

    def emit(event, value):
        print(json.dumps({"event": event, "value": value}), flush=True)

    output, errors = io.StringIO(), io.StringIO()
    directory = ""

    def notify(event, value):
        nonlocal directory
        if event == "checkpoint":
            directory = value
        # Write protocol events to the original stream, not captured CLI output.
        with contextlib.redirect_stdout(sys.__stdout__):
            emit(event, value)

    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        code = main(args, progress=notify)
    status = "failed"
    if directory:
        try:
            status = json.loads((Path(directory) / "report.json").read_text(encoding="utf-8"))[
                "status"
            ]
        except (OSError, ValueError, KeyError):
            pass
    emit("finished", {"status": status, "diagnostic": errors.getvalue().strip()})
    return code


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--child":
        raise SystemExit(_child(sys.argv[2:]))
    raise SystemExit("Internal worker entry point")
