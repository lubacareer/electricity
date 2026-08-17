"""Headless Renode adapter and deterministic qualification surface."""

from __future__ import annotations

import json
import math
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

from ..models import (
    Diagnostic,
    DiagnosticSeverity,
    FirmwareRequest,
    FirmwareResult,
    FirmwareState,
)
from ..tooling import discover_tools
from .process import run_isolated_process

RESULT_PREFIX = "SMD_TWIN_RESULT "
_VERSION_PATTERN = re.compile(r"Renode(?:\s+version)?\s+([0-9][\w.+-]*)", re.IGNORECASE)


def _renode_path(path: Path) -> str:
    return "@" + path.resolve().as_posix().replace(" ", "\\ ")


def build_renode_runner_script(
    request: FirmwareRequest,
    *,
    firmware_path: Path,
    integration_script_path: Path,
) -> str:
    """Build the tiny variable-binding script consumed by a board integration script.

    The project-owned integration script is responsible for creating the
    STM32G071 machine, loading ``$firmware``, injecting ``$adcVoltage`` and
    emitting one ``SMD_TWIN_RESULT`` JSON line after deterministic virtual time.
    """

    if request.duration_s <= 0:
        raise ValueError("Renode duration_s must be positive")
    if not math.isfinite(request.adc_voltage_v) or not math.isfinite(request.supply_voltage_v):
        raise ValueError("Renode voltage inputs must be finite")
    acknowledge = "true" if request.acknowledge else "false"
    return "\n".join(
        [
            f"$firmware={_renode_path(firmware_path)}",
            f"$adcVoltage={request.adc_voltage_v:.12g}",
            f"$supplyVoltage={request.supply_voltage_v:.12g}",
            f"$acknowledge={acknowledge}",
            f"$durationSeconds={request.duration_s:.12g}",
            f"include {_renode_path(integration_script_path)}",
            "quit",
            "",
        ]
    )


def _strict_json(line: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"Non-finite JSON number {value!r}")

    return json.loads(line, parse_constant=reject_constant)


