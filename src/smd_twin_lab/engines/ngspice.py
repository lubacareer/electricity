"""Owned ngspice batch adapter for the reference NTC circuit."""

from __future__ import annotations

import math
import re
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from threading import Event

from ..models import (
    Diagnostic,
    DiagnosticSeverity,
    FaultKind,
    MessageRef,
    SignalSeries,
    SimulationRequest,
    SimulationResult,
)
from ..tooling import discover_tools
from .process import run_isolated_process
from .reference import (
    _GROUND_NETS,
    _SENSOR_NETS,
    _SUPPLY_NETS,
    FIXED_RESISTANCE_OHM,
    OPEN_RESISTANCE_OHM,
    SHORT_RESISTANCE_OHM,
    SUPPLY_VOLTAGE_V,
    _fault_targets_thermistor,
    ntc_resistance_ohm,
    temperature_from_divider_c,
)

_VERSION_PATTERN = re.compile(r"ngspice(?:-|\s+)([0-9][\w.+-]*)", re.IGNORECASE)


def _spice_number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("SPICE values must be finite")
    return f"{value:.12g}"


def _spice_output_name(path: Path) -> str:
    """Return a safe working-directory filename for ngspice ``wrdata``.

    Windows ngspice treats a quoted absolute path as a literal invalid
    argument.  Runs already have an isolated working directory, so a simple
    filename is both portable and sufficient.
    """

    name = path.name
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ValueError(
            "ngspice output filename must contain only letters, digits, ._- characters"
        )
    return name


