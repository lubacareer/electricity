"""Small, bounded subprocess runner used by external engine adapters."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Captured outcome of one isolated external command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False


def _stop_process(process: subprocess.Popen[str]) -> None:
    """Stop a process group, escalating only when a polite stop fails."""

    if process.poll() is not None:
        return

    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=0.5)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass

    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        # The caller still receives a bounded failure result. There is nothing
        # further a library process can safely do here without killing peers.
        pass


def run_isolated_process(
    argv: Sequence[str | Path],
    *,
    cwd: Path | None = None,
    timeout_s: float = 10.0,
    cancel_event: Event | None = None,
    env: Mapping[str, str] | None = None,
) -> ProcessResult:
    """Run an argv without a shell, with timeout and cooperative cancellation.

    A new process group prevents console-control events from leaking into the
    desktop application. Output is always decoded as UTF-8 with replacement so
    malformed native-tool output cannot crash the adapter.
    """

    command = tuple(str(part) for part in argv)
    if not command or not command[0]:
        raise ValueError("argv must contain an executable")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")

    popen_kwargs: dict[str, object] = {}
    if os.name == "nt":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
        creation_flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        popen_kwargs["creationflags"] = creation_flags
    else:
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(  # noqa: S603 - argv is intentionally not a shell string
        command,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs,
    )

    deadline = time.monotonic() + timeout_s
    while True:
        if cancel_event is not None and cancel_event.is_set():
            _stop_process(process)
            stdout, stderr = process.communicate()
            return ProcessResult(
                argv=command,
                returncode=process.returncode if process.returncode is not None else -1,
                stdout=stdout,
                stderr=stderr,
                cancelled=True,
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_process(process)
            stdout, stderr = process.communicate()
            return ProcessResult(
                argv=command,
                returncode=process.returncode if process.returncode is not None else -1,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
            )

        try:
            stdout, stderr = process.communicate(timeout=min(0.05, remaining))
            return ProcessResult(
                argv=command,
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        except subprocess.TimeoutExpired:
            continue