def parse_renode_result(stdout: str) -> FirmwareResult:
    """Parse the last structured result marker emitted by a Renode script."""

    result_lines = [
        line[len(RESULT_PREFIX) :] for line in stdout.splitlines() if line.startswith(RESULT_PREFIX)
    ]
    if not result_lines:
        raise ValueError("Renode emitted no SMD_TWIN_RESULT line")
    payload = _strict_json(result_lines[-1])
    if not isinstance(payload, dict):
        raise ValueError("Renode result must be a JSON object")

    try:
        state = FirmwareState(payload["state"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("Renode result contains an invalid firmware state") from exc

    outputs = payload.get("outputs")
    if not isinstance(outputs, dict) or not all(
        isinstance(key, str)
        and isinstance(value, (bool, int, float))
        and (not isinstance(value, float) or math.isfinite(value))
        for key, value in outputs.items()
    ):
        raise ValueError("Renode outputs must be a string-to-boolean/number object")

    uart_lines = payload.get("uart_lines", [])
    events = payload.get("events", [])
    if not isinstance(uart_lines, list) or not all(isinstance(line, str) for line in uart_lines):
        raise ValueError("Renode uart_lines must be a list of strings")
    if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        raise ValueError("Renode events must be a list of objects")

    return FirmwareResult(
        success=True,
        engine="renode",
        state=state,
        outputs=dict(outputs),
        uart_lines=tuple(uart_lines),
        events=tuple(dict(event) for event in events),
    )


@dataclass(frozen=True, slots=True)
class RenodeQualification:
    available: bool
    deterministic: bool
    detail: str
    first: FirmwareResult | None = None
    second: FirmwareResult | None = None


class RenodeFirmwareEngine:
    """Run one STM32G071 integration script in a fresh Renode process."""

    def __init__(
        self,
        firmware_path: str | Path | None = None,
        integration_script_path: str | Path | None = None,
        *,
        executable: str | Path | None = None,
        command_prefix: Sequence[str | Path] | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        discovered = discover_tools().renode if executable is None else Path(executable)
        if command_prefix is not None:
            self._command_prefix = tuple(str(part) for part in command_prefix)
        elif discovered is not None:
            self._command_prefix = (str(discovered),)
        else:
            self._command_prefix = ()
        self.firmware_path = Path(firmware_path).resolve() if firmware_path else None
        self.integration_script_path = (
            Path(integration_script_path).resolve() if integration_script_path else None
        )
        self.timeout_s = timeout_s

    @property
    def available(self) -> bool:
        return bool(self._command_prefix) and all(
            path is not None and path.is_file()
            for path in (
                Path(self._command_prefix[0]),
                self.firmware_path,
                self.integration_script_path,
            )
        )

    def load_and_step(
        self,
        request: FirmwareRequest,
        *,
        cancel_event: Event | None = None,
    ) -> FirmwareResult:
        if not self.available:
            return self._failure(
                "Renode, the firmware ELF, or the STM32G071 integration script is unavailable.",
                "renode.unavailable",
            )

        assert self.firmware_path is not None
        assert self.integration_script_path is not None
        try:
            with tempfile.TemporaryDirectory(prefix="smd-twin-renode-") as temporary_dir:
                work_dir = Path(temporary_dir)
                runner_path = work_dir / "run.resc"
                runner_path.write_text(
                    build_renode_runner_script(
                        request,
                        firmware_path=self.firmware_path,
                        integration_script_path=self.integration_script_path,
                    ),
                    encoding="utf-8",
                    newline="\n",
                )
                process = run_isolated_process(
                    (
                        *self._command_prefix,
                        "--disable-xwt",
                        "--plain",
                        "--execute",
                        f"include {_renode_path(runner_path)}",
                    ),
                    cwd=work_dir,
                    timeout_s=self.timeout_s,
                    cancel_event=cancel_event,
                )
        except (OSError, ValueError) as exc:
            return self._failure(str(exc), "renode.launch_failed")

        if process.cancelled:
            return self._failure(
                "Renode execution was cancelled.",
                "renode.cancelled",
                stderr=process.stderr,
            )
        if process.timed_out:
            return self._failure(
                f"Renode exceeded the {self.timeout_s:g} second timeout.",
                "renode.timeout",
                stderr=process.stderr,
            )
        if process.returncode != 0:
            return self._failure(
                f"Renode exited with status {process.returncode}.",
                "renode.failed",
                stderr=process.stderr,
            )
        try:
            parsed = parse_renode_result(process.stdout)
        except ValueError as exc:
            return self._failure(
                str(exc),
                "renode.invalid_output",
                stderr=process.stderr,
            )

        version_match = _VERSION_PATTERN.search(process.stdout + "\n" + process.stderr)
        version_event = {
            "kind": "engine",
            "version": version_match.group(1) if version_match else "unknown",
        }
        return FirmwareResult(
            success=parsed.success,
            engine=parsed.engine,
            state=parsed.state,
            outputs=parsed.outputs,
            uart_lines=parsed.uart_lines,
            events=(version_event, *parsed.events),
            diagnostics=parsed.diagnostics,
        )

    def qualify(self, request: FirmwareRequest) -> RenodeQualification:
        """Repeat an identical fresh-machine run and compare observable events."""

        if not self.available:
            return RenodeQualification(
                available=False,
                deterministic=False,
                detail="Renode qualification prerequisites are unavailable.",
            )
        first = self.load_and_step(request)
        second = self.load_and_step(request)
        deterministic = first.success and second.success and first == second
        return RenodeQualification(
            available=True,
            deterministic=deterministic,
            detail=(
                "Repeated UART, GPIO, ADC, and virtual-time observations match."
                if deterministic
                else "Repeated Renode observations differ or a run failed."
            ),
            first=first,
            second=second,
        )

    @staticmethod
    def _failure(message: str, code: str, *, stderr: str = "") -> FirmwareResult:
        return FirmwareResult(
            success=False,
            engine="renode",
            state=FirmwareState.SENSOR_FAULT,
            outputs={"green_led": False, "red_led": True, "buzzer": True},
            uart_lines=(),
            diagnostics=(
                Diagnostic(
                    severity=DiagnosticSeverity.ERROR,
                    code=code,
                    message=message,
                ),
            ),
        )