def parse_wrdata(text: str) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Parse an ngspice ``wrdata`` file into finite time/value pairs."""

    times: list[float] = []
    values: list[float] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith(("#", "*")):
            continue
        tokens = [token for token in re.split(r"[\s,]+", line) if token]
        numeric: list[float] = []
        try:
            for token in tokens:
                numeric.append(float(token.replace("D", "E").replace("d", "e")))
        except ValueError:
            # wrdata emits a vector-name header; any other all-text metadata is
            # harmless. A partly numeric malformed row is rejected below.
            if not numeric:
                continue
            raise ValueError(f"Malformed ngspice data on line {line_number}") from None
        if len(numeric) < 2:
            raise ValueError(f"Expected time and value on line {line_number}")

        # Some ngspice builds repeat the scale for every vector. Selecting the
        # final two columns handles both ``time value`` and ``time time value``.
        time_s, value = numeric[-2], numeric[-1]
        if not math.isfinite(time_s) or not math.isfinite(value):
            raise ValueError(f"Non-finite ngspice data on line {line_number}")
        if times and time_s < times[-1]:
            raise ValueError("ngspice time values must be monotonic")
        times.append(time_s)
        values.append(value)

    if not times:
        raise ValueError("ngspice produced no waveform samples")
    return tuple(times), tuple(values)


def _fault_short_nodes(request: SimulationRequest) -> tuple[str, str] | None:
    a = (request.fault.net_a or "").strip().casefold()
    b = (request.fault.net_b or "").strip().casefold()
    if a in _SENSOR_NETS:
        other = b
    elif b in _SENSOR_NETS:
        other = a
    else:
        return None
    if other in _GROUND_NETS:
        return "ADC_SENSE", "0"
    if other in _SUPPLY_NETS:
        return "ADC_SENSE", "VDD"
    return None


def build_reference_deck(request: SimulationRequest, output_path: Path) -> str:
    """Generate a self-contained, non-mutating ngspice working deck."""

    if request.duration_s <= 0 or request.sample_count < 2:
        raise ValueError("A transient run needs positive duration and at least two samples")

    resistance = ntc_resistance_ohm(request.temperature_c)
    fault = request.fault
    if fault.kind in {
        FaultKind.COMPONENT_OPEN,
        FaultKind.WRONG_VALUE,
        FaultKind.REVERSED_POLARITY,
        FaultKind.INTERMITTENT,
    } and not _fault_targets_thermistor(fault.reference):
        raise ValueError("The reference ngspice model supports component faults only on RT1")
    if fault.kind is FaultKind.NET_SHORT and _fault_short_nodes(request) is None:
        raise ValueError("The reference ngspice model supports only ADC_SENSE-to-3V3/GND shorts")
    if fault.kind is FaultKind.COMPONENT_OPEN and _fault_targets_thermistor(fault.reference):
        resistance = OPEN_RESISTANCE_OHM
    elif fault.kind is FaultKind.WRONG_VALUE and _fault_targets_thermistor(fault.reference):
        if fault.value is None or not math.isfinite(fault.value) or fault.value <= 0:
            raise ValueError("Wrong-value fault requires a positive resistance in ohms")
        resistance = fault.value

    circuit_lines = [
        "V_SUPPLY VDD 0 DC 3.3",
        f".param NTC_RESISTANCE_OHM={_spice_number(resistance)}",
        f"R_FIXED VDD ADC_SENSE {_spice_number(FIXED_RESISTANCE_OHM)}",
        "C_ADC ADC_SENSE 0 1n",
    ]
    if fault.kind is FaultKind.INTERMITTENT and _fault_targets_thermistor(fault.reference):
        start_s = fault.start_s if fault.start_s is not None else request.duration_s * 0.25
        duration_s = fault.duration_s if fault.duration_s is not None else request.duration_s * 0.5
        if start_s < 0 or duration_s <= 0:
            raise ValueError("Intermittent fault needs non-negative start and positive duration")
        period_s = max(request.duration_s * 2.0, start_s + duration_s + request.duration_s)
        circuit_lines.extend(
            [
                "R_NTC ADC_SENSE NTC_FAULT_NODE {NTC_RESISTANCE_OHM}",
                "S_NTC NTC_FAULT_NODE 0 FAULT_CTRL 0 NTC_SWITCH",
                "V_FAULT_CTRL FAULT_CTRL 0 "
                f"PULSE(1 0 {_spice_number(start_s)} 1n 1n "
                f"{_spice_number(duration_s)} {_spice_number(period_s)})",
                ".model NTC_SWITCH SW(Ron=1m Roff=1T Vt=0.5 Vh=0)",
            ]
        )
    else:
        circuit_lines.append("R_NTC ADC_SENSE 0 {NTC_RESISTANCE_OHM}")

    if fault.kind is FaultKind.NET_SHORT:
        nodes = _fault_short_nodes(request)
        if nodes is not None:
            circuit_lines.append(
                f"R_FAULT {nodes[0]} {nodes[1]} {_spice_number(SHORT_RESISTANCE_OHM)}"
            )

    step_s = request.duration_s / (request.sample_count - 1)
    lines = [
        "* SMD Twin Lab generated reference-deck working copy",
        "* Source KiCad and SPICE files are never modified.",
        *circuit_lines,
        f".tran {_spice_number(step_s)} {_spice_number(request.duration_s)}",
        ".control",
        "set wr_vecnames",
        "set wr_singlescale",
        "run",
        "linearize v(ADC_SENSE)",
        f"wrdata {_spice_output_name(output_path)} v(ADC_SENSE)",
        "quit",
        ".endc",
        ".end",
        "",
    ]
    return "\n".join(lines)


class NgspiceBatchEngine:
    """ngspice 47 batch adapter with bounded process lifetime."""

    def __init__(
        self,
        executable: str | Path | None = None,
        *,
        command_prefix: Sequence[str | Path] | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        discovered = discover_tools().ngspice if executable is None else Path(executable)
        if command_prefix is not None:
            self._command_prefix = tuple(str(part) for part in command_prefix)
        elif discovered is not None:
            self._command_prefix = (str(discovered),)
        else:
            self._command_prefix = ()
        self.timeout_s = timeout_s

    @property
    def available(self) -> bool:
        return bool(self._command_prefix) and Path(self._command_prefix[0]).is_file()

    def run(
        self,
        request: SimulationRequest,
        *,
        cancel_event: Event | None = None,
    ) -> SimulationResult:
        if not self.available:
            return self._failure(
                "ngspice was not found. Install standalone ngspice 47 or set SMD_TWIN_NGSPICE.",
                "ngspice.unavailable",
                message_ref=MessageRef("diagnostic.ngspice.unavailable"),
            )

        diagnostics: list[Diagnostic] = []
        if request.netlist_path:
            diagnostics.append(
                Diagnostic(
                    severity=DiagnosticSeverity.WARNING,
                    code="ngspice.reference_model_used",
                    message=(
                        "This release uses the validated reference NTC model; the imported netlist "
                        "was not modified or executed."
                    ),
                    path=request.netlist_path,
                    message_ref=MessageRef("diagnostic.ngspice.reference_model_used"),
                )
            )

        try:
            with tempfile.TemporaryDirectory(prefix="smd-twin-ngspice-") as temporary_dir:
                work_dir = Path(temporary_dir)
                output_path = work_dir / "sensor.tsv"
                deck_path = work_dir / "reference.cir"
                deck = build_reference_deck(request, output_path)
                deck_path.write_text(deck, encoding="utf-8", newline="\n")
                process = run_isolated_process(
                    (*self._command_prefix, "-b", deck_path),
                    cwd=work_dir,
                    timeout_s=self.timeout_s,
                    cancel_event=cancel_event,
                )

                if process.cancelled:
                    return self._failure(
                        "ngspice simulation was cancelled.",
                        "ngspice.cancelled",
                        message_ref=MessageRef("diagnostic.ngspice.cancelled"),
                        stdout=process.stdout,
                        stderr=process.stderr,
                    )
                if process.timed_out:
                    return self._failure(
                        f"ngspice exceeded the {self.timeout_s:g} second timeout.",
                        "ngspice.timeout",
                        message_ref=MessageRef(
                            "diagnostic.ngspice.timeout",
                            {"timeout": f"{self.timeout_s:g}"},
                        ),
                        stdout=process.stdout,
                        stderr=process.stderr,
                    )
                if process.returncode != 0:
                    return self._failure(
                        f"ngspice exited with status {process.returncode}.",
                        "ngspice.failed",
                        message_ref=MessageRef(
                            "diagnostic.ngspice.failed",
                            {"status": process.returncode},
                        ),
                        stdout=process.stdout,
                        stderr=process.stderr,
                    )
                if not output_path.is_file():
                    return self._failure(
                        "ngspice did not create the expected waveform file.",
                        "ngspice.missing_output",
                        message_ref=MessageRef("diagnostic.ngspice.missing_output"),
                        stdout=process.stdout,
                        stderr=process.stderr,
                    )
                x_values, y_values = parse_wrdata(output_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return self._failure(
                str(exc),
                "ngspice.invalid_output",
                message_ref=MessageRef(
                    "diagnostic.ngspice.invalid_output",
                    {"detail": str(exc)},
                ),
            )

        resistance = ntc_resistance_ohm(request.temperature_c)
        if request.fault.kind is FaultKind.COMPONENT_OPEN and _fault_targets_thermistor(
            request.fault.reference
        ):
            resistance = OPEN_RESISTANCE_OHM
        elif (
            request.fault.kind is FaultKind.WRONG_VALUE
            and _fault_targets_thermistor(request.fault.reference)
            and request.fault.value is not None
        ):
            resistance = request.fault.value

        observed_voltage = y_values[-1]
        measurements = {
            "supply_voltage_v": SUPPLY_VOLTAGE_V,
            "adc_voltage_v": observed_voltage,
            "sensor_voltage_v": observed_voltage,
            "sensor_voltage_min_v": min(y_values),
            "sensor_voltage_max_v": max(y_values),
            "thermistor_resistance_ohm": resistance,
            "ambient_temperature_c": request.temperature_c,
        }
        with suppress(ValueError):
            measurements["inferred_temperature_c"] = temperature_from_divider_c(observed_voltage)

        version_match = _VERSION_PATTERN.search(process.stdout + "\n" + process.stderr)
        version = version_match.group(1) if version_match else "unknown"
        if version.split(".", 1)[0] != "47":
            return self._failure(
                f"Expected the pinned ngspice 47 runtime, but detected {version}.",
                "ngspice.unsupported_version",
                message_ref=MessageRef(
                    "diagnostic.ngspice.unsupported_version",
                    {"version": version},
                ),
                stdout=process.stdout,
                stderr=process.stderr,
            )
        return SimulationResult(
            success=True,
            engine="ngspice",
            engine_version=version,
            measurements=measurements,
            signals=(SignalSeries("ADC_SENSE", "V", x_values, y_values),),
            stdout=process.stdout,
            stderr=process.stderr,
            diagnostics=tuple(diagnostics),
        )

    @staticmethod
    def _failure(
        message: str,
        code: str,
        *,
        message_ref: MessageRef | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> SimulationResult:
        return SimulationResult(
            success=False,
            engine="ngspice",
            engine_version="unknown",
            measurements={},
            signals=(),
            stdout=stdout,
            stderr=stderr,
            diagnostics=(
                Diagnostic(
                    severity=DiagnosticSeverity.ERROR,
                    code=code,
                    message=message,
                    message_ref=message_ref,
                ),
            ),
        )
